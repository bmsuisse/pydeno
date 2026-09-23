//! Isolate termination state and the per-runtime deadline watchdog thread.

use crate::runtime::error::{RuntimeError, RuntimeResult};
use deno_core::v8;
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

const TERMINATION_STATUS_RUNNING: u8 = 0;
const TERMINATION_STATUS_REQUESTED: u8 = 1;
const TERMINATION_STATUS_TERMINATED: u8 = 2;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Thread-safe controller for V8 isolate termination.
///
/// Provides a clone-able handle to request and track termination of a V8 isolate.
/// Uses atomic operations to coordinate termination state across threads.
#[derive(Clone)]
pub struct TerminationController {
    inner: Arc<TerminationState>,
}

struct TerminationState {
    /// 0=running, 1=requested, 2=terminated.
    status: AtomicU8,
    isolate_handle: v8::IsolateHandle,
    reason: Mutex<Option<String>>,
    /// The dispatcher's wake handle (shared with `DispatcherWaker`): `request()`
    /// signals it so a parked dispatcher observes an off-thread termination at
    /// once instead of on the next `PENDING_WORK_TICK`.
    dispatcher_wake: Arc<tokio::sync::Notify>,
}

impl TerminationController {
    pub(super) fn new(isolate_handle: v8::IsolateHandle) -> Self {
        Self {
            inner: Arc::new(TerminationState {
                status: AtomicU8::new(TERMINATION_STATUS_RUNNING),
                isolate_handle,
                reason: Mutex::new(None),
                dispatcher_wake: Arc::new(tokio::sync::Notify::new()),
            }),
        }
    }

    pub(super) fn dispatcher_wake(&self) -> Arc<tokio::sync::Notify> {
        self.inner.dispatcher_wake.clone()
    }

    pub fn request(&self) -> bool {
        let first = self
            .inner
            .status
            .compare_exchange(
                TERMINATION_STATUS_RUNNING,
                TERMINATION_STATUS_REQUESTED,
                Ordering::SeqCst,
                Ordering::SeqCst,
            )
            .is_ok();
        // Wake even on a repeat request; a spurious wake costs one loop iteration.
        self.inner.dispatcher_wake.notify_one();
        first
    }

    pub fn terminate_execution(&self) {
        self.inner.isolate_handle.terminate_execution();
    }

    pub fn ensure_reason(&self, reason: impl Into<String>) {
        let mut guard = self.inner.reason.lock().unwrap();
        if guard.is_none() {
            *guard = Some(reason.into());
        }
    }

    pub fn reason(&self) -> Option<String> {
        self.inner.reason.lock().unwrap().clone()
    }

    pub fn terminated_error(&self) -> RuntimeError {
        match self.reason() {
            Some(reason) => RuntimeError::terminated_with(reason),
            None => RuntimeError::terminated(),
        }
    }

    pub fn is_requested(&self) -> bool {
        matches!(
            self.inner.status.load(Ordering::SeqCst),
            TERMINATION_STATUS_REQUESTED | TERMINATION_STATUS_TERMINATED
        )
    }

    pub fn is_terminated(&self) -> bool {
        self.inner.status.load(Ordering::SeqCst) == TERMINATION_STATUS_TERMINATED
    }

    /// Mark the isolate terminated from the *host* side. Used only by the
    /// force-kill escalation in `RuntimeHandle::recv_result`, where the runtime
    /// thread is wedged and will never mark itself.
    pub fn force_mark_terminated(&self) {
        self.mark_terminated();
    }

    pub(super) fn mark_terminated(&self) -> bool {
        self.inner
            .status
            .swap(TERMINATION_STATUS_TERMINATED, Ordering::SeqCst)
            != TERMINATION_STATUS_TERMINATED
    }
}

/// One armed deadline tracked by the [`Watchdog`] thread.
///
/// Several can be outstanding (a sync command runs inline while an async job
/// is parked on its own timed promise). Enforcement is isolate-wide
/// (`terminate_execution` has no per-job scope), so a deadline stops whatever
/// is running at that moment, not necessarily the job that set it; that
/// cross-talk is a documented contract pinned by
/// `tests/test_timeout_cross_talk.py`. Use a runtime per concurrent job if a
/// bare termination error must be attributable.
struct ArmedDeadline {
    id: u64,
    deadline: Instant,
    reason: String,
    fired: bool,
}

