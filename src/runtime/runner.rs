//! Runtime thread backed by `deno_core::JsRuntime`.
//!
//! This module hosts the JavaScript engine on a dedicated OS thread with a
//! single-threaded Tokio runtime. Commands from Python are forwarded through
//! [`RuntimeCommand`] and executed sequentially on that thread.

use crate::runtime::config::RuntimeConfig;
use crate::runtime::error::{JsExceptionDetails, RuntimeError, RuntimeResult};
use crate::runtime::handle::BoundObjectProperty;
use crate::runtime::inspector::{
    InspectorConnectionState, InspectorMetadata, InspectorRegistration,
    InspectorRegistrationParams, InspectorServer,
};
use crate::runtime::js_value::{
    record_stack_anchor, JSValue, LimitTracker, SerializationLimits, RUNTIME_THREAD_STACK_SIZE,
};
use crate::runtime::loader::PythonModuleLoader;
use crate::runtime::ops::{python_extension, OpToken, PythonOpMode, PythonOpRegistry};
use crate::runtime::stats::{
    ActivitySummary, HeapSnapshot, RuntimeCallKind, RuntimeStatsSnapshot, RuntimeStatsState,
};
use crate::runtime::stream::{JsStreamRegistry, PyStreamRegistry, StreamChunk};
use deno_core::error::{CoreError, JsError};
use deno_core::stats::{RuntimeActivityStatsFactory, RuntimeActivityStatsFilter};
use deno_core::{v8, JsRuntime, PollEventLoopOptions, RuntimeOptions};
use indexmap::IndexMap;
use num_bigint::{BigInt, Sign};
use pyo3::prelude::Py;
use pyo3::PyAny;
use pyo3_async_runtimes::TaskLocals;
use std::cell::RefCell;
use std::collections::HashMap;
use std::ptr;
use std::ptr::NonNull;
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, AtomicU8, AtomicUsize, Ordering};
use std::sync::mpsc::Receiver as StdReceiver;
use std::sync::mpsc::Sender as StdSender;
use std::sync::mpsc::Sender;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;
use tokio::sync::oneshot;

type RuntimeInitResult = RuntimeResult<(
    TerminationController,
    Option<(InspectorMetadata, InspectorConnectionState)>,
    PyStreamRegistry,
)>;
type InitSignalChannel = (StdSender<RuntimeInitResult>, StdReceiver<RuntimeInitResult>);
type SpawnRuntimeResult = (
    mpsc::UnboundedSender<RuntimeCommand>,
    TerminationController,
    Option<(InspectorMetadata, InspectorConnectionState)>,
    PyStreamRegistry,
);

static ACTIVE_RUNTIME_THREADS: AtomicUsize = AtomicUsize::new(0);

struct RuntimeThreadGuard;

impl RuntimeThreadGuard {
    fn new() -> Self {
        ACTIVE_RUNTIME_THREADS.fetch_add(1, Ordering::SeqCst);
        Self
    }
}

