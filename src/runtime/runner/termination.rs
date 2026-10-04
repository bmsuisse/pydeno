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

/// While a fired deadline is still armed, the watchdog re-issues `terminate_execution` this often.
///
/// One termination is not enough: when it stops a script, deno_core converts the resulting
/// "execution terminated" error after *cancelling* the termination, and that conversion reads
/// properties of the error (`constructor`, `name`, `cause`, `stack`...) whose prototype is the
/// guest's `Error.prototype`. A getter there would otherwise run with no deadline left. Re-issuing
/// cuts each such read short; the caller's `disarm` (and its cancel) ends the repeats.
const REISSUE_INTERVAL: Duration = Duration::from_millis(20);

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

    /// Request termination with `reason`. The first request's reason replaces whatever a
    /// deadline left in the slot (a timed-out call may still be unwinding), and it is written
    /// under the same lock `reason()` and `clear_handled_reason()` take, so whoever observes the
    /// request reads this reason. A repeat request leaves the first one's reason in place.
    pub fn request_with_reason(&self, reason: impl Into<String>) -> bool {
        let mut guard = lock(&self.inner.reason);
        let first = self.request();
        if first {
            *guard = Some(reason.into());
        }
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

    /// Forget the reason of a termination that has been handled (cancelled), so a later,
    /// unrelated termination does not report it. Kept while a termination is requested.
    pub(super) fn clear_handled_reason(&self) {
        let mut guard = lock(&self.inner.reason);
        if !self.is_requested() {
            *guard = None;
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

/// What the watchdog thread is doing, so `arm` wakes it only when it must.
#[derive(Clone, Copy)]
enum Parked {
    /// Running (or about to run) its loop, which reads every armed entry afresh.
    Busy,
    /// Waiting with nothing armed: any new deadline needs a wake.
    Forever,
    /// Waiting to wake at this instant: only a deadline before it needs a wake.
    Until(Instant),
}

struct Armed {
    entries: Vec<ArmedDeadline>,
    parked: Parked,
}

struct WatchdogState {
    armed: Mutex<Armed>,
    /// Signalled on `arm` when the new deadline is sooner than the one the thread
    /// sleeps toward, and on shutdown. Every timed call arms and disarms, and a
    /// wake per call costs the runtime thread two context switches and a
    /// contended lock; a thread that already wakes earlier needs none, it
    /// simply re-reads the entries then. `disarm` need not signal: removing an
    /// entry only pushes the wakeup later.
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
            armed: Mutex::new(Armed {
                entries: Vec::new(),
                parked: Parked::Busy,
            }),
            wake: Condvar::new(),
            shutdown: Mutex::new(false),
            next_id: AtomicU64::new(0),
        });
        let thread_state = state.clone();

        let handle = thread::Builder::new()
            .name("pydeno-watchdog".to_string())
            .spawn(move || {
                let mut last_terminate: Option<Instant> = None;
                loop {
                    let mut armed = lock(&thread_state.armed);
                    if *lock(&thread_state.shutdown) {
                        return;
                    }

                    let now = Instant::now();
                    let mut any_fired = false;
                    for entry in armed
                        .entries
                        .iter_mut()
                        .filter(|e| !e.fired && e.deadline <= now)
                    {
                        entry.fired = true;
                        any_fired = true;
                    }
                    let outstanding = armed.entries.iter().any(|entry| entry.fired);
                    let reissue_due = outstanding
                        && last_terminate
                            .is_none_or(|at| now.saturating_duration_since(at) >= REISSUE_INTERVAL);

                    // One termination covers every deadline that just expired;
                    // report the one that expired first.
                    if any_fired || reissue_due {
                        if any_fired {
                            let reason = armed
                                .entries
                                .iter()
                                .filter(|entry| entry.fired)
                                .min_by_key(|entry| entry.deadline)
                                .map(|entry| entry.reason.clone());
                            if let Some(reason) = reason {
                                termination.ensure_reason(reason);
                            }
                        }
                        // Hold `armed` until `terminate_execution` is issued:
                        // `disarm` reads `fired` under this lock and its caller
                        // cancels the termination, so releasing earlier lets a late
                        // terminate latch the isolate for the next, unrelated call.
                        termination.terminate_execution();
                        last_terminate = Some(now);
                    }

                    let mut next_deadline = armed
                        .entries
                        .iter()
                        .filter(|entry| !entry.fired)
                        .map(|entry| entry.deadline)
                        .min();
                    if outstanding {
                        let reissue_at = now + REISSUE_INTERVAL;
                        next_deadline =
                            Some(next_deadline.map_or(reissue_at, |d| d.min(reissue_at)));
                    }
                    // Recorded from the FINAL next deadline (after the re-issue
                    // adjustment above), under the lock the wait releases
                    // atomically, so an `arm` either sees it or runs before this
                    // iteration read the entries: no deadline can be missed, and
                    // a later deadline than the re-issue tick needs no wake.
                    armed.parked = next_deadline.map_or(Parked::Forever, Parked::Until);
                    let mut guard = match next_deadline {
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
                    guard.parked = Parked::Busy;
                }
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
        let deadline = Instant::now() + duration;
        let mut armed = lock(&self.state.armed);
        armed.entries.push(ArmedDeadline {
            id,
            deadline,
            reason: reason.into(),
            fired: false,
        });
        // Wake the thread only if it sleeps past the new deadline.
        let wake = match armed.parked {
            Parked::Busy => false,
            Parked::Forever => true,
            Parked::Until(at) => deadline < at,
        };
        drop(armed);
        if wake {
            self.state.wake.notify_one();
        }
        WatchdogToken { id, duration }
    }

    /// Remove an armed deadline, returning whether it had already fired.
    pub(super) fn disarm(&self, token: WatchdogToken) -> bool {
        let mut armed = lock(&self.state.armed);
        armed
            .entries
            .iter()
            .position(|entry| entry.id == token.id)
            .is_some_and(|index| armed.entries.swap_remove(index).fired)
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