struct WatchdogState {
    armed: Mutex<Vec<ArmedDeadline>>,
    /// Signalled on `arm` (a sooner deadline may exist) and on shutdown.
    /// `disarm` need not signal: removing an entry only pushes the wakeup later.
    wake: Condvar,
    /// Read by the watchdog while it holds `armed`, so writers must hold
    /// `armed` too or the `wake` notification can be lost (see `Drop`).
    shutdown: Mutex<bool>,
    next_id: AtomicU64,
}

/// One long-lived watchdog thread per runtime, so arming a deadline costs a
/// mutex rather than a thread spawn + join per timed call.
pub(super) struct Watchdog {
    state: Arc<WatchdogState>,
    handle: Option<thread::JoinHandle<()>>,
}

/// Identifies one armed deadline for [`Watchdog::disarm`].
pub(super) struct WatchdogToken {
    id: u64,
    pub(super) duration: Duration,
}

impl Watchdog {
    pub(super) fn spawn(termination: TerminationController) -> RuntimeResult<Self> {
        let state = Arc::new(WatchdogState {
            armed: Mutex::new(Vec::new()),
            wake: Condvar::new(),
            shutdown: Mutex::new(false),
            next_id: AtomicU64::new(0),
        });
        let thread_state = state.clone();

        let handle = thread::Builder::new()
            .name("pydeno-watchdog".to_string())
            .spawn(move || loop {
                let mut armed = lock(&thread_state.armed);
                if *lock(&thread_state.shutdown) {
                    return;
                }

                let now = Instant::now();
                let mut any_fired = false;
                for entry in armed.iter_mut().filter(|e| !e.fired && e.deadline <= now) {
                    entry.fired = true;
                    any_fired = true;
                }

                // One termination covers every deadline that just expired;
                // report the one that expired first.
                if any_fired {
                    let reason = armed
                        .iter()
                        .filter(|entry| entry.fired)
                        .min_by_key(|entry| entry.deadline)
                        .map(|entry| entry.reason.clone());
                    // Hold `armed` until `terminate_execution` is issued:
                    // `disarm` reads `fired` under this lock and its caller
                    // cancels the termination, so releasing earlier lets a late
                    // terminate latch the isolate for the next, unrelated call.
                    if let Some(reason) = reason {
                        termination.ensure_reason(reason);
                    }
                    termination.terminate_execution();
                    drop(armed);
                    continue;
                }

                let next_deadline = armed
                    .iter()
                    .filter(|entry| !entry.fired)
                    .map(|entry| entry.deadline)
                    .min();
                let _guard = match next_deadline {
                    None => thread_state
                        .wake
                        .wait(armed)
                        .unwrap_or_else(PoisonError::into_inner),
                    Some(deadline) => {
                        thread_state
                            .wake
                            .wait_timeout(armed, deadline.saturating_duration_since(now))
                            .unwrap_or_else(PoisonError::into_inner)
                            .0
                    }
                };
            })
            .map_err(|e| {
                RuntimeError::internal(format!("Failed to spawn watchdog thread: {}", e))
            })?;

        Ok(Self {
            state,
            handle: Some(handle),
        })
    }

    /// Arm a deadline `duration` from now.
    pub(super) fn arm(&self, duration: Duration, reason: impl Into<String>) -> WatchdogToken {
        let id = self.state.next_id.fetch_add(1, Ordering::Relaxed);
        lock(&self.state.armed).push(ArmedDeadline {
            id,
            deadline: Instant::now() + duration,
            reason: reason.into(),
            fired: false,
        });
        // The new deadline may be sooner than what the thread sleeps toward.
        self.state.wake.notify_one();
        WatchdogToken { id, duration }
    }

    /// Remove an armed deadline, returning whether it had already fired.
    pub(super) fn disarm(&self, token: WatchdogToken) -> bool {
        let mut armed = lock(&self.state.armed);
        armed
            .iter()
            .position(|entry| entry.id == token.id)
            .is_some_and(|index| armed.swap_remove(index).fired)
    }
}

impl Drop for Watchdog {
    fn drop(&mut self) {
        {
            // Hold `armed` (the condvar's mutex) across the flag write so the
            // notify cannot land between the thread's shutdown read and its
            // park -- a lost wakeup there hung `close()` forever. Lock order
            // matches the watchdog thread's (`armed`, then `shutdown`).
            let _armed = lock(&self.state.armed);
            *lock(&self.state.shutdown) = true;
        }
        self.state.wake.notify_one();
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}