impl Drop for RuntimeThreadGuard {
    fn drop(&mut self) {
        ACTIVE_RUNTIME_THREADS.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Stored function with optional receiver for 'this' binding.
///
/// Holds a V8 global handle to a JavaScript function and an optional receiver
/// object for method calls.
struct StoredFunction {
    /// Global handle to the JavaScript function.
    function: v8::Global<v8::Function>,
    /// Optional receiver object for method invocation ('this' binding).
    receiver: Option<v8::Global<v8::Value>>,
}

/// Pending JavaScript promise produced by a synchronous call.
///
/// Tracks a promise returned from a function call that hasn't resolved yet,
/// along with timeout information.
struct PendingFunctionCall {
    /// Global handle to the pending promise.
    promise: v8::Global<v8::Promise>,
    /// Time when the call started (for tracking elapsed time).
    start_time: Instant,
    /// Absolute deadline for timeout (if specified).
    deadline: Option<Instant>,
    /// Timeout duration in milliseconds (if specified).
    timeout_ms: Option<u64>,
}

/// Outcome of attempting to call a JS function synchronously.
///
/// A function call can either complete immediately (non-promise return value)
/// or require asynchronous polling (promise return value).
pub enum FunctionCallResult {
    /// Function returned a non-promise value immediately.
    Immediate(JSValue),
    /// Function returned a promise; call ID can be used to resume.
    Pending { call_id: u64 },
}

const TERMINATION_STATUS_RUNNING: u8 = 0;
const TERMINATION_STATUS_REQUESTED: u8 = 1;
const TERMINATION_STATUS_TERMINATED: u8 = 2;
/// Minimum amount of memory we temporarily add to the heap limit when V8 tells us
/// it's out of space so that it can unwind and propagate an exception instead of
/// killing the process.
const NEAR_HEAP_LIMIT_MIN_HEADROOM_BYTES: usize = 1024 * 1024; // 1 MiB

/// Thread-safe controller for V8 isolate termination.
///
/// Provides a clone-able handle to request and track termination of a V8 isolate.
/// Uses atomic operations to coordinate termination state across threads.
#[derive(Clone)]
pub struct TerminationController {
    inner: Arc<TerminationState>,
}

/// Internal state for termination tracking.
struct TerminationState {
    /// Atomic status: 0=running, 1=requested, 2=terminated.
    status: AtomicU8,
    /// V8 isolate handle for triggering execution termination.
    isolate_handle: v8::IsolateHandle,
    /// Optional reason describing why termination was requested.
    reason: Mutex<Option<String>>,
    /// The dispatcher's wake handle -- the same `Notify` that backs
    /// `DispatcherWaker`.
    ///
    /// `request()` signals this so the `2b.` block in `RuntimeDispatcher::run`
    /// observes an off-thread termination *immediately* rather than on the next
    /// `PENDING_WORK_TICK`. Without it the tick is load-bearing for
    /// parked-promise termination, which is what made it impossible to raise
    /// the tick (the `test_parked_termination` suite failed outright at a
    /// raised tick before this existed).
    dispatcher_wake: Arc<tokio::sync::Notify>,
}

/// One armed deadline tracked by the persistent [`Watchdog`] thread.
///
/// More than one can be outstanding at once: a sync command
/// (`RuntimeCommand::Eval`, `CallFunctionSync`, `EvalModule`) is handled
/// inline in `RuntimeDispatcher::run` and can therefore be dispatched while
/// an unrelated async job is still parked waiting on its own, independently
/// timed, promise -- both need their own deadline enforced, so the watchdog
/// tracks a small set rather than a single slot.
///
/// # Deadlines are not attributed to the job that set them
///
/// The set is tracked per job, but the *enforcement* is not: firing calls
/// `terminate_execution()`, which is isolate-wide, and the isolate runs one
/// thing at a time. So a deadline stops whatever the isolate is running at
/// that moment, which need not be the job whose deadline expired.
///
/// Concretely, and reproducible on this checkout: an async job parked on a
/// never-resolving promise under a 0.5 s timeout, with a sync `Eval`
/// dispatched inline 0.2 s later and no timeout of its own, ends with the
/// async job correctly reporting `Evaluation timed out after 500ms (promise
/// still pending)` and the *sync* call -- which had no deadline at all --
/// failing with a bare `Error: execution terminated`. Its own token never
/// fired, so `apply_watchdog_result` has nothing to convert the termination
/// into and passes it through.
///
/// This is a property of one-isolate-one-thread, not a bug in the set:
/// `v8::Isolate::terminate_execution` has no per-job scope, there is nothing
/// finer to aim at, and a runtime can be wedged inside blocking JS belonging
/// to any job. Attributing a deadline would mean either declining to fire
/// while an unrelated job is inline (which defeats the deadline whenever the
/// wedged job *is* the inline one) or tracking which job owns the isolate at
/// every instant, which is the one-isolate-one-thread model with extra steps.
/// 0.4.1 leaves the behaviour alone and states it here and on
/// `RuntimeConfig.timeout`; `tests/test_timeout_cross_talk.py` pins it, so
/// this is a known contract rather than a surprise.
///
/// Practical consequence for callers: with concurrent work on one runtime, a
/// bare termination error is not necessarily about the call that raised it.
/// If that matters, use a runtime per concurrent job.
///
/// The cross-talk is the *only* thing left here. It used to be compounded by
/// a real defect found while pinning it -- `JobCommon::expired` asked V8 to
/// terminate and nothing cancelled it, so one async timeout on a pending
/// promise latched the isolate and every later call failed -- which made a
/// runtime unusable rather than merely confusing. That is fixed: `expired`
/// records the request and `JobCommon::respond` clears it, the async
/// counterpart of `resolve_sync_watchdog`. See
/// `test_runtime_survives_an_async_deadline_on_a_pending_promise`.
struct ArmedDeadline {
    id: u64,
    deadline: Instant,
    reason: String,
    fired: bool,
}

struct WatchdogState {
    armed: Mutex<Vec<ArmedDeadline>>,
    /// Signalled on `arm` (a new, possibly sooner, deadline exists) and on
    /// `shutdown` (the thread should stop). `disarm` does not need to signal
    /// this: removing an entry can only push the next wakeup *later*, and the
    /// watchdog re-evaluates the remaining set every time it wakes anyway.
    wake: Condvar,
    /// Set once, by `Watchdog::drop`, to stop the thread.
    ///
    /// It has its own mutex, but it is *read* by the watchdog thread while
    /// that thread holds `armed`, so any writer must hold `armed` too across
    /// the write -- otherwise the `wake` notification can land in the window
    /// between that read and the `Condvar::wait` that parks the thread, and
    /// be lost. See `Watchdog::drop`.
    shutdown: Mutex<bool>,
    next_id: AtomicU64,
}

/// One long-lived watchdog thread per runtime, replacing a thread spawned
/// and joined for every timed call.
///
/// Before this, every timed sync call (and every timed async job, which also
/// needs a real OS thread able to call `terminate_execution()` from outside
/// in case the runtime thread itself is wedged inside blocking JS) paid a
/// full `thread::spawn` + `join` just to arm a deadline -- measured on this
/// checkout at roughly 16.9 -> 40.3us for a timed eval and 22.2 -> 44.7us for
/// a timed tool call, i.e. spawn/join overhead alone was the majority of the
/// cost of *any* deadline at all. `arm`/`disarm` here only take a mutex.
struct Watchdog {
    state: Arc<WatchdogState>,
    handle: Option<thread::JoinHandle<()>>,
}

/// Returned by [`Watchdog::arm`]; pass back to [`Watchdog::disarm`] to
/// identify which armed deadline is being resolved, since more than one may
/// be outstanding at a time.
struct WatchdogToken {
    id: u64,
    duration: Duration,
}

impl Watchdog {
    fn spawn(termination: TerminationController) -> RuntimeResult<Self> {
        let state = Arc::new(WatchdogState {
            armed: Mutex::new(Vec::new()),
            wake: Condvar::new(),
            shutdown: Mutex::new(false),
            next_id: AtomicU64::new(0),
        });
        let state_for_thread = state.clone();

        let handle = thread::Builder::new()
            .name("pydeno-watchdog".to_string())
            .spawn(move || {
                loop {
                    let mut armed = state_for_thread
                        .armed
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());

                    if *state_for_thread
                        .shutdown
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                    {
                        return;
                    }

                    let now = Instant::now();
                    let mut any_fired = false;
                    for entry in armed.iter_mut() {
                        if !entry.fired && entry.deadline <= now {
                            entry.fired = true;
                            any_fired = true;
                        }
                    }

                    // The isolate can only be running one thing at a time, so
                    // firing once covers every deadline that just expired.
                    // Terminating is idempotent (deno_core/V8 tolerate a
                    // repeat `terminate_execution` call) and cheap, so there
                    // is no need to pick "the" expired entry.
                    if any_fired {
                        // Pick the deadline that expired *first*, not whatever
                        // `Vec` order happens to be: `disarm` uses
                        // `swap_remove`, so position carries no meaning, and
                        // taking the last fired entry made the reason attached
                        // to a multi-expiry pass effectively arbitrary.
                        let reason = armed
                            .iter()
                            .filter(|entry| entry.fired)
                            .min_by_key(|entry| entry.deadline)
                            .map(|entry| entry.reason.clone());
                        // Keep holding `armed` until `terminate_execution` has
                        // been issued. `disarm` reads `fired` under this lock
                        // and its caller cancels the termination when it sees
                        // `true`. Released any earlier, a call that finished
                        // on its own just past its deadline could be disarmed
                        // (`fired == true`) and cancel a termination not yet
                        // requested -- after which this thread's late
                        // `terminate_execution` latched the isolate and the
                        // *next*, unrelated, call failed with a bare
                        // `execution terminated`. Neither call below takes
                        // `armed`, so this adds no lock-order edge.
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

                    match next_deadline {
                        None => {
                            // Nothing armed: sleep until `arm` or `shutdown`
                            // signals us, however long that takes.
                            let _guard = state_for_thread
                                .wake
                                .wait(armed)
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                        }
                        Some(deadline) => {
                            let remaining = deadline.saturating_duration_since(now);
                            let _guard = state_for_thread
                                .wake
                                .wait_timeout(armed, remaining)
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                        }
                    }
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

    /// Arm a new deadline `duration` from now, returning a token to disarm it
    /// with. Replaces `SyncWatchdog::spawn`.
    fn arm(&self, duration: Duration, reason: impl Into<String>) -> WatchdogToken {
        let id = self.state.next_id.fetch_add(1, Ordering::Relaxed);
        {
            let mut armed = self
                .state
                .armed
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            armed.push(ArmedDeadline {
                id,
                deadline: Instant::now() + duration,
                reason: reason.into(),
                fired: false,
            });
        }
        // A new deadline can be sooner than whatever the watchdog thread is
        // currently sleeping toward (or it may have been idle, sleeping
        // forever), so it must be woken to re-evaluate.
        self.state.wake.notify_one();
        WatchdogToken { id, duration }
    }

    /// Resolve (remove) a previously armed deadline, returning whether it had
    /// already fired. Replaces `resolve_sync_watchdog`'s cancel-then-join.
    fn disarm(&self, token: WatchdogToken) -> bool {
        let mut armed = self
            .state
            .armed
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let index = armed.iter().position(|entry| entry.id == token.id);
        match index {
            Some(index) => armed.swap_remove(index).fired,
            // Already resolved (e.g. a duplicate disarm); nothing fired that
            // this caller hasn't already observed.
            None => false,
        }
    }
}

impl Drop for Watchdog {
    fn drop(&mut self) {
        {
            // Take `armed` -- the mutex `wake` is paired with -- *before*
            // setting `shutdown`, and hold it across the flag write.
            //
            // `shutdown` lives behind its own mutex, but the watchdog thread
            // reads it while holding `armed`, and then releases `armed`
            // atomically as it enters `Condvar::wait`. Setting the flag
            // without `armed` therefore had a real lost-wakeup window: the
            // watchdog could read `shutdown == false`, and only *after* that
            // read (but before it was actually parked) this `notify_one`
            // could fire against no waiter. A condvar notification is not
            // queued, so it was simply dropped; with nothing armed, the
            // watchdog then parked on the unbounded `wait` arm forever and
            // the `join` below never returned -- i.e. closing a runtime
            // could hang. Acquiring `armed` here serialises against that
            // whole read-then-park critical section, so the notify either
            // reaches a parked thread or is unnecessary because the flag is
            // already visible on the watchdog's next read.
            //
            // Lock order (`armed` then `shutdown`) matches the watchdog
            // thread's own order, so this cannot deadlock.
            let _armed = self
                .state
                .armed
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let mut shutdown = self
                .state
                .shutdown
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *shutdown = true;
        }
        self.state.wake.notify_one();
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

struct OwnedSnapshot {
    data: Option<Box<[u8]>>,
    leaked_ptr: Option<NonNull<[u8]>>,
}

impl OwnedSnapshot {
    fn new(bytes: Vec<u8>) -> Self {
        Self {
            data: Some(bytes.into_boxed_slice()),
            leaked_ptr: None,
        }
    }

    fn as_static(&mut self) -> &'static [u8] {
        if let Some(ptr) = self.leaked_ptr {
            // SAFETY: pointer remains valid until Drop reconstructs the box.
            return unsafe { ptr.as_ref() };
        }

        let boxed = self
            .data
            .take()
            .expect("OwnedSnapshot bytes already leaked");
        let leaked: &'static mut [u8] = Box::leak(boxed);
        self.leaked_ptr = Some(NonNull::from(&mut *leaked));
        leaked
    }
}

impl Drop for OwnedSnapshot {
    fn drop(&mut self) {
        if let Some(ptr) = self.leaked_ptr.take() {
            // SAFETY: pointer came from Box::leak and has not been reclaimed yet.
            unsafe {
                let _ = Box::from_raw(ptr.as_ptr());
            }
        }
    }
}

impl TerminationController {
    fn new(isolate_handle: v8::IsolateHandle) -> Self {
        Self {
            inner: Arc::new(TerminationState {
                status: AtomicU8::new(TERMINATION_STATUS_RUNNING),
                isolate_handle,
                reason: Mutex::new(None),
                dispatcher_wake: Arc::new(tokio::sync::Notify::new()),
            }),
        }
    }

    /// The dispatcher's wake handle, so `run` can await the same `Notify` that
    /// `request()` signals.
    fn dispatcher_wake(&self) -> Arc<tokio::sync::Notify> {
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
        // Wake the dispatcher whether or not this was the first request: a
        // repeat request still wants the flag observed promptly, and a spurious
        // wake only costs one extra loop iteration.
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

    /// Mark the isolate terminated from the *host* side, without the runtime
    /// thread having acknowledged anything.
    ///
    /// Used only by the force-kill escalation in `RuntimeHandle::recv_result`,
    /// where the runtime thread is wedged and will never mark itself. Normal
    /// termination goes through the runtime thread's own
    /// `finalize_termination` -> `mark_terminated`.
    pub fn force_mark_terminated(&self) {
        self.mark_terminated();
    }

    fn mark_terminated(&self) -> bool {
        self.inner
            .status
            .swap(TERMINATION_STATUS_TERMINATED, Ordering::SeqCst)
            != TERMINATION_STATUS_TERMINATED
    }
}

/// Commands sent from the handle to the runtime thread.
///
/// Each command includes a responder channel for returning results or errors.
/// Commands are processed sequentially on the runtime thread.
pub enum RuntimeCommand {
    Eval {
        code: String,
        responder: Sender<RuntimeResult<JSValue>>,
    },
    EvalAsync {
        code: String,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
    },
    EvalModule {
        specifier: String,
        responder: Sender<RuntimeResult<JSValue>>,
    },
    EvalModuleAsync {
        specifier: String,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
    },
    RegisterPythonOp {
        name: String,
        mode: PythonOpMode,
        handler: Py<PyAny>,
        responder: Sender<RuntimeResult<OpToken>>,
    },
    SetPythonOpExposure {
        op_id: OpToken,
        exposed: bool,
        responder: Sender<RuntimeResult<bool>>,
    },
    SetModuleResolver {
        handler: Py<PyAny>,
        responder: Sender<RuntimeResult<()>>,
    },
    SetModuleLoader {
        handler: Py<PyAny>,
        responder: Sender<RuntimeResult<()>>,
    },
    AddStaticModule {
        name: String,
        source: String,
        responder: Sender<RuntimeResult<()>>,
    },
    BindObject {
        name: String,
        properties: Vec<BoundObjectProperty>,
        responder: Sender<RuntimeResult<()>>,
    },
    CallFunctionSync {
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
        responder: Sender<RuntimeResult<FunctionCallResult>>,
    },
    CallFunctionAsync {
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
    },
    ResumeFunctionCall {
        call_id: u64,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
    },
    ReleaseFunction {
        fn_id: u32,
        responder: oneshot::Sender<RuntimeResult<()>>,
    },
    StreamRead {
        stream_id: u32,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
    },
    StreamRelease {
        stream_id: u32,
        responder: Sender<RuntimeResult<()>>,
    },
    StreamCancel {
        stream_id: u32,
        responder: Sender<RuntimeResult<()>>,
    },
    GetStats {
        responder: Sender<RuntimeResult<RuntimeStatsSnapshot>>,
    },
    Terminate {
        responder: Sender<RuntimeResult<()>>,
    },
    Shutdown {
        responder: Sender<()>,
    },
}

/// Safety-net poll interval used *only* while async JavaScript work is in
/// flight, and never while the runtime is idle.
///
/// Nearly every wake the dispatcher needs is already exact: `DispatcherWaker`
/// covers everything `deno_core` signals through the waker it is handed (a
/// completing async op, a resolved promise, an armed timer), a command
/// arriving wakes `cmd_rx.recv()`, and an active job's timeout is waited on to
/// its precise deadline by `pending_work_wait`.
///
/// This bound is the backstop for progress that is *not* signalled through the
/// waker. It is deliberately a safety net rather than a mechanism anything
/// depends on: the alternative to a bounded wait is an unbounded one, and an
/// unbounded wait turns any unsignalled wake into a permanent hang, where the
/// old busy-spin would merely have burned CPU and recovered. 1ms costs ~0.1%
/// of a core and only applies for the bounded stretch during which async work
/// is genuinely outstanding.
///
/// Nothing depends on it for termination any more. The `2b.` block in `run`
/// reads the termination flag directly, because `terminate_execution()` cannot
/// reach a runtime parked on a pending promise -- it only trips when V8 next
/// enters JavaScript, and a drained-but-pending event loop never does. That
/// check used to be reached only on this tick, which quietly made the tick
/// load-bearing: raising it failed `tests/test_parked_termination.py` outright.
/// `TerminationController::request` now signals the waker, so the check is
/// reached on the next loop iteration (~0.3ms median, down from ~1.4-1.9ms)
/// and this tick is free to be a backstop again.
const PENDING_WORK_TICK: Duration = Duration::from_millis(1);

/// Waker handed to `poll_event_loop`, replacing the `noop_waker` this
/// dispatcher used to pass.
///
/// A `noop_waker` discards every wake, which is why the loop previously had to
/// re-poll continuously to notice that async work had progressed -- it had no
/// other way to find out. With a real waker, `deno_core` registers it against
/// its op driver and promise machinery and signals us when there is something
/// to do, which is what lets the loop park instead of spin. This is the same
/// contract `deno_core`'s own `run_event_loop` relies on
/// (`poll_fn(|cx| self.poll_event_loop(cx, opts)).await`).
///
/// `Notify::notify_one` stores a permit when nobody is currently awaiting, so a
/// wake that lands while we are mid-poll is not lost -- it is consumed by the
/// next `notified()`.
struct DispatcherWaker(Arc<tokio::sync::Notify>);

impl futures::task::ArcWake for DispatcherWaker {
    fn wake_by_ref(arc_self: &Arc<Self>) {
        arc_self.0.notify_one();
    }
}

/// Dispatcher that multiplexes command processing with async job execution.
///
/// Runs the main loop on the runtime thread, polling the V8 event loop, processing
/// incoming commands, and driving active async jobs to completion.
struct RuntimeDispatcher {
    /// Core runtime state (V8 isolate, module loader, op registry, etc).
    core: RuntimeCoreState,
    /// Channel for receiving commands from the handle.
    cmd_rx: mpsc::UnboundedReceiver<RuntimeCommand>,
    /// Queue of jobs waiting to execute.
    pending_jobs: std::collections::VecDeque<Box<dyn RuntimeJob>>,
    /// Currently executing job (if any).
    active_job: Option<Box<dyn RuntimeJob>>,
    /// Set whenever a job becomes active; cleared by `run` after it has given
    /// that job one un-waited iteration.
    ///
    /// A freshly activated job has not run yet, so its first `poll` is what
    /// starts the work (`execute_script`, then a transition to a
    /// promise-waiting state) and necessarily returns `Pending`. The promise it
    /// is now waiting on is frequently *already* resolved -- `Promise.resolve`,
    /// or a microtask chain that `poll_event_loop` drains in a single call --
    /// so the job needs one more trip round the loop to observe that and
    /// finish. Nothing signals the waker for this: no op is in flight and no
    /// timer is armed, because the progress already happened. Without this
    /// flag the dispatcher would wait on the safety-net tick for a resolution
    /// that is sitting right there, putting a full tick of latency on every
    /// single async call.
    job_just_activated: bool,
}

impl RuntimeDispatcher {
    fn new(core: RuntimeCoreState, cmd_rx: mpsc::UnboundedReceiver<RuntimeCommand>) -> Self {
        Self {
            core,
            cmd_rx,
            pending_jobs: std::collections::VecDeque::new(),
            active_job: None,
            job_just_activated: false,
        }
    }

    /// How long the dispatcher may wait before re-polling, given the work
    /// currently in flight.
    ///
    /// Bounded by [`PENDING_WORK_TICK`], and clamped down to an active job's
    /// remaining timeout so the job's own deadline check fires on time rather
    /// than up to a tick late. A deadline already in the past yields `ZERO`,
    /// which re-polls immediately and lets the job report its timeout without
    /// delay. The clamp is what keeps a never-resolving promise's `timeout=`
    /// honest now that the loop no longer spins through that check.
    fn pending_work_wait(&self) -> Duration {
        let mut wait = PENDING_WORK_TICK;
        if let Some(deadline) = self.active_job.as_ref().and_then(|job| job.deadline()) {
            wait = wait.min(deadline.saturating_duration_since(Instant::now()));
        }
        wait
    }

    async fn run(&mut self) {
        // One waker for the whole loop. See `DispatcherWaker`: this replaces a
        // `noop_waker`, and is what makes parking possible at all.
        //
        // It is owned by the `TerminationController` so that an off-thread
        // `terminate()` can signal it too -- see `TerminationState`'s
        // `dispatcher_wake`. Both sources mean the same thing to this loop
        // ("something changed, re-poll"), so they share one handle.
        let notify = self.core.termination.dispatcher_wake();
        let waker = futures::task::waker(Arc::new(DispatcherWaker(notify.clone())));

        loop {
            // 1. SYNCHRONOUSLY drive the JavaScript event loop
            // This advances all promises, timers, and async ops one tick
            // Non-blocking - returns immediately even if work is pending
            let mut cx = std::task::Context::from_waker(&waker);
            // deno_core 0.409 dropped `pump_v8_message_loop` (pumping is now
            // unconditional inside poll_event_loop).
            let poll_opts = PollEventLoopOptions {
                wait_for_inspector: false,
            };

            // Whether the event loop reported that it has *nothing* left to do.
            //
            // `poll_event_loop` returning `Ready` is deno_core's statement that
            // no async op is in flight, no promise is awaiting a host result
            // and no timer is armed; `Pending` means at least one of those is
            // outstanding and the waker will be signalled when it progresses.
            // That distinction is the whole basis for deciding between parking
            // and polling below.
            let event_loop_drained;

            // An active job's own watchdog (armed for its whole lifetime, via
            // `effective_timeout_ms` falling back to `execution_timeout` when
            // no per-call timeout is given) already bounds every
            // `poll_event_loop` call made while that job is active. But a
            // script can queue work that outlives the job that queued it --
            // an async IIFE whose *outer* promise resolves (finishing the
            // job and clearing `active_job`) while an inner
            // `queueMicrotask`/promise chain keeps re-queuing itself forever
            // is still live on this isolate, and `poll_event_loop` keeps
            // getting called for it every iteration regardless. With no job
            // present, nothing was arming a deadline around that call, so a
            // runaway of this shape ignored `execution_timeout` entirely and
            // could hang the dispatcher (and therefore the whole runtime)
            // indefinitely. Arming here too, whenever no job holds the
            // deadline, closes that gap; the ~1us cost of an arm/disarm pair
            // added to every idle iteration is what P1's persistent watchdog
            // thread made affordable. See tests/test_dispatcher_step_timeout.py.
            let step_watchdog = if self.active_job.is_none() {
                match self
                    .core
                    .start_sync_watchdog("Event loop step exceeded execution_timeout")
                {
                    Ok(token) => token,
                    Err(err) => {
                        log::warn!("Failed to arm the dispatcher-step watchdog: {err}");
                        None
                    }
                }
            } else {
                None
            };

            // Check for event loop errors
            // Note: Termination errors (from timeout/abort) are expected and will be handled
            // by the job's own poll() method. Only fail the job on unexpected fatal errors.
            let poll_result = self.core.js_runtime.poll_event_loop(&mut cx, poll_opts);

            if let Some(watchdog) = step_watchdog {
                match self.core.resolve_sync_watchdog(watchdog) {
                    Ok((true, duration)) => {
                        log::warn!(
                            "Event loop step exceeded execution_timeout ({}ms) with no job \
                             active -- terminated a runaway async chain (e.g. a \
                             self-requeuing microtask/promise) that outlived the job \
                             that queued it",
                            duration.as_millis()
                        );
                    }
                    Ok((false, _)) => {}
                    Err(err) => {
                        log::warn!("Failed to resolve the dispatcher-step watchdog: {err}");
                    }
                }
            }

            match poll_result {
                std::task::Poll::Ready(Err(err)) => {
                    // An error means the loop is not going to make further
                    // progress on its own, so treat it as drained and let the
                    // job below decide the outcome.
                    event_loop_drained = true;
                    let runtime_err = self.core.translate_core_error(err);

                    // Check if this is a termination-related error (expected during timeout/abort)
                    if RuntimeCoreState::runtime_error_indicates_termination(&runtime_err) {
                        // Termination error - let the job handle it via its own timeout check
                        // Do nothing here, just continue to job polling
                    } else {
                        // Unexpected fatal error - fail the active job immediately
                        let runtime_err_debug = format!("{runtime_err:?}");
                        if let Some(completed_job) = self.active_job.take() {
                            log::error!("Unexpected event loop error: {runtime_err_debug}");
                            let elapsed = completed_job.start_time().elapsed();
                            self.core.stats_state.record(completed_job.kind(), elapsed);
                            completed_job.finish(&mut self.core, Err(runtime_err));
                            self.core.clear_task_locals();
                            if let Some(next_job) = self.pending_jobs.pop_front() {
                                self.job_just_activated = true;
                                self.active_job = Some(next_job);
                            }
                        } else {
                            log::error!(
                                "JavaScript event loop failed without an active job: {runtime_err_debug}"
                            );
                        }
                        continue;
                    }
                }
                std::task::Poll::Ready(Ok(())) => {
                    // Normal - event loop ran everything it had.
                    event_loop_drained = true;
                }
                std::task::Poll::Pending => {
                    // Normal - async work is still outstanding. The waker is
                    // registered, so we will be signalled on progress.
                    event_loop_drained = false;
                }
            }

            // 2. SYNCHRONOUSLY check if the active job is complete
            if let Some(job) = &mut self.active_job {
                match job.poll(&mut self.core) {
                    std::task::Poll::Ready(result) => {
                        // Job completed - record stats and send result
                        let completed_job = self.active_job.take().unwrap();
                        let elapsed = completed_job.start_time().elapsed();
                        self.core.stats_state.record(completed_job.kind(), elapsed);
                        completed_job.finish(&mut self.core, result);

                        // Clear task locals to prevent stale event loop references
                        self.core.clear_task_locals();

                        // Start the next pending job if any
                        if let Some(next_job) = self.pending_jobs.pop_front() {
                            self.job_just_activated = true;
                            self.active_job = Some(next_job);
                        }
                    }
                    std::task::Poll::Pending => {
                        // Job still running - continue
                    }
                }
            }

            // 2b. Honour a termination request raised from another thread.
            //
            // `TerminationHandle::terminate()` is the only kill switch callable
            // from off-thread (it wraps the `Send` `v8::IsolateHandle`), and all
            // it can do is flip the shared status flag and call V8's
            // `terminate_execution()`. That second half is a no-op against a
            // runtime parked on a *pending promise*: `terminate_execution()`
            // only trips when V8 next **enters** JavaScript, and a
            // drained-but-pending event loop never re-enters. Nothing else in
            // this loop consulted the flag, so the request was simply never
            // observed and the caller blocked on its result channel forever --
            // `new Promise(() => {})` was unkillable.
            //
            // Checking the flag here closes that hole without any new timer,
            // and costs one atomic load per iteration. `request()` signals the
            // dispatcher's waker, so this is reached on the very next iteration
            // (~0.3ms median; see BENCHMARKS.md) rather than on the next
            // `PENDING_WORK_TICK` -- which is what keeps the tick a backstop
            // instead of the mechanism this depends on.
            //
            // This is the *polite* tier and deliberately does not recreate
            // anything: bound host functions, module state and the isolate are
            // left to the normal termination path, exactly as a `while(true){}`
            // kill already behaved. It matches `RuntimeCommand::Terminate`
            // (cancel the work, finalize, exit) because a termination request
            // already makes `should_reject_new_work()` true, so the runtime
            // would reject every later command anyway.
            //
            // It cannot help when the dispatcher *thread itself* is wedged
            // inside a host call that never returns (a blocking sync op), since
            // then this line never runs. That case is what the bounded
            // escalation in `RuntimeHandle` exists for -- see
            // `FORCE_KILL_GRACE`.
            if self.core.should_reject_new_work() && self.has_work() {
                log::debug!("Observed off-thread termination request; aborting in-flight work");
                let termination_error = self.core.terminated_error();
                self.cancel_all_jobs(termination_error);
                if let Err(err) = self.core.finalize_termination() {
                    log::warn!("Failed to finalize termination after abort: {err}");
                }
                break;
            }

            // 3. ASYNCHRONOUSLY wait for something to actually happen.
            //
            // This step used to `select!` between `cmd_rx.recv()` and
            // `tokio::task::yield_now()`. Because `yield_now` is always
            // immediately ready, the loop never blocked: every `Runtime` burned
            // CPU continuously for its entire lifetime whether or not it had
            // any work, and K retained runtimes cost K times that. Retaining a
            // warm runtime per session -- the pattern this library is fastest
            // at -- was therefore capped by core count rather than by memory.
            //
            // Now the loop distinguishes two states and blocks in both:
            //
            //   idle    -- the event loop is drained and no job is queued or
            //              active, so nothing can happen until a command
            //              arrives. Park on `recv()` with no timer at all.
            //   pending -- async work is outstanding. Wait on the waker (which
            //              fires when it progresses), on a command, or on the
            //              bounded tick from `pending_work_wait()`.
            //
            // A newly activated job gets one immediate, un-waited iteration so
            // it can observe an already-resolved promise -- see
            // `job_just_activated`. At most one extra iteration per activation,
            // so this cannot spin.
            if std::mem::take(&mut self.job_just_activated) {
                continue;
            }

            // Both arms are real awaits, so an idle runtime costs no CPU.
            let idle =
                event_loop_drained && self.active_job.is_none() && self.pending_jobs.is_empty();

            let should_exit = if idle {
                tokio::select! {
                    biased; // Prefer new commands

                    cmd = self.cmd_rx.recv() => {
                        match cmd {
                            Some(cmd) => self.handle_command(cmd),
                            None => self.handle_channel_closed(),
                        }
                    }

                    // A wake that landed while we were polling. Nothing should
                    // be able to signal this once the loop is drained, but
                    // honouring it costs one extra iteration and removes any
                    // chance of sleeping through work, which is the failure
                    // mode worth spending an arm on.
                    _ = notify.notified() => false,
                }
            } else {
                let wait = self.pending_work_wait();
                tokio::select! {
                    biased; // Prefer new commands

                    cmd = self.cmd_rx.recv() => {
                        match cmd {
                            Some(cmd) => self.handle_command(cmd),
                            None => self.handle_channel_closed(),
                        }
                    }

                    // deno_core made progress on the outstanding work.
                    _ = notify.notified() => false,

                    // Job deadline, or the safety net -- see `PENDING_WORK_TICK`.
                    _ = tokio::time::sleep(wait) => false,
                }
            };

            if should_exit {
                break;
            }
        }
    }

    /// Activate `job` if the runtime is free, otherwise queue it behind the
    /// job that is running.
    ///
    /// Activation sets `job_just_activated`, which is what buys a new job its
    /// one un-waited iteration; queueing must not, because the queued job does
    /// not run until the active one finishes (and that path sets the flag
    /// itself).
    fn submit_job(&mut self, job: Box<dyn RuntimeJob>) {
        if self.active_job.is_none() {
            self.job_just_activated = true;
            self.active_job = Some(job);
        } else {
            self.pending_jobs.push_back(job);
        }
    }

    /// Whether any job is active or queued.
    fn has_work(&self) -> bool {
        self.active_job.is_some() || !self.pending_jobs.is_empty()
    }

    /// Fail the active job and every queued job with `error`.
    ///
    /// Shared by the three paths that abandon in-flight work: an explicit
    /// `Terminate` command, an off-thread termination request observed by
    /// `run`, and the command channel closing unexpectedly.
    fn cancel_all_jobs(&mut self, error: RuntimeError) {
        if let Some(job) = self.active_job.take() {
            log::debug!("Cancelling active job");
            job.finish(&mut self.core, Err(error.clone()));
        }

        let pending_count = self.pending_jobs.len();
        if pending_count > 0 {
            log::debug!("Cancelling {pending_count} pending jobs");
        }
        while let Some(job) = self.pending_jobs.pop_front() {
            job.finish(&mut self.core, Err(error.clone()));
        }

        // Clear task locals to prevent stale event loop references
        self.core.clear_task_locals();
    }

    /// Handle a command - returns true if dispatcher should exit
    fn handle_command(&mut self, cmd: RuntimeCommand) -> bool {
        match cmd {
            RuntimeCommand::Eval { code, responder } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else if let Err(err) = self.core.ensure_inspector_ready() {
                    Err(err)
                } else {
                    match self
                        .core
                        .start_sync_watchdog("Synchronous evaluation timed out")
                    {
                        Ok(watchdog) => {
                            let result = self.core.eval_sync(&code);
                            self.core
                                .apply_watchdog_result(result, watchdog, "Sync evaluation")
                        }
                        Err(err) => Err(err),
                    }
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::EvalAsync {
                code,
                timeout_ms,
                task_locals,
                responder,
            } => {
                if self.core.should_reject_new_work() {
                    let _ = responder.send(Err(self.core.terminated_error()));
                    return false;
                }
                if let Err(err) = self.core.ensure_inspector_ready() {
                    let _ = responder.send(Err(err));
                    return false;
                }

                // Determine effective timeout and spawn watchdog if needed
                let effective_timeout = self.core.effective_timeout_ms(timeout_ms);
                let watchdog = match self
                    .core
                    .start_timeout_watchdog(effective_timeout, "Asynchronous evaluation timed out")
                {
                    Ok(watchdog) => watchdog,
                    Err(err) => {
                        let _ = responder.send(Err(err));
                        return false;
                    }
                };

                self.submit_job(Box::new(eval_async_job(
                    code.clone(),
                    effective_timeout,
                    task_locals,
                    responder,
                    watchdog,
                )));
                false
            }
            RuntimeCommand::RegisterPythonOp {
                name,
                mode,
                handler,
                responder,
            } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    self.core.register_python_op(name, mode, handler)
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::SetPythonOpExposure {
                op_id,
                exposed,
                responder,
            } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    Ok(self.core.set_python_op_exposure(op_id, exposed))
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::SetModuleResolver { handler, responder } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    self.core.module_loader.set_resolver(handler);
                    if let Some(ref locals) = self.core.task_locals {
                        self.core.module_loader.set_task_locals(locals.clone());
                    }
                    Ok(())
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::SetModuleLoader { handler, responder } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    self.core.module_loader.set_loader(handler);
                    if let Some(ref locals) = self.core.task_locals {
                        self.core.module_loader.set_task_locals(locals.clone());
                    }
                    Ok(())
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::AddStaticModule {
                name,
                source,
                responder,
            } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    self.core.module_loader.add_static_module(name, source);
                    Ok(())
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::BindObject {
                name,
                properties,
                responder,
            } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    self.core.bind_object(name, properties)
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::EvalModule {
                specifier,
                responder,
            } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else if let Err(err) = self.core.ensure_inspector_ready() {
                    Err(err)
                } else {
                    match self
                        .core
                        .start_sync_watchdog("Synchronous module evaluation timed out")
                    {
                        Ok(watchdog) => {
                            let result = self.core.eval_module_sync(&specifier);
                            self.core.apply_watchdog_result(
                                result,
                                watchdog,
                                "Sync module evaluation",
                            )
                        }
                        Err(err) => Err(err),
                    }
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::EvalModuleAsync {
                specifier,
                timeout_ms,
                task_locals,
                responder,
            } => {
                if self.core.should_reject_new_work() {
                    let _ = responder.send(Err(self.core.terminated_error()));
                    return false;
                }
                if let Err(err) = self.core.ensure_inspector_ready() {
                    let _ = responder.send(Err(err));
                    return false;
                }

                // Determine effective timeout and spawn watchdog if needed
                let effective_timeout = self.core.effective_timeout_ms(timeout_ms);
                let watchdog = match self.core.start_timeout_watchdog(
                    effective_timeout,
                    "Asynchronous module evaluation timed out",
                ) {
                    Ok(watchdog) => watchdog,
                    Err(err) => {
                        let _ = responder.send(Err(err));
                        return false;
                    }
                };

                self.submit_job(Box::new(EvalModuleAsyncJob::new(
                    specifier,
                    effective_timeout,
                    task_locals,
                    responder,
                    watchdog,
                )));
                false
            }
            RuntimeCommand::CallFunctionSync {
                fn_id,
                args,
                timeout_ms,
                responder,
            } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else if let Err(err) = self.core.ensure_inspector_ready() {
                    Err(err)
                } else {
                    match self
                        .core
                        .start_sync_watchdog("Synchronous function call timed out")
                    {
                        Ok(watchdog) => {
                            let result = self.core.call_function_sync(fn_id, args, timeout_ms);
                            self.core
                                .apply_watchdog_result(result, watchdog, "Sync function call")
                        }
                        Err(err) => Err(err),
                    }
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::CallFunctionAsync {
                fn_id,
                args,
                timeout_ms,
                task_locals,
                responder,
            } => {
                if self.core.should_reject_new_work() {
                    let _ = responder.send(Err(self.core.terminated_error()));
                    return false;
                }
                if let Err(err) = self.core.ensure_inspector_ready() {
                    let _ = responder.send(Err(err));
                    return false;
                }

                let job = call_function_async_job(
                    fn_id,
                    args,
                    timeout_ms,
                    task_locals,
                    responder,
                    &self.core,
                );
                self.submit_job(Box::new(job));
                false
            }
            RuntimeCommand::ResumeFunctionCall {
                call_id,
                task_locals,
                responder,
            } => {
                if self.core.should_reject_new_work() {
                    let _ = responder.send(Err(self.core.terminated_error()));
                    return false;
                }
                if let Err(err) = self.core.ensure_inspector_ready() {
                    let _ = responder.send(Err(err));
                    return false;
                }

                let pending = match self.core.take_pending_call(call_id) {
                    Ok(pending) => pending,
                    Err(err) => {
                        let _ = responder.send(Err(err));
                        return false;
                    }
                };

                let job = resume_function_call_job(pending, task_locals, responder);
                self.submit_job(Box::new(job));
                false
            }
            RuntimeCommand::ReleaseFunction { fn_id, responder } => {
                let result = if self.core.should_reject_new_work() {
                    Err(self.core.terminated_error())
                } else {
                    self.core.release_function(fn_id)
                };
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::StreamRead {
                stream_id,
                responder,
            } => {
                if self.core.should_reject_new_work() {
                    let _ = responder.send(Err(self.core.terminated_error()));
                } else {
                    self.submit_job(Box::new(stream_read_job(stream_id, responder)));
                }
                false
            }
            RuntimeCommand::StreamRelease {
                stream_id,
                responder,
            } => {
                let result = self.core.release_js_stream(stream_id);
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::StreamCancel {
                stream_id,
                responder,
            } => {
                let result = self.core.cancel_js_stream(stream_id);
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::GetStats { responder } => {
                let result = self.core.collect_stats();
                let _ = responder.send(result);
                false
            }
            RuntimeCommand::Terminate { responder } => {
                let termination_error = self.core.terminated_error();
                self.cancel_all_jobs(termination_error);

                let result = self.core.finalize_termination();
                let _ = responder.send(result);
                self.cmd_rx.close();
                true // Exit the loop
            }
            RuntimeCommand::Shutdown { responder } => {
                let leaked_count = self.core.fn_registry.borrow().len();
                if leaked_count > 0 {
                    log::warn!(
                        "Function handles not released before shutdown: {leaked_count} leaked"
                    );
                }
                self.core.fn_registry.borrow_mut().clear();

                // Clear task locals on shutdown
                self.core.clear_task_locals();

                let _ = responder.send(());
                self.cmd_rx.close();
                true // Exit the loop
            }
        }
    }

    /// Handle the command channel closing without an explicit shutdown request.
    fn handle_channel_closed(&mut self) -> bool {
        log::warn!("Command channel closed without explicit shutdown - cleaning up");
        self.core
            .termination
            .ensure_reason("Command channel closed unexpectedly");
        let termination_error = self.core.terminated_error();
        self.cancel_all_jobs(termination_error);
        if let Err(err) = self.core.finalize_termination() {
            log::warn!(
                "Failed to finalize termination after command channel closed: {}",
                err
            );
        }
        true
    }
}

/// Trait for async runtime jobs that can be polled without holding long-term borrows.
/// Jobs are state machines that advance one step at a time.
trait RuntimeJob {
    /// Returns the kind of runtime call for stats tracking
    fn kind(&self) -> RuntimeCallKind;

    /// Poll the job for one tick. Returns Poll::Ready when complete.
    /// The job can borrow core mutably but must release it before returning.
    fn poll(&mut self, core: &mut RuntimeCoreState) -> std::task::Poll<RuntimeResult<JSValue>>;

    /// Finalize the job with a result, allowing cleanup before responding.
    fn finish(self: Box<Self>, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>);

    /// Get the start time for stats tracking
    fn start_time(&self) -> Instant;

    /// Absolute deadline after which this job's own `poll` will time out, if it
    /// has one.
    ///
    /// The dispatcher needs this because every job enforces its timeout from
    /// inside `poll` -- so a dispatcher that parked indefinitely while a job
    /// was in flight would never reach that check and the timeout would never
    /// fire. Exposing the deadline lets the wait be bounded by it exactly.
    fn deadline(&self) -> Option<Instant> {
        None
    }
}

/// The two strings a job's timeout produces, kept per job so the shared
/// deadline check below can reproduce each message verbatim.
///
/// `reason` lands on the termination controller (it is what
/// `TerminationHandle` reports afterwards) and `error` + `error_suffix` become
/// the `RuntimeError::timeout` the caller sees. They differ in wording only,
/// which is the entire reason the five `poll` preambles used to be five.
#[derive(Clone, Copy)]
struct TimeoutWording {
    reason: &'static str,
    error: &'static str,
    error_suffix: &'static str,
}

impl TimeoutWording {
    const EVAL: Self = Self {
        reason: "Asynchronous evaluation",
        error: "Evaluation",
        error_suffix: " (promise still pending)",
    };
    const EVAL_MODULE: Self = Self {
        reason: "Asynchronous module evaluation",
        error: "Module evaluation",
        error_suffix: "",
    };
    const CALL_FUNCTION: Self = Self {
        reason: "Asynchronous function call",
        error: "Function call",
        error_suffix: "",
    };
    /// A job with no deadline never formats either string.
    const NONE: Self = Self {
        reason: "",
        error: "",
        error_suffix: "",
    };
}

/// The state every async job carries, and the two steps every `poll` opens
/// with.
///
/// Before this existed each job repeated the same field clump, the same
/// eight-line deadline check and the same verbatim task-locals installation.
/// Both steps are load-bearing and neither may be skipped: the deadline check
/// inside `poll` is what makes `timeout=` honest for a promise that never
/// resolves (the dispatcher only bounds its park by `deadline()`; it does not
/// enforce the timeout itself), and the task locals are what let a host op
/// re-enter the caller's Python event loop.
struct JobCommon {
    timeout_ms: Option<u64>,
    task_locals: Option<TaskLocals>,
    responder: oneshot::Sender<RuntimeResult<JSValue>>,
    start_time: Instant,
    deadline: Option<Instant>,
    watchdog: Option<WatchdogToken>,
    kind: RuntimeCallKind,
    wording: TimeoutWording,
    /// Context string for `apply_watchdog_result`, used only when a watchdog
    /// is armed.
    watchdog_context: &'static str,
    /// Set by `expired` when *it* asked V8 to terminate, so `respond` knows it
    /// owns that request and has to clear it. See `expired`.
    terminated_by_deadline: bool,
}

impl JobCommon {
    /// A job whose deadline is derived from `timeout_ms` at construction time.
    fn new(
        kind: RuntimeCallKind,
        wording: TimeoutWording,
        watchdog_context: &'static str,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
        watchdog: Option<WatchdogToken>,
    ) -> Self {
        let start_time = Instant::now();
        let deadline = timeout_ms.map(|ms| start_time + Duration::from_millis(ms));
        Self {
            timeout_ms,
            task_locals,
            responder,
            start_time,
            deadline,
            watchdog,
            kind,
            wording,
            watchdog_context,
            terminated_by_deadline: false,
        }
    }

    /// A job that inherits an already-running call's clock, rather than
    /// starting one. Used when a synchronous call turns out to have returned a
    /// promise and is resumed as an async job: restarting the deadline there
    /// would silently double the caller's timeout.
    fn resumed(
        kind: RuntimeCallKind,
        wording: TimeoutWording,
        watchdog_context: &'static str,
        pending: &PendingFunctionCall,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
    ) -> Self {
        Self {
            timeout_ms: pending.timeout_ms,
            task_locals,
            responder,
            start_time: pending.start_time,
            deadline: pending.deadline,
            watchdog: None,
            kind,
            wording,
            watchdog_context,
            terminated_by_deadline: false,
        }
    }

    /// Returns the error to fail the job with if its deadline has passed,
    /// having first asked V8 to stop executing.
    ///
    /// The termination requested here is *this job's*, and it has to be
    /// cleared again in `respond` -- see `terminated_by_deadline`. It cannot be
    /// left to `apply_watchdog_result`: that only cancels when the job's own
    /// watchdog token comes back `fired`, and this check routinely wins the
    /// race against the watchdog thread (both wake on the same deadline, and
    /// the dispatcher parks until exactly that instant). When it does, `disarm`
    /// returns `false`, nothing cancels, and a job that timed out on a pending
    /// promise leaves the isolate latched -- every later call on the runtime
    /// then fails with a bare `execution terminated`.
    fn expired(&mut self, core: &mut RuntimeCoreState) -> Option<RuntimeError> {
        let deadline = self.deadline?;
        if Instant::now() < deadline {
            return None;
        }
        let ms = self.timeout_ms.unwrap_or(0);
        core.termination
            .ensure_reason(format!("{} timed out after {}ms", self.wording.reason, ms));
        core.termination.terminate_execution();
        self.terminated_by_deadline = true;
        Some(RuntimeError::timeout(format!(
            "{} timed out after {}ms{}",
            self.wording.error, ms, self.wording.error_suffix
        )))
    }

    /// Publish the caller's Python task locals to everything on this thread
    /// that can re-enter them.
    fn install_task_locals(&self, core: &mut RuntimeCoreState) {
        if let Some(ref locals) = self.task_locals {
            core.task_locals = Some(locals.clone());
            core.module_loader.set_task_locals(locals.clone());
            core.js_runtime
                .op_state()
                .borrow_mut()
                .put(crate::runtime::ops::GlobalTaskLocals(Some(locals.clone())));
        }
    }

    /// Resolve the watchdog (a no-op when none is armed) and answer the caller.
    fn respond(mut self, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>) {
        let result =
            core.apply_watchdog_result(result, self.watchdog.take(), self.watchdog_context);
        // The job is over, so whatever `expired` latched has nothing left to
        // stop: clear it, or it stops the *next* call instead. This is the
        // async counterpart of what `resolve_sync_watchdog` does for a sync
        // timeout, and it is safe for the same reason -- one isolate on one
        // thread, so no other job can be executing while this runs.
        if self.terminated_by_deadline {
            core.cancel_pending_termination();
        }
        let _ = self.responder.send(result);
    }
}

/// Produces the promise a [`PromiseJob`] then waits on. Runs once, on the
/// runtime thread, during the job's first `poll`.
type PromiseStart =
    Box<dyn FnOnce(&mut RuntimeCoreState) -> RuntimeResult<v8::Global<v8::Promise>>>;

/// Optional post-processing for a fulfilled promise's already-deserialized
/// value, for the one job (stream reads) that has bookkeeping to do.
type FulfilledHook = Box<dyn FnOnce(&mut RuntimeCoreState, JSValue) -> RuntimeResult<JSValue>>;

/// The one async-job state machine: start something that yields a JS promise,
/// then wait for that promise while the dispatcher drives the event loop.
///
/// Async evaluation, stream reads, async function calls and resumed function
/// calls are all this machine; they differ only in the closure that produces
/// the promise (and, for stream reads, in what happens to the fulfilled
/// value). Module evaluation is genuinely a different shape -- it waits on a
/// Rust future and then extracts a namespace, with no promise anywhere -- so
/// it stays its own state machine below and shares only [`JobCommon`].
struct PromiseJob {
    common: JobCommon,
    start: Option<PromiseStart>,
    on_fulfilled: Option<FulfilledHook>,
    state: PromiseJobState,
}

enum PromiseJobState {
    /// Nothing started yet; the next poll runs `start`.
    Init,
    /// Waiting for the promise to settle. The dispatcher drives it via
    /// `poll_event_loop`.
    Waiting { promise: v8::Global<v8::Promise> },
    /// Settled and reported.
    Done,
}

impl PromiseJob {
    fn new(common: JobCommon, start: PromiseStart) -> Self {
        Self {
            common,
            start: Some(start),
            on_fulfilled: None,
            state: PromiseJobState::Init,
        }
    }

    fn with_fulfilled_hook(mut self, hook: FulfilledHook) -> Self {
        self.on_fulfilled = Some(hook);
        self
    }

    /// Wrap `value` in a promise unless it already is one.
    ///
    /// `resolve()` on a fresh resolver is how a plain return value joins the
    /// same waiting path as a real promise, so there is exactly one path to
    /// maintain.
    fn as_promise<'s>(
        scope: &v8::PinScope<'s, '_>,
        value: v8::Local<'s, v8::Value>,
    ) -> RuntimeResult<v8::Local<'s, v8::Promise>> {
        if value.is_promise() {
            return v8::Local::<v8::Promise>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Promise"));
        }
        let resolver = v8::PromiseResolver::new(scope)
            .ok_or_else(|| RuntimeError::internal("Failed to create PromiseResolver"))?;
        resolver.resolve(scope, value);
        Ok(resolver.get_promise(scope))
    }
}

impl RuntimeJob for PromiseJob {
    fn kind(&self) -> RuntimeCallKind {
        self.common.kind
    }

    fn poll(&mut self, core: &mut RuntimeCoreState) -> std::task::Poll<RuntimeResult<JSValue>> {
        use std::task::Poll;

        if let Some(err) = self.common.expired(core) {
            return Poll::Ready(Err(err));
        }

        match &mut self.state {
            PromiseJobState::Init => {
                self.common.install_task_locals(core);

                let start = match self.start.take() {
                    Some(start) => start,
                    None => {
                        self.state = PromiseJobState::Done;
                        return Poll::Ready(Err(RuntimeError::internal(
                            "Job started more than once",
                        )));
                    }
                };

                match start(core) {
                    Ok(promise) => {
                        self.state = PromiseJobState::Waiting { promise };
                        // The dispatcher drives the event loop from here; it
                        // gives a freshly activated job one un-waited
                        // iteration, so an already-resolved promise costs no
                        // extra latency.
                        Poll::Pending
                    }
                    Err(err) => {
                        self.state = PromiseJobState::Done;
                        Poll::Ready(Err(err))
                    }
                }
            }
            PromiseJobState::Waiting { promise } => {
                let promise_state = {
                    deno_core::scope!(scope, core.js_runtime);
                    let promise_local: v8::Local<v8::Promise> = v8::Local::new(scope, &*promise);
                    promise_local.state()
                };

                match promise_state {
                    v8::PromiseState::Pending => Poll::Pending,
                    v8::PromiseState::Fulfilled => {
                        let value = {
                            let fn_registry = core.fn_registry.clone();
                            let next_fn_id = core.next_fn_id.clone();
                            let limits = core.serialization_limits;
                            let stream_registry = core.js_stream_registry.clone();
                            deno_core::scope!(scope, core.js_runtime);
                            let promise_local: v8::Local<v8::Promise> =
                                v8::Local::new(scope, &*promise);
                            let result_value = promise_local.result(scope);
                            RuntimeCoreState::value_to_js_value(
                                &fn_registry,
                                &next_fn_id,
                                scope,
                                result_value,
                                limits,
                                stream_registry,
                            )
                        };
                        self.state = PromiseJobState::Done;
                        Poll::Ready(match (value, self.on_fulfilled.take()) {
                            (Ok(value), Some(hook)) => hook(core, value),
                            (other, _) => other,
                        })
                    }
                    v8::PromiseState::Rejected => {
                        let js_error = {
                            deno_core::scope!(scope, core.js_runtime);
                            let promise_local: v8::Local<v8::Promise> =
                                v8::Local::new(scope, &*promise);
                            let exception = promise_local.result(scope);
                            *JsError::from_v8_exception(scope, exception)
                        };
                        // Scope dropped, so `core` can be borrowed again.
                        let error = core.translate_js_error(js_error);
                        self.state = PromiseJobState::Done;
                        Poll::Ready(Err(error))
                    }
                }
            }
            PromiseJobState::Done => {
                Poll::Ready(Err(RuntimeError::internal("Job already completed")))
            }
        }
    }

    fn finish(self: Box<Self>, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>) {
        self.common.respond(core, result);
    }

    fn start_time(&self) -> Instant {
        self.common.start_time
    }

    fn deadline(&self) -> Option<Instant> {
        self.common.deadline
    }
}

/// Async evaluation: run the script, then await whatever it produced.
fn eval_async_job(
    code: String,
    timeout_ms: Option<u64>,
    task_locals: Option<TaskLocals>,
    responder: oneshot::Sender<RuntimeResult<JSValue>>,
    watchdog: Option<WatchdogToken>,
) -> PromiseJob {
    let common = JobCommon::new(
        RuntimeCallKind::EvalAsync,
        TimeoutWording::EVAL,
        "Async evaluation",
        timeout_ms,
        task_locals,
        responder,
        watchdog,
    );

    PromiseJob::new(
        common,
        Box::new(move |core: &mut RuntimeCoreState| {
            let global_value = core
                .js_runtime
                .execute_script("<eval_async>", code)
                .map_err(|err| core.translate_js_error(*err))?;

            deno_core::scope!(scope, core.js_runtime);
            let local_value = v8::Local::new(scope, global_value);
            let promise = PromiseJob::as_promise(scope, local_value)?;
            Ok(v8::Global::new(scope, promise))
        }),
    )
}

/// One read from a JS `ReadableStream`, with the chunk bookkeeping the stream
/// registry needs once the read resolves.
fn stream_read_job(
    stream_id: u32,
    responder: oneshot::Sender<RuntimeResult<JSValue>>,
) -> PromiseJob {
    let common = JobCommon::new(
        RuntimeCallKind::EvalAsync,
        TimeoutWording::NONE,
        "Stream read",
        None,
        None,
        responder,
        None,
    );

    PromiseJob::new(
        common,
        Box::new(move |core: &mut RuntimeCoreState| {
            deno_core::scope!(scope, core.js_runtime);
            core.js_stream_registry.start_read(scope, stream_id)
        }),
    )
    .with_fulfilled_hook(Box::new(
        move |core: &mut RuntimeCoreState, value: JSValue| {
            let chunk = StreamChunk::from_js_value(value)?;
            core.js_stream_registry
                .update_stats_after_chunk(stream_id, &chunk);
            if chunk.done {
                core.js_stream_registry.release(stream_id);
            }
            Ok(chunk.to_js_value())
        },
    ))
}

/// Async module evaluation.
///
/// Unlike every other async job this one has no promise: `mod_evaluate`
/// returns a Rust future, and the module namespace is only reachable once that
/// future is ready. It shares [`JobCommon`] (and so the deadline check, the
/// task-locals installation and the watchdog handling) and nothing else.
struct EvalModuleAsyncJob {
    specifier: String,
    common: JobCommon,
    state: EvalModuleAsyncJobState,
}

enum EvalModuleAsyncJobState {
    /// Initial state - need to load module and start evaluation
    Init,
    /// Module loaded, evaluation in progress (polling receiver)
    Evaluating {
        module_id: deno_core::ModuleId,
        receiver: std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), CoreError>>>>,
    },
    /// Evaluation complete, ready to extract namespace
    WaitingNamespace { module_id: deno_core::ModuleId },
    /// Done
    Done,
}

impl EvalModuleAsyncJob {
    fn new(
        specifier: String,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: oneshot::Sender<RuntimeResult<JSValue>>,
        watchdog: Option<WatchdogToken>,
    ) -> Self {
        Self {
            specifier,
            common: JobCommon::new(
                RuntimeCallKind::EvalModuleAsync,
                TimeoutWording::EVAL_MODULE,
                "Async module evaluation",
                timeout_ms,
                task_locals,
                responder,
                watchdog,
            ),
            state: EvalModuleAsyncJobState::Init,
        }
    }
}

impl RuntimeJob for EvalModuleAsyncJob {
    fn kind(&self) -> RuntimeCallKind {
        self.common.kind
    }

    fn poll(&mut self, core: &mut RuntimeCoreState) -> std::task::Poll<RuntimeResult<JSValue>> {
        use std::task::Poll;

        if let Some(err) = self.common.expired(core) {
            return Poll::Ready(Err(err));
        }

        match &mut self.state {
            EvalModuleAsyncJobState::Init => {
                self.common.install_task_locals(core);

                // Parse module specifier
                let module_specifier =
                    if self.specifier.contains(':') || self.specifier.starts_with('/') {
                        deno_core::ModuleSpecifier::parse(&self.specifier).map_err(|e| {
                            RuntimeError::internal(format!(
                                "Invalid module specifier '{}': {}",
                                self.specifier, e
                            ))
                        })?
                    } else {
                        let base = deno_core::ModuleSpecifier::parse("pydeno://runtime/").map_err(
                            |e| RuntimeError::internal(format!("Failed to create base URL: {}", e)),
                        )?;
                        base.join(&self.specifier).map_err(|e| {
                            RuntimeError::internal(format!(
                                "Failed to resolve module specifier '{}': {}",
                                self.specifier, e
                            ))
                        })?
                    };

                // Load module synchronously (module loading is inherently blocking in deno_core)
                // This is consistent with eval_module_sync and doesn't prevent re-entrance
                // because the actual async work (promise resolution) happens in the Evaluating state
                let module_id = futures::executor::block_on(
                    core.js_runtime.load_main_es_module(&module_specifier),
                )
                .map_err(|e| {
                    RuntimeError::internal(format!(
                        "Failed to load module '{}': {}",
                        self.specifier, e
                    ))
                })?;

                // Start evaluation - this returns a future that we'll poll
                let receiver = Box::pin(core.js_runtime.mod_evaluate(module_id));

                self.state = EvalModuleAsyncJobState::Evaluating {
                    module_id,
                    receiver,
                };
                Poll::Pending
            }
            EvalModuleAsyncJobState::Evaluating {
                module_id,
                receiver,
            } => {
                // The dispatcher is driving poll_event_loop which will progress the module evaluation
                // We need to poll the receiver to see if it's done
                let noop_waker = futures::task::noop_waker();
                let mut cx = std::task::Context::from_waker(&noop_waker);

                match receiver.as_mut().poll(&mut cx) {
                    Poll::Ready(result) => {
                        // Evaluation complete - check result
                        if let Err(err) = result {
                            self.state = EvalModuleAsyncJobState::Done;
                            return Poll::Ready(Err(core.translate_core_error(err)));
                        }

                        // Success - transition to namespace extraction
                        self.state = EvalModuleAsyncJobState::WaitingNamespace {
                            module_id: *module_id,
                        };
                        Poll::Pending
                    }
                    Poll::Pending => Poll::Pending,
                }
            }
            EvalModuleAsyncJobState::WaitingNamespace { module_id } => {
                // Extract module namespace
                let fn_registry = core.fn_registry.clone();
                let next_fn_id = core.next_fn_id.clone();
                let limits = core.serialization_limits;
                let module_namespace =
                    core.js_runtime
                        .get_module_namespace(*module_id)
                        .map_err(|e| {
                            RuntimeError::internal(format!("Failed to get module namespace: {}", e))
                        })?;

                let stream_registry = core.js_stream_registry.clone();
                deno_core::scope!(scope, core.js_runtime);
                let local = v8::Local::new(scope, module_namespace);
                let value: v8::Local<'_, v8::Value> = local.into();
                let result = RuntimeCoreState::value_to_js_value(
                    &fn_registry,
                    &next_fn_id,
                    scope,
                    value,
                    limits,
                    stream_registry,
                );

                self.state = EvalModuleAsyncJobState::Done;
                Poll::Ready(result)
            }
            EvalModuleAsyncJobState::Done => {
                Poll::Ready(Err(RuntimeError::internal("Job already completed")))
            }
        }
    }

    fn finish(self: Box<Self>, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>) {
        self.common.respond(core, result);
    }

    fn start_time(&self) -> Instant {
        self.common.start_time
    }

    fn deadline(&self) -> Option<Instant> {
        self.common.deadline
    }
}

/// An async call of a stored JS function: look the function up, call it, then
/// await the result.
fn call_function_async_job(
    fn_id: u32,
    args: Vec<JSValue>,
    timeout_ms: Option<u64>,
    task_locals: Option<TaskLocals>,
    responder: oneshot::Sender<RuntimeResult<JSValue>>,
    core: &RuntimeCoreState,
) -> PromiseJob {
    // An explicit `timeout=` wins; otherwise the runtime-wide execution
    // timeout applies, so an async call is bounded by the same clock as a
    // synchronous one.
    let effective_timeout = timeout_ms.or_else(|| {
        core.execution_timeout.map(|d| {
            let millis = d.as_millis();
            if millis > u128::from(u64::MAX) {
                u64::MAX
            } else {
                millis as u64
            }
        })
    });

    let common = JobCommon::new(
        RuntimeCallKind::CallFunctionAsync,
        TimeoutWording::CALL_FUNCTION,
        "Async function call",
        effective_timeout,
        task_locals,
        responder,
        None,
    );

    PromiseJob::new(
        common,
        Box::new(move |core: &mut RuntimeCoreState| {
            // Check for a missing function before entering a scope.
            if !core.fn_registry.borrow().contains_key(&fn_id) {
                return Err(RuntimeError::internal(format!(
                    "Function ID {} not found",
                    fn_id
                )));
            }

            let promise_result: Result<Result<v8::Global<v8::Promise>, JsError>, RuntimeError> =
                (|| {
                    deno_core::scope!(scope, core.js_runtime);
                    v8::tc_scope!(let try_catch, scope);

                    // Get function and receiver from registry
                    let (func, receiver) = {
                        let registry = core.fn_registry.borrow();
                        let stored = registry.get(&fn_id).unwrap(); // Safe: checked above
                        let func = v8::Local::new(try_catch, &stored.function);
                        let receiver = stored
                            .receiver
                            .as_ref()
                            .map(|r| v8::Local::new(try_catch, r));
                        (func, receiver)
                    };

                    // Convert arguments
                    let mut v8_args = Vec::with_capacity(args.len());
                    for arg in &args {
                        let v8_val =
                            RuntimeCoreState::js_value_to_v8(&core.fn_registry, try_catch, arg)?;
                        v8_args.push(v8_val);
                    }

                    let call_receiver = receiver.unwrap_or_else(|| {
                        try_catch.get_current_context().global(try_catch).into()
                    });

                    match func.call(try_catch, call_receiver, &v8_args) {
                        Some(result_value) => {
                            let promise = PromiseJob::as_promise(try_catch, result_value)?;
                            Ok(Ok(v8::Global::new(try_catch, promise)))
                        }
                        None => match try_catch.exception() {
                            Some(exception) => {
                                let js_error = JsError::from_v8_exception(try_catch, exception);
                                Ok(Err(*js_error))
                            }
                            None => Err(RuntimeError::internal(
                                "Function call failed with no exception",
                            )),
                        },
                    }
                })();

            // Handle the result outside the scope, so `core` is free again.
            match promise_result {
                Ok(Ok(promise)) => Ok(promise),
                Ok(Err(js_error)) => Err(core.translate_js_error(js_error)),
                Err(err) => Err(err),
            }
        }),
    )
}

/// Resume a previously-started JS function call by awaiting its stored
/// promise, on the original call's clock.
fn resume_function_call_job(
    pending: PendingFunctionCall,
    task_locals: Option<TaskLocals>,
    responder: oneshot::Sender<RuntimeResult<JSValue>>,
) -> PromiseJob {
    let common = JobCommon::resumed(
        RuntimeCallKind::CallFunctionAsync,
        TimeoutWording::CALL_FUNCTION,
        "Async function call",
        &pending,
        task_locals,
        responder,
    );

    let promise = pending.promise;
    PromiseJob::new(common, Box::new(move |_core| Ok(promise)))
}

pub fn spawn_runtime_thread(config: RuntimeConfig) -> RuntimeResult<SpawnRuntimeResult> {
    let (cmd_tx, cmd_rx) = mpsc::unbounded_channel::<RuntimeCommand>();
    let (init_tx, init_rx): InitSignalChannel = std::sync::mpsc::channel();

    thread::Builder::new()
        .name("pydeno-deno-runtime".to_string())
        // This thread owns the V8 isolate and runs the recursive JSValue
        // serializers, which descend once per nesting level up to
        // `MAX_JS_DEPTH`. See `RUNTIME_THREAD_STACK_SIZE` for the measurement.
        .stack_size(RUNTIME_THREAD_STACK_SIZE)
        .spawn(move || {
            let _thread_guard = RuntimeThreadGuard::new();
            // Recorded here, before `JsRuntime::new` and while this thread's
            // stack is still shallow, so `LimitTracker::enter` can later tell
            // how much of this thread's *real* stack a deep recursive
            // conversion has actually consumed -- the OS thread's 16 MiB
            // reservation (`RUNTIME_THREAD_STACK_SIZE`) is not itself a safe
            // bound once `max_serialization_depth` is raised past its
            // default; V8's own stack limit is far smaller. See
            // `record_stack_anchor` and `STACK_HEADROOM_BYTES`.
            record_stack_anchor();
            let tokio_rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("failed to build tokio runtime");

            // `JsRuntime::new` (inside `RuntimeCoreState::new`) registers this
            // isolate with deno_core's platform via
            // `tokio::runtime::Handle::try_current()`. If no tokio runtime is
            // entered on this thread at that moment, the isolate is registered
            // with no handle, and V8 background compilation for large scripts
            // (which schedules a delayed foreground task once source size
            // crosses V8's internal streaming-compile threshold) later hits
            // deno_core's `spawn_delayed_task`, finds no handle, and calls
            // `std::process::abort()` -- an uncatchable SIGABRT, observed here
            // starting at ~148KB of JS source. Entering the runtime before
            // creating the isolate is exactly the fix deno_core's own abort
            // message recommends.
            let _tokio_enter = tokio_rt.enter();
            let core = match RuntimeCoreState::new(config) {
                Ok(core) => {
                    let termination = core.termination_controller();
                    let inspector_info =
                        match (core.inspector_metadata(), core.inspector_connection_state()) {
                            (Some(meta), Some(state)) => Some((meta, state)),
                            _ => None,
                        };
                    let py_stream_registry = core.py_stream_registry.clone();
                    let _ = init_tx.send(Ok((termination, inspector_info, py_stream_registry)));
                    core
                }
                Err(err) => {
                    let _ = init_tx.send(Err(err));
                    return;
                }
            };
            drop(_tokio_enter);

            tokio_rt.block_on(async move {
                let mut dispatcher = RuntimeDispatcher::new(core, cmd_rx);
                dispatcher.run().await;
            });
        })
        .map_err(|e| RuntimeError::internal(format!("Failed to spawn runtime thread: {}", e)))?;

    match init_rx.recv() {
        Ok(Ok((termination, inspector_info, py_stream_registry))) => {
            Ok((cmd_tx, termination, inspector_info, py_stream_registry))
        }
        Ok(Err(err)) => Err(err),
        Err(_) => Err(RuntimeError::internal(
            "Runtime thread initialization failed",
        )),
    }
}

pub fn active_runtime_threads() -> usize {
    ACTIVE_RUNTIME_THREADS.load(Ordering::SeqCst)
}

struct InspectorRuntimeState {
    _server: InspectorServer,
    registration: InspectorRegistration,
    wait_for_connection: bool,
    break_on_next_statement: bool,
    has_waited: bool,
    connection_state: InspectorConnectionState,
}

impl InspectorRuntimeState {
    fn metadata(&self) -> InspectorMetadata {
        self.registration.metadata().clone()
    }

    fn connection_state(&self) -> InspectorConnectionState {
        self.connection_state.clone()
    }
}

/// Core state that holds the V8 isolate and all runtime data.
/// Owned directly by RuntimeDispatcher to enable job polling without RefCell borrows.
struct RuntimeCoreState {
    js_runtime: JsRuntime,
    registry: PythonOpRegistry,
    module_loader: Rc<PythonModuleLoader>,
    task_locals: Option<TaskLocals>,
    execution_timeout: Option<Duration>,
    fn_registry: Rc<RefCell<HashMap<u32, StoredFunction>>>,
    next_fn_id: Rc<RefCell<u32>>,
    pending_calls: Rc<RefCell<HashMap<u64, PendingFunctionCall>>>,
    next_pending_call_id: Rc<RefCell<u64>>,
    stats_state: RuntimeStatsState,
    termination: TerminationController,
    /// One persistent watchdog thread per runtime; see [`Watchdog`]. Dropped
    /// (and joined) automatically when this state is dropped.
    watchdog: Watchdog,
    terminated: bool,
    inspector_state: Option<InspectorRuntimeState>,
    #[allow(dead_code)]
    startup_snapshot: Option<OwnedSnapshot>,
    serialization_limits: SerializationLimits,
    js_stream_registry: Rc<JsStreamRegistry>,
    py_stream_registry: PyStreamRegistry,
}

impl RuntimeCoreState {
    fn new(config: RuntimeConfig) -> RuntimeResult<Self> {
        let registry = PythonOpRegistry::new();
        let extension = python_extension(registry.clone());
        let module_loader = Rc::new(PythonModuleLoader::new());

        let RuntimeConfig {
            max_heap_size,
            initial_heap_size,
            execution_timeout,
            bootstrap_script,
            enable_console,
            on_console,
            inspector,
            snapshot,
            max_serialization_depth,
            max_serialization_bytes,
            // Consumed by `RuntimeHandle`, which does the waiting; the runtime
            // thread itself never needs it.
            force_kill_grace: _,
        } = config;

        if initial_heap_size.is_some() && max_heap_size.is_none() {
            return Err(RuntimeError::internal(
                "initial_heap_size requires max_heap_size to be set as well",
            ));
        }

        if let (Some(initial), Some(max)) = (initial_heap_size, max_heap_size) {
            if initial > max {
                return Err(RuntimeError::internal(format!(
                    "initial_heap_size ({}) cannot exceed max_heap_size ({})",
                    initial, max
                )));
            }
        }

        let create_params = match (max_heap_size, initial_heap_size) {
            (Some(max), initial) => {
                let initial_bytes = initial.unwrap_or(0);
                Some(v8::CreateParams::default().heap_limits(initial_bytes, max))
            }
            (None, _) => None,
        };

        let serialization_limits =
            SerializationLimits::new(max_serialization_depth, max_serialization_bytes);

        let mut snapshot_source = snapshot.map(OwnedSnapshot::new);
        let startup_snapshot = snapshot_source.as_mut().map(|source| source.as_static());

        let inspector_enabled = inspector.is_some();
        let mut js_runtime = JsRuntime::new(RuntimeOptions {
            extensions: vec![extension],
            create_params,
            module_loader: Some(module_loader.clone()),
            inspector: inspector_enabled,
            is_main: true,
            startup_snapshot,
            ..Default::default()
        });

        let js_stream_registry = Rc::new(JsStreamRegistry::new());
        let py_stream_registry = PyStreamRegistry::new(serialization_limits);

        js_runtime
            .op_state()
            .borrow_mut()
            .put(py_stream_registry.clone());

        // Must be in OpState before *any* script runs: the sync op path reads
        // SerializationLimits out of OpState (src/runtime/ops.rs), and console
        // capture plus a bootstrap script that logs both call an op during
        // construction. Registering it afterwards made those calls fail with
        // "Serialization limits not configured", which the console shim then
        // swallowed -- bootstrap output silently never reached the callback.
        js_runtime.op_state().borrow_mut().put(serialization_limits);

        if inspector_enabled {
            js_runtime.maybe_init_inspector();
        }

        // Disable console if enable_console is set to false, since Deno's bootstrap script enables console by default
        if enable_console == Some(false) {
            js_runtime
                .execute_script(
                    "<disable_console>",
                    r#"
                    (() => {
                        const noop = () => {};
                        const stub = new Proxy(Object.create(null), { get: () => noop });
                        const existing = globalThis.console;
                        if (typeof existing === "object" && existing !== null) {
                            for (const key of Reflect.ownKeys(existing)) {
                                try { existing[key] = noop; } catch (_) {} // ignore non-writable properties
                            }
                            return;
                        }
                        globalThis.console = stub;
                    })();
                    "#
                    .to_string(),
                )
                .map_err(|err| RuntimeError::javascript(JsExceptionDetails::from_js_error(*err)))?;
        }

        // Console capture. Installed *after* the disable_console stub above so
        // the callback still sees output when enable_console is false, and
        // *before* the user's bootstrap script so bootstrap output is captured
        // too. See RuntimeConfig::on_console for the full composition table.
        if let Some(callback) = on_console {
            let op_id = registry.register(
                "__pydeno_console__".to_string(),
                PythonOpMode::Sync,
                callback.0,
            );
            // The console shim below closes over the token, so exposing it
            // here is the bind step for this capability. The token is
            // unguessable and never a readable property, so guest JS cannot
            // forge console output through it.
            registry.expose(op_id);
            let passthrough = enable_console == Some(true);
            js_runtime
                .execute_script(
                    "<console_capture>",
                    format!(
                        r#"
                    (() => {{
                      const OP_ID = {op_id};
                      const PASSTHROUGH = {passthrough};
                      const forward = globalThis.__host_op_sync__;
                      const previous = globalThis.console;
                      const target = {{}};
                      for (const level of ["log", "info", "warn", "error", "debug", "trace"]) {{
                        const prior =
                          PASSTHROUGH &&
                          previous !== null &&
                          typeof previous === "object" &&
                          typeof previous[level] === "function"
                            ? previous[level].bind(previous)
                            : null;
                        target[level] = (...args) => {{
                          try {{
                            forward(OP_ID, level, args);
                          }} catch (_) {{
                            // The argument list was not representable as a
                            // Python value (a function, a circular object, over
                            // the serialization limits). Degrade to strings
                            // rather than throwing into guest JS, which would
                            // turn a console.log into a program-visible error.
                            try {{
                              forward(
                                OP_ID,
                                level,
                                args.map((a) => {{
                                  try {{
                                    return String(a);
                                  }} catch (_) {{
                                    return "[unrepresentable]";
                                  }}
                                }}),
                              );
                            }} catch (_) {{
                              // Give up: a console call must never break the
                              // script that made it.
                            }}
                          }}
                          if (prior !== null) {{
                            prior(...args);
                          }}
                        }};
                      }}
                      globalThis.console = target;
                    }})();
                    "#
                    ),
                )
                .map_err(|err| RuntimeError::javascript(JsExceptionDetails::from_js_error(*err)))?;
        }

        if let Some(script) = bootstrap_script {
            js_runtime
                .execute_script("<bootstrap>", script)
                .map_err(|err| RuntimeError::javascript(JsExceptionDetails::from_js_error(*err)))?;
        }

        let termination = {
            let isolate = js_runtime.v8_isolate();
            let handle = isolate.thread_safe_handle();
            TerminationController::new(handle)
        };

        let watchdog = Watchdog::spawn(termination.clone())?;

        if let Some(heap_limit_bytes) = max_heap_size {
            let termination_for_heap_limit = termination.clone();
            js_runtime.add_near_heap_limit_callback(move |current_limit, initial_limit| {
                termination_for_heap_limit.ensure_reason("Heap limit exceeded");
                let first_request = termination_for_heap_limit.request();
                if first_request {
                    log::error!(
                        "V8 isolate is nearing its heap limit; terminating execution \
                         (configured_heap_limit={heap_limit_bytes}, \
                         current_heap_limit={current_limit}, \
                         initial_heap_limit={initial_limit})"
                    );
                }
                termination_for_heap_limit.terminate_execution();

                // Returning a slightly larger limit gives V8 enough breathing room to unwind
                // after we terminate execution rather than letting it abort the process.
                let extra_headroom = initial_limit
                    .max(heap_limit_bytes / 8)
                    .max(NEAR_HEAP_LIMIT_MIN_HEADROOM_BYTES);
                current_limit.saturating_add(extra_headroom)
            });
        }

        let inspector_state = match inspector {
            Some(inspector_cfg) => {
                let wait_for_connection = inspector_cfg.wait_for_connection;
                let break_on_next_statement = inspector_cfg.break_on_next_statement;
                let connection_state = InspectorConnectionState::default();
                let server = InspectorServer::bind(inspector_cfg.socket_addr(), "pydeno").map_err(
                    |err| {
                        RuntimeError::internal(format!("Failed to start inspector server: {err}"))
                    },
                )?;

                let registration = server
                    .register_runtime(
                        js_runtime.inspector(),
                        InspectorRegistrationParams {
                            target_url: inspector_cfg.target_url.clone(),
                            display_name: inspector_cfg.display_name.clone(),
                            wait_for_connection,
                        },
                        connection_state.clone(),
                    )
                    .map_err(|err| {
                        RuntimeError::internal(format!("Failed to register inspector: {err}"))
                    })?;

                Some(InspectorRuntimeState {
                    _server: server,
                    registration,
                    wait_for_connection,
                    break_on_next_statement,
                    has_waited: false,
                    connection_state,
                })
            }
            None => None,
        };

        Ok(Self {
            js_runtime,
            registry,
            module_loader,
            task_locals: None,
            execution_timeout,
            fn_registry: Rc::new(RefCell::new(HashMap::new())),
            next_fn_id: Rc::new(RefCell::new(0)),
            pending_calls: Rc::new(RefCell::new(HashMap::new())),
            next_pending_call_id: Rc::new(RefCell::new(0)),
            stats_state: RuntimeStatsState::default(),
            termination,
            watchdog,
            terminated: false,
            inspector_state,
            startup_snapshot: snapshot_source,
            serialization_limits,
            js_stream_registry,
            py_stream_registry,
        })
    }

    fn inspector_metadata(&self) -> Option<InspectorMetadata> {
        self.inspector_state.as_ref().map(|state| state.metadata())
    }

    fn inspector_connection_state(&self) -> Option<InspectorConnectionState> {
        self.inspector_state
            .as_ref()
            .map(|state| state.connection_state())
    }

    fn ensure_inspector_ready(&mut self) -> RuntimeResult<()> {
        if let Some(state) = self.inspector_state.as_mut() {
            if state.has_waited {
                return Ok(());
            }
            if state.wait_for_connection || state.break_on_next_statement {
                let inspector = self.js_runtime.inspector();
                if state.break_on_next_statement {
                    inspector.wait_for_session_and_break_on_next_statement();
                } else if state.wait_for_connection {
                    inspector.wait_for_session();
                }
            }
            state.has_waited = true;
        }
        Ok(())
    }

    fn termination_controller(&self) -> TerminationController {
        self.termination.clone()
    }

    fn should_reject_new_work(&self) -> bool {
        self.terminated || self.termination.is_requested()
    }

    fn terminated_error(&self) -> RuntimeError {
        self.termination.terminated_error()
    }

    /// Clear task locals after a job completes to prevent stale event loop references
    fn clear_task_locals(&mut self) {
        self.task_locals = None;
        self.module_loader.clear_task_locals();
        self.js_runtime
            .op_state()
            .borrow_mut()
            .put(crate::runtime::ops::GlobalTaskLocals(None));
    }

    fn effective_timeout_ms(&self, timeout_ms: Option<u64>) -> Option<u64> {
        timeout_ms.or_else(|| {
            self.execution_timeout.map(|d| {
                let millis = d.as_millis();
                if millis > u128::from(u64::MAX) {
                    u64::MAX
                } else {
                    millis as u64
                }
            })
        })
    }

    fn is_readable_stream(
        scope: &mut v8::PinScope<'_, '_>,
        value: v8::Local<'_, v8::Value>,
    ) -> bool {
        if !value.is_object() {
            return false;
        }

        let key = match v8::String::new(scope, "ReadableStream") {
            Some(k) => k,
            None => return false,
        };
        let ctor_value = match scope
            .get_current_context()
            .global(scope)
            .get(scope, key.into())
        {
            Some(val) => val,
            None => return false,
        };
        let ctor = match v8::Local::<v8::Function>::try_from(ctor_value) {
            Ok(func) => func,
            Err(_) => return false,
        };

        value.instance_of(scope, ctor.into()).unwrap_or_default()
    }

    fn store_pending_call(
        &self,
        promise: v8::Global<v8::Promise>,
        start_time: Instant,
        deadline: Option<Instant>,
        timeout_ms: Option<u64>,
    ) -> u64 {
        let mut next_id = self.next_pending_call_id.borrow_mut();
        let call_id = *next_id;
        *next_id = next_id.wrapping_add(1);
        self.pending_calls.borrow_mut().insert(
            call_id,
            PendingFunctionCall {
                promise,
                start_time,
                deadline,
                timeout_ms,
            },
        );
        call_id
    }

    fn take_pending_call(&self, call_id: u64) -> RuntimeResult<PendingFunctionCall> {
        self.pending_calls
            .borrow_mut()
            .remove(&call_id)
            .ok_or_else(|| {
                RuntimeError::internal(format!("Pending function call {} not found", call_id))
            })
    }

    fn start_sync_watchdog(&self, reason: &str) -> RuntimeResult<Option<WatchdogToken>> {
        Ok(self
            .execution_timeout
            .map(|duration| self.watchdog.arm(duration, reason.to_string())))
    }

    fn start_timeout_watchdog(
        &self,
        timeout_ms: Option<u64>,
        reason: &str,
    ) -> RuntimeResult<Option<WatchdogToken>> {
        Ok(timeout_ms.map(|ms| {
            let duration = Duration::from_millis(ms);
            self.watchdog.arm(duration, reason.to_string())
        }))
    }

    fn resolve_sync_watchdog(
        &mut self,
        watchdog: WatchdogToken,
    ) -> RuntimeResult<(bool, Duration)> {
        let duration = watchdog.duration;
        let fired = self.watchdog.disarm(watchdog);
        if fired {
            self.cancel_pending_termination();
        }
        Ok((fired, duration))
    }

    /// Clear a termination request that has already done its job, so it cannot
    /// latch and kill the next, unrelated, call. A no-op when none is pending.
    fn cancel_pending_termination(&mut self) {
        let isolate = self.js_runtime.v8_isolate();
        let _ = isolate.cancel_terminate_execution();
    }

    fn apply_watchdog_result<T>(
        &mut self,
        result: RuntimeResult<T>,
        watchdog: Option<WatchdogToken>,
        context: &str,
    ) -> RuntimeResult<T> {
        if let Some(watchdog) = watchdog {
            let (fired, duration) = self.resolve_sync_watchdog(watchdog)?;
            if fired {
                let message = format!("{context} timed out after {}ms", duration.as_millis());
                return match result {
                    Err(err) if Self::runtime_error_indicates_termination(&err) => {
                        Err(RuntimeError::timeout(message))
                    }
                    Err(err) => Err(err),
                    Ok(_) => Err(RuntimeError::timeout(message)),
                };
            }
        }
        result
    }

    fn finalize_termination(&mut self) -> RuntimeResult<()> {
        if self.terminated {
            return Ok(());
        }

        let isolate = self.js_runtime.v8_isolate();
        // ignore return value; false indicates no termination was pending.
        let _ = isolate.cancel_terminate_execution();

        self.fn_registry.borrow_mut().clear();
        self.pending_calls.borrow_mut().clear();
        self.termination.mark_terminated();
        self.terminated = true;
        Ok(())
    }

    fn translate_js_error(&mut self, err: JsError) -> RuntimeError {
        let details = JsExceptionDetails::from_js_error(err);
        if self.should_reject_new_work() && Self::js_error_indicates_termination(&details) {
            let _ = self.finalize_termination();
            self.terminated_error()
        } else {
            RuntimeError::javascript(details)
        }
    }

    fn translate_core_error(&mut self, err: CoreError) -> RuntimeError {
        let runtime_error = RuntimeError::from(err);
        if self.should_reject_new_work()
            && Self::runtime_error_indicates_termination(&runtime_error)
        {
            let _ = self.finalize_termination();
            self.terminated_error()
        } else {
            runtime_error
        }
    }

    fn runtime_error_indicates_termination(err: &RuntimeError) -> bool {
        match err {
            RuntimeError::JavaScript(details) => Self::js_error_indicates_termination(details),
            RuntimeError::Timeout { context } | RuntimeError::Internal { context } => {
                context.contains("execution terminated")
            }
            RuntimeError::Terminated { .. } | RuntimeError::ForceKilled { .. } => true,
        }
    }

    fn js_error_indicates_termination(details: &JsExceptionDetails) -> bool {
        let needle = "execution terminated";
        details
            .message
            .as_deref()
            .map(|msg| msg.contains(needle))
            .unwrap_or(false)
            || details.summary().contains(needle)
    }

    fn register_python_op(
        &self,
        name: String,
        mode: PythonOpMode,
        handler: Py<PyAny>,
    ) -> RuntimeResult<OpToken> {
        Ok(self.registry.register(name, mode, handler))
    }

    /// Expose (`true`) or revoke (`false`) one op capability.
    fn set_python_op_exposure(&self, op_id: OpToken, exposed: bool) -> bool {
        if exposed {
            self.registry.expose(op_id)
        } else {
            self.registry.revoke(op_id)
        }
    }

    fn bind_object(
        &mut self,
        name: String,
        properties: Vec<BoundObjectProperty>,
    ) -> RuntimeResult<()> {
        let registry = self.registry.clone();
        deno_core::scope!(scope, self.js_runtime);
        v8::tc_scope!(let try_catch, scope);
        let context = try_catch.get_current_context();
        let global = context.global(try_catch);

        let helper_key = v8::String::new(try_catch, "__pydeno_bind_object")
            .ok_or_else(|| RuntimeError::internal("Failed to allocate helper name"))?;
        let helper_value = global
            .get(try_catch, helper_key.into())
            .ok_or_else(|| RuntimeError::internal("Missing __pydeno_bind_object helper"))?;
        let helper_fn = v8::Local::<v8::Function>::try_from(helper_value)
            .map_err(|_| RuntimeError::internal("__pydeno_bind_object is not callable"))?;

        let global_name = v8::String::new(try_catch, &name)
            .ok_or_else(|| RuntimeError::internal("Failed to allocate target name"))?;

        let assignments = v8::Array::new(try_catch, properties.len() as i32);
        let mut op_tokens: Vec<OpToken> = Vec::new();

        for (index, entry) in properties.into_iter().enumerate() {
            let entry_obj = v8::Object::new(try_catch);

            let key_literal = v8::String::new(try_catch, "key")
                .ok_or_else(|| RuntimeError::internal("Failed to allocate 'key' literal"))?;
            let key_value = v8::String::new(
                try_catch,
                match &entry {
                    BoundObjectProperty::Value { key, .. }
                    | BoundObjectProperty::Op { key, .. } => key,
                },
            )
            .ok_or_else(|| RuntimeError::internal("Failed to allocate property name"))?;
            entry_obj
                .set(try_catch, key_literal.into(), key_value.into())
                .ok_or_else(|| RuntimeError::internal("Failed to set entry key"))?;

            let kind_literal = v8::String::new(try_catch, "kind")
                .ok_or_else(|| RuntimeError::internal("Failed to allocate 'kind' literal"))?;

            match entry {
                BoundObjectProperty::Value { key: _, value } => {
                    let kind_value = v8::String::new(try_catch, "value")
                        .ok_or_else(|| RuntimeError::internal("Failed to allocate kind value"))?;
                    entry_obj
                        .set(try_catch, kind_literal.into(), kind_value.into())
                        .ok_or_else(|| RuntimeError::internal("Failed to set entry kind"))?;

                    let value_literal = v8::String::new(try_catch, "value").ok_or_else(|| {
                        RuntimeError::internal("Failed to allocate 'value' literal")
                    })?;
                    let v8_value =
                        RuntimeCoreState::js_value_to_v8(&self.fn_registry, try_catch, &value)?;
                    entry_obj
                        .set(try_catch, value_literal.into(), v8_value)
                        .ok_or_else(|| RuntimeError::internal("Failed to set entry value"))?;
                }
                BoundObjectProperty::Op {
                    key: _,
                    op_id,
                    mode,
                } => {
                    op_tokens.push(op_id);
                    let kind_value = v8::String::new(try_catch, "op")
                        .ok_or_else(|| RuntimeError::internal("Failed to allocate kind value"))?;
                    entry_obj
                        .set(try_catch, kind_literal.into(), kind_value.into())
                        .ok_or_else(|| RuntimeError::internal("Failed to set entry kind"))?;

                    let op_id_literal = v8::String::new(try_catch, "op_id").ok_or_else(|| {
                        RuntimeError::internal("Failed to allocate 'op_id' literal")
                    })?;
                    let op_id_value = v8::Number::new(try_catch, op_id as f64);
                    entry_obj
                        .set(try_catch, op_id_literal.into(), op_id_value.into())
                        .ok_or_else(|| RuntimeError::internal("Failed to set op id"))?;

                    let mode_literal = v8::String::new(try_catch, "mode").ok_or_else(|| {
                        RuntimeError::internal("Failed to allocate 'mode' literal")
                    })?;
                    let mode_value = v8::String::new(
                        try_catch,
                        match mode {
                            PythonOpMode::Async => "async",
                            PythonOpMode::Sync => "sync",
                        },
                    )
                    .ok_or_else(|| RuntimeError::internal("Failed to allocate mode value"))?;
                    entry_obj
                        .set(try_catch, mode_literal.into(), mode_value.into())
                        .ok_or_else(|| RuntimeError::internal("Failed to set mode"))?;
                }
            }

            assignments
                .set_index(try_catch, index as u32, entry_obj.into())
                .ok_or_else(|| RuntimeError::internal("Failed to store assignment entry"))?;
        }

        match helper_fn.call(
            try_catch,
            global.into(),
            &[global_name.into(), assignments.into()],
        ) {
            Some(_) => {
                // Exposure happens here, not at registration: the capability
                // becomes dispatchable only once the binding it belongs to is
                // actually installed in the guest's scope. A `__pydeno_bind_object`
                // that threw leaves the handlers registered but unreachable.
                for token in op_tokens {
                    registry.expose(token);
                }
                Ok(())
            }
            None => {
                if let Some(exception) = try_catch.exception() {
                    let js_error = JsError::from_v8_exception(try_catch, exception);
                    Err(RuntimeError::javascript(JsExceptionDetails::from_js_error(
                        *js_error,
                    )))
                } else {
                    Err(RuntimeError::internal(
                        "__pydeno_bind_object invocation failed",
                    ))
                }
            }
        }
    }

    /// Measure the duration of a synchronous entry point, including error paths.
    fn with_timing<T, F>(&mut self, kind: RuntimeCallKind, f: F) -> RuntimeResult<T>
    where
        F: FnOnce(&mut Self) -> RuntimeResult<T>,
    {
        let start = Instant::now();
        let result = f(self);
        let elapsed = start.elapsed();
        self.stats_state.record(kind, elapsed);
        result
    }

    fn eval_sync(&mut self, code: &str) -> RuntimeResult<JSValue> {
        self.with_timing(RuntimeCallKind::EvalSync, |this| {
            let global_value = this
                .js_runtime
                .execute_script("<eval>", code.to_string())
                .map_err(|err| this.translate_js_error(*err))?;

            // `execute_script` only runs the top-level script; anything the
            // script queued with `queueMicrotask` (including a microtask
            // that re-queues itself, forever) is still pending and does not
            // run here. Left undrained, that queue was later drained by
            // *some other, unrelated, untimed* call on this thread -- the
            // next `eval`, `close`, or the dispatcher's own event-loop poll
            // between commands -- which is what actually hung, arbitrarily
            // far from the call that caused it and with no watchdog covering
            // it. Draining it here, still inside this call's own
            // start_sync_watchdog window, means a runaway microtask is
            // bounded by *this* call's timeout and reported against it: a
            // watchdog-driven `terminate_execution` interrupts the drain
            // exactly as it would interrupt a runaway script, and
            // `apply_watchdog_result` maps that to `TimeoutError` the same
            // way. See tests/test_microtask_timeout.py.
            this.js_runtime.v8_isolate().perform_microtask_checkpoint();

            let fn_registry = this.fn_registry.clone();
            let next_fn_id = this.next_fn_id.clone();
            let limits = this.serialization_limits;
            let stream_registry = this.js_stream_registry.clone();
            deno_core::scope!(scope, this.js_runtime);
            let local = v8::Local::new(scope, global_value);
            Self::value_to_js_value(
                &fn_registry,
                &next_fn_id,
                scope,
                local,
                limits,
                stream_registry,
            )
        })
    }

    fn eval_module_sync(&mut self, specifier: &str) -> RuntimeResult<JSValue> {
        self.with_timing(RuntimeCallKind::EvalModuleSync, |this| {
            // Try to parse as absolute URL first, if it fails, resolve it as a bare specifier
            let module_specifier = if specifier.contains(':') || specifier.starts_with('/') {
                // Already a URL or absolute path
                deno_core::ModuleSpecifier::parse(specifier).map_err(|e| {
                    RuntimeError::internal(format!(
                        "Invalid module specifier '{}': {}",
                        specifier, e
                    ))
                })?
            } else {
                // Bare specifier - resolve relative to a synthetic base
                let base = deno_core::ModuleSpecifier::parse("pydeno://runtime/").map_err(|e| {
                    RuntimeError::internal(format!("Failed to create base URL: {}", e))
                })?;
                base.join(specifier).map_err(|e| {
                    RuntimeError::internal(format!(
                        "Failed to resolve module specifier '{}': {}",
                        specifier, e
                    ))
                })?
            };

            // Load the module
            let module_id =
                futures::executor::block_on(this.js_runtime.load_main_es_module(&module_specifier))
                    .map_err(|e| {
                        RuntimeError::internal(format!(
                            "Failed to load module '{}': {}",
                            specifier, e
                        ))
                    })?;

            // Evaluate the module
            let receiver = this.js_runtime.mod_evaluate(module_id);

            // Poll the runtime until the module evaluation completes
            let poll_options = PollEventLoopOptions::default();
            futures::executor::block_on(this.js_runtime.run_event_loop(poll_options))
                .map_err(|err| this.translate_core_error(err))?;

            // Wait for the evaluation result - receiver returns Result<(), CoreError>
            let eval_result = futures::executor::block_on(receiver);

            // Check if evaluation succeeded
            if let Err(err) = eval_result {
                return Err(this.translate_core_error(err));
            }

            // `run_event_loop` above already drains microtasks tied to the
            // module's own evaluation, but a microtask queued from module
            // top-level code that isn't on the path the event loop waited
            // for (e.g. a bare `queueMicrotask(...)` call, not part of what
            // `mod_evaluate`'s receiver awaits) can still be left pending.
            // Drain it here too, for the same reason and with the same
            // watchdog coverage as `eval_sync` -- see the comment there and
            // tests/test_microtask_timeout.py.
            this.js_runtime.v8_isolate().perform_microtask_checkpoint();

            // Get the module namespace - must call get_module_namespace before handle_scope
            let module_namespace =
                this.js_runtime
                    .get_module_namespace(module_id)
                    .map_err(|e| {
                        RuntimeError::internal(format!("Failed to get module namespace: {}", e))
                    })?;
            let fn_registry = this.fn_registry.clone();
            let next_fn_id = this.next_fn_id.clone();
            let limits = this.serialization_limits;
            let stream_registry = this.js_stream_registry.clone();
            deno_core::scope!(scope, this.js_runtime);
            let namespace_obj = v8::Local::new(scope, module_namespace);
            let namespace_value: v8::Local<'_, v8::Value> = namespace_obj.into();
            Self::value_to_js_value(
                &fn_registry,
                &next_fn_id,
                scope,
                namespace_value,
                limits,
                stream_registry,
            )
        })
    }

    fn call_function_sync(
        &mut self,
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
    ) -> RuntimeResult<FunctionCallResult> {
        self.with_timing(RuntimeCallKind::CallFunctionSync, |this| {
            this.invoke_function_sync(fn_id, args, timeout_ms)
        })
    }

    fn invoke_function_sync(
        &mut self,
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
    ) -> RuntimeResult<FunctionCallResult> {
        if !self.fn_registry.borrow().contains_key(&fn_id) {
            return Err(RuntimeError::internal(format!(
                "Function ID {} not found",
                fn_id
            )));
        }

        let stream_registry = self.js_stream_registry.clone();

        let start_time = Instant::now();
        let effective_timeout = self.effective_timeout_ms(timeout_ms);
        let deadline = effective_timeout.map(|ms| start_time + Duration::from_millis(ms));

        let fn_registry = self.fn_registry.clone();
        let next_fn_id = self.next_fn_id.clone();
        let limits = self.serialization_limits;

        enum SyncCallOutcome {
            Immediate(JSValue),
            Pending(v8::Global<v8::Promise>),
        }

        enum SyncCallError {
            Runtime(RuntimeError),
            // Boxed: `JsError` carries the whole stack-frame vector, so an
            // inline variant makes every `Ok` return on this path pay for the
            // error case (clippy::result_large_err).
            Js(Box<JsError>),
        }

        let call_outcome: Result<SyncCallOutcome, SyncCallError> = (|| {
            deno_core::scope!(scope, self.js_runtime);
            v8::tc_scope!(let try_catch, scope);

            let (func, receiver) = {
                let registry = self.fn_registry.borrow();
                let stored = registry.get(&fn_id).unwrap();
                let func = v8::Local::new(try_catch, &stored.function);
                let receiver = stored
                    .receiver
                    .as_ref()
                    .map(|recv| v8::Local::new(try_catch, recv));
                (func, receiver)
            };

            let mut v8_args = Vec::with_capacity(args.len());
            for arg in &args {
                let v8_val = RuntimeCoreState::js_value_to_v8(&fn_registry, try_catch, arg)
                    .map_err(SyncCallError::Runtime)?;
                v8_args.push(v8_val);
            }

            let call_receiver = receiver
                .unwrap_or_else(|| try_catch.get_current_context().global(try_catch).into());

            match func.call(try_catch, call_receiver, &v8_args) {
                Some(result_value) => {
                    try_catch.perform_microtask_checkpoint();

                    if result_value.is_promise() {
                        let promise =
                            v8::Local::<v8::Promise>::try_from(result_value).map_err(|_| {
                                SyncCallError::Runtime(RuntimeError::internal(
                                    "Failed to cast to Promise",
                                ))
                            })?;

                        match promise.state() {
                            v8::PromiseState::Pending => {
                                let promise_global = v8::Global::new(try_catch, promise);
                                Ok(SyncCallOutcome::Pending(promise_global))
                            }
                            v8::PromiseState::Fulfilled => {
                                let fulfilled_value = promise.result(try_catch);
                                RuntimeCoreState::value_to_js_value(
                                    &fn_registry,
                                    &next_fn_id,
                                    try_catch,
                                    fulfilled_value,
                                    limits,
                                    stream_registry.clone(),
                                )
                                .map(SyncCallOutcome::Immediate)
                                .map_err(SyncCallError::Runtime)
                            }
                            v8::PromiseState::Rejected => {
                                let exception = promise.result(try_catch);
                                let js_error = JsError::from_v8_exception(try_catch, exception);
                                Err(SyncCallError::Js(js_error))
                            }
                        }
                    } else {
                        RuntimeCoreState::value_to_js_value(
                            &fn_registry,
                            &next_fn_id,
                            try_catch,
                            result_value,
                            limits,
                            stream_registry,
                        )
                        .map(SyncCallOutcome::Immediate)
                        .map_err(SyncCallError::Runtime)
                    }
                }
                None => match try_catch.exception() {
                    Some(exception) => {
                        let js_error = JsError::from_v8_exception(try_catch, exception);
                        Err(SyncCallError::Js(js_error))
                    }
                    None => Err(SyncCallError::Runtime(RuntimeError::internal(
                        "Function call failed with no exception",
                    ))),
                },
            }
        })();

        match call_outcome {
            Ok(SyncCallOutcome::Immediate(value)) => Ok(FunctionCallResult::Immediate(value)),
            Ok(SyncCallOutcome::Pending(promise)) => {
                let call_id =
                    self.store_pending_call(promise, start_time, deadline, effective_timeout);
                Ok(FunctionCallResult::Pending { call_id })
            }
            Err(SyncCallError::Runtime(err)) => Err(err),
            Err(SyncCallError::Js(js_error)) => Err(self.translate_js_error(*js_error)),
        }
    }

    /// Remove a function from the registry, freeing its V8 global handle.
    fn release_function(&mut self, fn_id: u32) -> RuntimeResult<()> {
        let mut registry = self.fn_registry.borrow_mut();
        if registry.remove(&fn_id).is_none() {
            log::debug!("Attempted to release unknown function id {}", fn_id);
        }
        Ok(())
    }

    fn release_js_stream(&self, stream_id: u32) -> RuntimeResult<()> {
        self.js_stream_registry.release(stream_id);
        Ok(())
    }

    fn cancel_js_stream(&mut self, stream_id: u32) -> RuntimeResult<()> {
        {
            deno_core::scope!(scope, self.js_runtime);
            if let Ok(reader) = self.js_stream_registry.ensure_reader(scope, stream_id) {
                if let Some(cancel_key) = v8::String::new(scope, "cancel") {
                    if let Some(cancel_value) = reader.get(scope, cancel_key.into()) {
                        if let Ok(cancel_fn) = v8::Local::<v8::Function>::try_from(cancel_value) {
                            let _ = cancel_fn.call(scope, reader.into(), &[]);
                        }
                    }
                }
            }
        }

        self.js_stream_registry.release(stream_id);
        Ok(())
    }

    fn collect_stats(&mut self) -> RuntimeResult<RuntimeStatsSnapshot> {
        let heap = self.snapshot_memory_usage();
        let execution = self.stats_state.snapshot();
        let activity = self.snapshot_activity();
        let mut streams = self.js_stream_registry.stats_snapshot();
        streams.merge(&self.py_stream_registry.stats_snapshot());
        Ok(RuntimeStatsSnapshot::new(
            heap, execution, activity, streams,
        ))
    }

    /// Snapshot V8 heap statistics. `get_heap_statistics` is safe here because it only reads isolate state.
    fn snapshot_memory_usage(&mut self) -> HeapSnapshot {
        let stats = self.js_runtime.v8_isolate().get_heap_statistics();
        HeapSnapshot {
            heap_total_bytes: stats.total_heap_size() as u64,
            heap_used_bytes: stats.used_heap_size() as u64,
            external_memory_bytes: stats.external_memory() as u64,
            physical_total_bytes: stats.total_physical_size() as u64,
        }
    }

    fn snapshot_activity(&self) -> ActivitySummary {
        let factory: RuntimeActivityStatsFactory = self.js_runtime.runtime_activity_stats_factory();
        let filter = RuntimeActivityStatsFilter::all();
        let snapshot = factory.capture(&filter).dump();
        ActivitySummary::from_snapshot(snapshot)
    }

    /// Convert a V8 value to JSValue with circular reference detection and limits enforced.
    fn value_to_js_value<'s>(
        fn_registry: &Rc<RefCell<HashMap<u32, StoredFunction>>>,
        next_fn_id: &Rc<RefCell<u32>>,
        scope: &mut v8::PinScope<'s, '_>,
        value: v8::Local<'s, v8::Value>,
        limits: SerializationLimits,
        stream_registry: Rc<JsStreamRegistry>,
    ) -> RuntimeResult<JSValue> {
        let mut seen: Vec<v8::Local<'s, v8::Object>> = Vec::new();
        let mut tracker = LimitTracker::new(limits.max_depth, limits.max_bytes);
        Self::value_to_js_value_internal(
            fn_registry,
            next_fn_id,
            scope,
            value,
            &mut seen,
            &mut tracker,
            None,
            stream_registry,
        )
    }

    fn js_value_to_v8<'s>(
        registry: &Rc<RefCell<HashMap<u32, StoredFunction>>>,
        scope: &mut v8::PinScope<'s, '_>,
        value: &JSValue,
    ) -> RuntimeResult<v8::Local<'s, v8::Value>> {
        match value {
            JSValue::Undefined => Ok(v8::undefined(scope).into()),
            JSValue::Null => Ok(v8::null(scope).into()),
            JSValue::Bool(b) => Ok(v8::Boolean::new(scope, *b).into()),
            JSValue::Int(i) => Ok(v8::Number::new(scope, *i as f64).into()),
            JSValue::BigInt(bigint) => {
                let (sign, bytes) = bigint.to_bytes_le();
                let mut words = Vec::with_capacity(bytes.len().div_ceil(8));
                for chunk in bytes.chunks(8) {
                    let mut buf = [0u8; 8];
                    buf[..chunk.len()].copy_from_slice(chunk);
                    words.push(u64::from_le_bytes(buf));
                }
                let sign_bit = matches!(sign, Sign::Minus);
                let v8_bigint = v8::BigInt::new_from_words(scope, sign_bit, &words)
                    .ok_or_else(|| RuntimeError::internal("Failed to create BigInt"))?;
                Ok(v8_bigint.into())
            }
            JSValue::Float(f) => Ok(v8::Number::new(scope, *f).into()),
            JSValue::String(s) => {
                let v8_str = v8::String::new(scope, s)
                    .ok_or_else(|| RuntimeError::internal("Failed to allocate string"))?;
                Ok(v8_str.into())
            }
            JSValue::Bytes(bytes) => {
                let backing = v8::ArrayBuffer::new_backing_store_from_vec(bytes.clone());
                let shared = backing.make_shared();
                let buffer = v8::ArrayBuffer::with_backing_store(scope, &shared);
                let len = bytes.len();
                let typed = v8::Uint8Array::new(scope, buffer, 0, len)
                    .ok_or_else(|| RuntimeError::internal("Failed to create Uint8Array"))?;
                Ok(typed.into())
            }
            JSValue::Array(items) => {
                let array = v8::Array::new(scope, items.len() as i32);
                for (index, item) in items.iter().enumerate() {
                    let v8_value = Self::js_value_to_v8(registry, scope, item)?;
                    array
                        .set_index(scope, index as u32, v8_value)
                        .ok_or_else(|| RuntimeError::internal("Failed to set array element"))?;
                }
                Ok(array.into())
            }
            JSValue::Set(values) => {
                let set = v8::Set::new(scope);
                for value in values {
                    let v8_value = Self::js_value_to_v8(registry, scope, value)?;
                    set.add(scope, v8_value);
                }
                Ok(set.into())
            }
            JSValue::Object(map) => {
                let object = v8::Object::new(scope);
                for (key, val) in map.iter() {
                    let key_str = v8::String::new(scope, key).ok_or_else(|| {
                        RuntimeError::internal(format!("Failed to allocate key '{key}'"))
                    })?;
                    let v8_value = Self::js_value_to_v8(registry, scope, val)?;
                    object.set(scope, key_str.into(), v8_value).ok_or_else(|| {
                        RuntimeError::internal(format!("Failed to set property '{key}'"))
                    })?;
                }
                Ok(object.into())
            }
            JSValue::Date(epoch_ms) => {
                let date = v8::Date::new(scope, *epoch_ms as f64)
                    .ok_or_else(|| RuntimeError::internal("Failed to create Date"))?;
                Ok(date.into())
            }
            JSValue::Function { id } => {
                let registry_ref = registry.borrow();
                let stored = registry_ref.get(id).ok_or_else(|| {
                    RuntimeError::internal(format!("Function ID {} not found in args", id))
                })?;
                Ok(v8::Local::new(scope, &stored.function).into())
            }
            JSValue::PyStream { id } => {
                let context = scope.get_current_context();
                let global = context.global(scope);
                let helper_key = v8::String::new(scope, "__pydeno_from_py_stream")
                    .ok_or_else(|| RuntimeError::internal("Failed to allocate helper key"))?;
                let helper_value = global.get(scope, helper_key.into()).ok_or_else(|| {
                    RuntimeError::internal("Missing __pydeno_from_py_stream helper")
                })?;
                let helper_fn =
                    v8::Local::<v8::Function>::try_from(helper_value).map_err(|_| {
                        RuntimeError::internal("__pydeno_from_py_stream is not callable")
                    })?;
                let id_value = v8::Number::new(scope, *id as f64);
                helper_fn
                    .call(scope, global.into(), &[id_value.into()])
                    .ok_or_else(|| {
                        RuntimeError::internal("__pydeno_from_py_stream invocation failed")
                    })
            }
            JSValue::JsStream { .. } => Err(RuntimeError::internal(
                "JsStream values cannot be sent back into JavaScript",
            )),
        }
    }

    /// Internal recursive converter with cycle detection and optional receiver capture.
    ///
    /// `seen` is the path of container objects (`Array`/`Set`/plain object)
    /// currently being descended into, in traversal order -- not a global set
    /// of every object visited. A cycle is "this object is its own ancestor
    /// on the current path", checked with `strict_equals` in O(depth) rather
    /// than a `v8::Object::get_identity_hash()`-keyed `HashSet<i32>`.
    ///
    /// The identity-hash version rejected valid, acyclic input on a hash
    /// collision: `get_identity_hash` returns a 32-bit hash that two distinct
    /// live objects can share, and the old code treated any repeated hash as
    /// "this object again", regardless of which object it actually was --
    /// `{a: {}, b: {}}` was one misfortune of V8's hash function away from a
    /// spurious "Cannot serialize circular reference". A path membership
    /// check has no such failure mode: it only reports a cycle when an
    /// object *actually* reappears as its own ancestor.
    #[allow(clippy::too_many_arguments)]
    fn value_to_js_value_internal<'s>(
        fn_registry: &Rc<RefCell<HashMap<u32, StoredFunction>>>,
        next_fn_id: &Rc<RefCell<u32>>,
        scope: &mut v8::PinScope<'s, '_>,
        value: v8::Local<'s, v8::Value>,
        seen: &mut Vec<v8::Local<'s, v8::Object>>,
        tracker: &mut LimitTracker,
        receiver: Option<v8::Global<v8::Value>>,
        stream_registry: Rc<JsStreamRegistry>,
    ) -> RuntimeResult<JSValue> {
        tracker.enter()?;

        let result = if value.is_undefined() {
            tracker.add_bytes(0)?;
            Ok(JSValue::Undefined)
        } else if value.is_null() {
            tracker.add_bytes(4)?;
            Ok(JSValue::Null)
        } else if value.is_boolean() {
            tracker.add_bytes(5)?; // "false" (worst case)
            Ok(JSValue::Bool(value.boolean_value(scope)))
        } else if value.is_number() {
            // Handle special numeric values (NaN, ±Infinity)
            let num_obj = value
                .to_number(scope)
                .ok_or_else(|| RuntimeError::internal("Failed to convert value to number"))?;
            let num_val = num_obj.value();
            if num_val.is_nan() || num_val.is_infinite() {
                tracker.add_bytes(24)?;
                Ok(JSValue::Float(num_val))
            } else if num_val.fract() == 0.0 && num_val.is_finite() {
                let as_int = num_val as i64;
                if as_int as f64 == num_val {
                    tracker.add_bytes(20)?;
                    Ok(JSValue::Int(as_int))
                } else {
                    tracker.add_bytes(24)?;
                    Ok(JSValue::Float(num_val))
                }
            } else {
                tracker.add_bytes(24)?;
                Ok(JSValue::Float(num_val))
            }
        } else if value.is_big_int() {
            let bigint = v8::Local::<v8::BigInt>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to BigInt"))?;
            let (int_value, lossless) = bigint.i64_value();
            if lossless {
                tracker.add_bytes(20)?;
                Ok(JSValue::Int(int_value))
            } else {
                let string = bigint
                    .to_string(scope)
                    .ok_or_else(|| RuntimeError::internal("Failed to stringify BigInt"))?
                    .to_rust_string_lossy(scope);
                let parsed = BigInt::parse_bytes(string.as_bytes(), 10)
                    .ok_or_else(|| RuntimeError::internal("Failed to parse BigInt literal"))?;
                tracker.add_bytes(string.len())?;
                Ok(JSValue::BigInt(parsed))
            }
        } else if value.is_string() {
            let string = value
                .to_string(scope)
                .ok_or_else(|| RuntimeError::internal("Failed to convert string"))?;
            let rust_str = string.to_rust_string_lossy(scope);
            tracker.add_bytes(rust_str.len())?;
            Ok(JSValue::String(rust_str))
        } else if value.is_function() {
            // Register function and return proxy ID
            let func = v8::Local::<v8::Function>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to function"))?;

            // Create a Global handle to keep the function alive
            let fn_handle = v8::Global::new(scope, func);

            // Register in the function registry
            let mut registry = fn_registry.borrow_mut();
            let mut next_id_val = next_fn_id.borrow_mut();

            let fn_id = *next_id_val;
            *next_id_val += 1;

            registry.insert(
                fn_id,
                StoredFunction {
                    function: fn_handle,
                    receiver, // Capture receiver for 'this' binding
                },
            );

            tracker.add_bytes(8)?; // ID size
            Ok(JSValue::Function { id: fn_id })
        } else if value.is_symbol() {
            Err(RuntimeError::internal("Cannot serialize V8 symbol"))
        } else if value.is_uint8_array() {
            let typed_array = v8::Local::<v8::Uint8Array>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Uint8Array"))?;
            let length = typed_array.byte_length();
            tracker.add_bytes(length)?;
            let mut buffer = vec![0u8; length];
            let view: v8::Local<v8::ArrayBufferView> = typed_array.into();
            view.copy_contents(&mut buffer);
            Ok(JSValue::Bytes(buffer))
        } else if value.is_array_buffer() {
            let array_buffer = v8::Local::<v8::ArrayBuffer>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to ArrayBuffer"))?;
            let length = array_buffer.byte_length();
            tracker.add_bytes(length)?;
            let mut buffer = vec![0u8; length];
            if length > 0 {
                if let Some(data_ptr) = array_buffer.data() {
                    unsafe {
                        ptr::copy_nonoverlapping(
                            data_ptr.as_ptr() as *const u8,
                            buffer.as_mut_ptr(),
                            length,
                        );
                    }
                }
            }
            Ok(JSValue::Bytes(buffer))
        } else if value.is_array() {
            // Check for a circular reference: is this object already on the
            // current traversal path?
            let obj = v8::Local::<v8::Object>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast array to object"))?;

            if seen
                .iter()
                .any(|ancestor| ancestor.strict_equals(obj.into()))
            {
                return Err(RuntimeError::internal(
                    "Cannot serialize circular reference",
                ));
            }
            seen.push(obj);

            let array = v8::Local::<v8::Array>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to array"))?;
            let len = array.length() as usize;

            // Charge for the array *before* touching its elements, exactly as
            // the `Set` branch below does. Without this the array branch was
            // the one container that cost nothing: every element of a sparse
            // array is a hole, holes convert to `Undefined`, and `Undefined`
            // charges `add_bytes(0)`. So `a[10_000_000] = 1` -- a 40-character
            // guest script -- produced a ten-million-element Python list while
            // `max_serialization_bytes` (10 MB by default) saw zero bytes
            // consumed, and `a[4294967294] = 1` reached `Vec::with_capacity`
            // with a guest-chosen four-billion-element reservation and took
            // the host process out with SIGKILL before any limit was ever
            // consulted.
            //
            // `array.length()` is guest-controlled, so it is metered first and
            // only then used as a capacity hint, and the hint is clamped: a
            // length that passes the byte check is small enough to reserve,
            // but a tracker configured with a very large `max_bytes` must
            // still not turn one `length` read into one enormous allocation.
            tracker.add_bytes(16)?;
            tracker.add_bytes(len.saturating_mul(size_of::<usize>()))?;

            const ARRAY_CAPACITY_HINT_CAP: usize = 4096;
            let mut items = Vec::with_capacity(len.min(ARRAY_CAPACITY_HINT_CAP));
            for i in 0..len {
                let idx = i as u32;
                let item = array.get_index(scope, idx).ok_or_else(|| {
                    RuntimeError::internal(format!("Failed to get array index {}", i))
                })?;
                items.push(Self::value_to_js_value_internal(
                    fn_registry,
                    next_fn_id,
                    scope,
                    item,
                    seen,
                    tracker,
                    None,
                    stream_registry.clone(),
                )?);
            }

            seen.pop();
            Ok(JSValue::Array(items))
        } else if value.is_set() {
            let obj = v8::Local::<v8::Object>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast set to object"))?;

            if seen
                .iter()
                .any(|ancestor| ancestor.strict_equals(obj.into()))
            {
                return Err(RuntimeError::internal(
                    "Cannot serialize circular reference",
                ));
            }
            seen.push(obj);

            let set = v8::Local::<v8::Set>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Set"))?;
            let entries = set.as_array(scope);
            let len = entries.length() as usize;

            tracker.add_bytes(24)?;
            tracker.add_bytes(len.saturating_mul(size_of::<usize>()))?;

            let mut values = Vec::with_capacity(len);
            for index in 0..len {
                let element = entries
                    .get_index(scope, index as u32)
                    .ok_or_else(|| RuntimeError::internal("Failed to get Set entry"))?;
                values.push(Self::value_to_js_value_internal(
                    fn_registry,
                    next_fn_id,
                    scope,
                    element,
                    seen,
                    tracker,
                    None,
                    stream_registry.clone(),
                )?);
            }

            seen.pop();
            Ok(JSValue::Set(values))
        } else if value.is_date() {
            let date = v8::Local::<v8::Date>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Date"))?;
            let epoch_ms = date.value_of();
            if !epoch_ms.is_finite() || epoch_ms < i64::MIN as f64 || epoch_ms > i64::MAX as f64 {
                return Err(RuntimeError::internal("Date value out of range"));
            }
            tracker.add_bytes(16)?;
            Ok(JSValue::Date(epoch_ms.round() as i64))
        } else if value.is_object() && Self::is_readable_stream(scope, value) {
            let stream_id = stream_registry.register_stream(scope, value);
            tracker.add_bytes(size_of::<u32>())?;
            Ok(JSValue::JsStream { id: stream_id })
        } else if value.is_object() {
            // Check for a circular reference: is this object already on the
            // current traversal path?
            let obj = v8::Local::<v8::Object>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to object"))?;

            if seen
                .iter()
                .any(|ancestor| ancestor.strict_equals(obj.into()))
            {
                return Err(RuntimeError::internal(
                    "Cannot serialize circular reference",
                ));
            }
            seen.push(obj);

            // Get property names
            let prop_names = obj
                .get_own_property_names(scope, v8::GetPropertyNamesArgs::default())
                .ok_or_else(|| RuntimeError::internal("Failed to get property names"))?;

            let mut map = IndexMap::new();
            for i in 0..prop_names.length() {
                let key = prop_names
                    .get_index(scope, i)
                    .ok_or_else(|| RuntimeError::internal("Failed to get property name"))?;
                let key_str = key
                    .to_string(scope)
                    .ok_or_else(|| RuntimeError::internal("Failed to convert key to string"))?
                    .to_rust_string_lossy(scope);

                let val = obj.get(scope, key).ok_or_else(|| {
                    RuntimeError::internal(format!("Failed to get property '{}'", key_str))
                })?;

                // If the value is a function, capture the object as the receiver for 'this' binding
                let receiver_for_val = if val.is_function() {
                    let obj_as_value: v8::Local<v8::Value> = obj.into();
                    Some(v8::Global::new(scope, obj_as_value))
                } else {
                    None
                };

                tracker.add_bytes(key_str.len())?;
                map.insert(
                    key_str,
                    Self::value_to_js_value_internal(
                        fn_registry,
                        next_fn_id,
                        scope,
                        val,
                        seen,
                        tracker,
                        receiver_for_val,
                        stream_registry.clone(),
                    )?,
                );
            }

            seen.pop();
            Ok(JSValue::Object(map))
        } else {
            // Fallback: convert to string
            let string = value
                .to_string(scope)
                .ok_or_else(|| RuntimeError::internal("Failed to convert value to string"))?;
            let rust_str = string.to_rust_string_lossy(scope);
            tracker.add_bytes(rust_str.len())?;
            Ok(JSValue::String(rust_str))
        };

        tracker.exit();
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dispatcher_exits_when_channel_closes() {
        let baseline = active_runtime_threads();
        let (cmd_tx, termination, _, _) =
            spawn_runtime_thread(RuntimeConfig::default()).expect("spawn runtime");
        assert_eq!(
            active_runtime_threads(),
            baseline + 1,
            "runtime thread should register"
        );

        drop(cmd_tx);

        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if active_runtime_threads() == baseline {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }

        assert_eq!(
            active_runtime_threads(),
            baseline,
            "runtime thread should exit after command channel closes"
        );
        drop(termination);
    }
}
