//! Async jobs: state machines the dispatcher polls between event-loop steps.

use super::convert::{caught_call_error, CallError};
use super::core::{PendingFunctionCall, RuntimeCoreState};
use super::termination::WatchdogToken;
use crate::runtime::error::{RuntimeError, RuntimeResult};
use crate::runtime::js_value::JSValue;
use crate::runtime::stats::RuntimeCallKind;
use crate::runtime::stream::StreamChunk;
use deno_core::error::{CoreError, JsError};
use deno_core::{v8, ModuleId};
use pyo3_async_runtimes::TaskLocals;
use std::future::Future;
use std::pin::Pin;
use std::task::Poll;
use std::time::{Duration, Instant};
use tokio::sync::oneshot;

pub(super) type Responder = oneshot::Sender<RuntimeResult<JSValue>>;

/// An async job, advanced one step per `poll` without holding borrows of the core.
pub(super) trait RuntimeJob {
    fn common(&self) -> &JobCommon;

    /// Advance one tick; `Ready` when complete.
    fn poll(&mut self, core: &mut RuntimeCoreState) -> Poll<RuntimeResult<JSValue>>;

    /// Consume the job, resolving its watchdog and answering the caller.
    fn finish(self: Box<Self>, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>);

    fn kind(&self) -> RuntimeCallKind {
        self.common().kind
    }

    fn start_time(&self) -> Instant {
        self.common().start_time
    }

    /// When this job's own `poll` will time out. Timeouts are enforced inside
    /// `poll`, so the dispatcher must bound its park by this.
    fn deadline(&self) -> Option<Instant> {
        self.common().deadline
    }
}

/// The per-job wording of timeout messages: `reason` goes to the termination
/// controller, `error` + `error_suffix` to the caller's `RuntimeError::timeout`.
#[derive(Clone, Copy)]
pub(super) struct TimeoutWording {
    reason: &'static str,
    error: &'static str,
    error_suffix: &'static str,
    /// Context for `apply_watchdog_result`.
    watchdog_context: &'static str,
}

impl TimeoutWording {
    const EVAL: Self = Self {
        reason: "Asynchronous evaluation",
        error: "Evaluation",
        error_suffix: " (promise still pending)",
        watchdog_context: "Async evaluation",
    };
    const EVAL_MODULE: Self = Self {
        reason: "Asynchronous module evaluation",
        error: "Module evaluation",
        error_suffix: "",
        watchdog_context: "Async module evaluation",
    };
    const CALL_FUNCTION: Self = Self {
        reason: "Asynchronous function call",
        error: "Function call",
        error_suffix: "",
        watchdog_context: "Async function call",
    };
    /// Stream reads have no deadline, so only the context is ever used.
    const STREAM_READ: Self = Self {
        reason: "",
        error: "",
        error_suffix: "",
        watchdog_context: "Stream read",
    };
}

/// State every async job carries, plus the deadline check and task-locals
/// installation every `poll` opens with.
pub(super) struct JobCommon {
    timeout_ms: Option<u64>,
    task_locals: Option<TaskLocals>,
    responder: Responder,
    start_time: Instant,
    deadline: Option<Instant>,
    watchdog: Option<WatchdogToken>,
    kind: RuntimeCallKind,
    wording: TimeoutWording,
    /// Set when `expired` asked V8 to terminate, so `respond` clears it.
    terminated_by_deadline: bool,
}

impl JobCommon {
    #[allow(clippy::too_many_arguments)]
    fn new(
        kind: RuntimeCallKind,
        wording: TimeoutWording,
        timeout_ms: Option<u64>,
        start_time: Instant,
        deadline: Option<Instant>,
        task_locals: Option<TaskLocals>,
        responder: Responder,
        watchdog: Option<WatchdogToken>,
    ) -> Self {
        Self {
            timeout_ms,
            task_locals,
            responder,
            start_time,
            deadline,
            watchdog,
            kind,
            wording,
            terminated_by_deadline: false,
        }
    }

    /// A job whose clock starts now.
    fn starting(
        kind: RuntimeCallKind,
        wording: TimeoutWording,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: Responder,
        watchdog: Option<WatchdogToken>,
    ) -> Self {
        let start_time = Instant::now();
        let deadline = timeout_ms.map(|ms| start_time + Duration::from_millis(ms));
        Self::new(
            kind,
            wording,
            timeout_ms,
            start_time,
            deadline,
            task_locals,
            responder,
            watchdog,
        )
    }

    /// The deadline error, if it has passed, after asking V8 to stop.
    ///
    /// This termination is the job's own and `respond` must cancel it: this
    /// check routinely beats the watchdog thread to the same deadline, in which
    /// case `disarm` reports `false` and nothing else would, leaving the
    /// isolate latched for every later call.
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

    fn install_task_locals(&self, core: &mut RuntimeCoreState) {
        if self.task_locals.is_some() {
            core.set_task_locals(self.task_locals.clone());
        }
    }

    fn respond(mut self, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>) {
        let result =
            core.apply_watchdog_result(result, self.watchdog.take(), self.wording.watchdog_context);
        // Whatever `expired` latched has nothing left to stop; clear it or it
        // stops the next call. Safe: one isolate, one thread, nothing else running.
        if self.terminated_by_deadline {
            core.cancel_pending_termination();
        }
        let _ = self.responder.send(result);
    }
}

/// Produces the promise a [`PromiseJob`] waits on; runs once, on the first poll.
type PromiseStart =
    Box<dyn FnOnce(&mut RuntimeCoreState) -> RuntimeResult<v8::Global<v8::Promise>>>;

/// Post-processing of a fulfilled value (stream-read bookkeeping).
type FulfilledHook = Box<dyn FnOnce(&mut RuntimeCoreState, JSValue) -> RuntimeResult<JSValue>>;

/// Start something that yields a JS promise, then wait for it while the
/// dispatcher drives the event loop. Async eval, stream reads and (resumed)
/// function calls differ only in `start` and `on_fulfilled`.
pub(super) struct PromiseJob {
    common: JobCommon,
    start: Option<PromiseStart>,
    on_fulfilled: Option<FulfilledHook>,
    promise: Option<v8::Global<v8::Promise>>,
    done: bool,
}

impl PromiseJob {
    fn new(common: JobCommon, start: PromiseStart) -> Self {
        Self {
            common,
            start: Some(start),
            on_fulfilled: None,
            promise: None,
            done: false,
        }
    }

    /// Wrap `value` in a resolved promise unless it already is one, so plain
    /// values and promises share one waiting path.
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

    fn settle(&mut self, result: RuntimeResult<JSValue>) -> Poll<RuntimeResult<JSValue>> {
        self.done = true;
        Poll::Ready(result)
    }
}

impl RuntimeJob for PromiseJob {
    fn common(&self) -> &JobCommon {
        &self.common
    }

    fn poll(&mut self, core: &mut RuntimeCoreState) -> Poll<RuntimeResult<JSValue>> {
        if let Some(err) = self.common.expired(core) {
            return Poll::Ready(Err(err));
        }
        if self.done {
            return Poll::Ready(Err(RuntimeError::internal("Job already completed")));
        }

        let Some(promise) = &self.promise else {
            self.common.install_task_locals(core);
            let Some(start) = self.start.take() else {
                return self.settle(Err(RuntimeError::internal("Job started more than once")));
            };
            return match start(core) {
                Ok(promise) => {
                    // The dispatcher gives a fresh job one un-waited iteration,
                    // so an already-resolved promise costs no extra latency.
                    self.promise = Some(promise);
                    Poll::Pending
                }
                Err(err) => self.settle(Err(err)),
            };
        };

        let state = {
            deno_core::scope!(scope, core.js_runtime);
            v8::Local::new(scope, promise).state()
        };
        let result = match state {
            v8::PromiseState::Pending => return Poll::Pending,
            v8::PromiseState::Fulfilled => {
                let conv = core.conv.clone();
                let value = {
                    deno_core::scope!(scope, core.js_runtime);
                    let result = v8::Local::new(scope, promise).result(scope);
                    conv.to_js_value(scope, result)
                };
                match (value, self.on_fulfilled.take()) {
                    (Ok(value), Some(hook)) => hook(core, value),
                    (other, _) => other,
                }
            }
            v8::PromiseState::Rejected => {
                let js_error = {
                    deno_core::scope!(scope, core.js_runtime);
                    let exception = v8::Local::new(scope, promise).result(scope);
                    *JsError::from_v8_exception(scope, exception)
                };
                Err(core.translate_js_error(js_error))
            }
        };
        self.settle(result)
    }

    fn finish(self: Box<Self>, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>) {
        self.common.respond(core, result);
    }
}

/// Async evaluation: run the script, then await whatever it produced.
pub(super) fn eval_async_job(
    code: String,
    timeout_ms: Option<u64>,
    task_locals: Option<TaskLocals>,
    responder: Responder,
    watchdog: Option<WatchdogToken>,
) -> PromiseJob {
    let common = JobCommon::starting(
        RuntimeCallKind::EvalAsync,
        TimeoutWording::EVAL,
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

/// One read from a JS `ReadableStream`, plus the registry bookkeeping.
pub(super) fn stream_read_job(stream_id: u32, responder: Responder) -> PromiseJob {
    let common = JobCommon::starting(
        RuntimeCallKind::EvalAsync,
        TimeoutWording::STREAM_READ,
        None,
        None,
        responder,
        None,
    );
    let mut job = PromiseJob::new(
        common,
        Box::new(move |core: &mut RuntimeCoreState| {
            let streams = core.conv.streams.clone();
            deno_core::scope!(scope, core.js_runtime);
            streams.start_read(scope, stream_id)
        }),
    );
    job.on_fulfilled = Some(Box::new(move |core, value| {
        let chunk = StreamChunk::from_js_value(value)?;
        let streams = &core.conv.streams;
        streams.update_stats_after_chunk(stream_id, &chunk);
        if chunk.done {
            streams.release(stream_id);
        }
        Ok(chunk.to_js_value())
    }));
    job
}

/// An async call of a stored JS function: call it, then await the result.
pub(super) fn call_function_async_job(
    fn_id: u32,
    args: Vec<JSValue>,
    timeout_ms: Option<u64>,
    task_locals: Option<TaskLocals>,
    responder: Responder,
    core: &RuntimeCoreState,
) -> PromiseJob {
    // An explicit `timeout=` wins over the runtime-wide execution timeout.
    let effective_timeout = core.effective_timeout_ms(timeout_ms);
    // Armed for the job's whole lifetime: `expired` only runs between
    // event-loop steps, so without it JS spinning inside the call hangs.
    let watchdog = core.arm_watchdog(effective_timeout, "Asynchronous function call timed out");
    let common = JobCommon::starting(
        RuntimeCallKind::CallFunctionAsync,
        TimeoutWording::CALL_FUNCTION,
        effective_timeout,
        task_locals,
        responder,
        watchdog,
    );

    PromiseJob::new(
        common,
        Box::new(move |core: &mut RuntimeCoreState| {
            if !core.conv.fn_registry.borrow().contains_key(&fn_id) {
                return Err(RuntimeError::internal(format!(
                    "Function ID {} not found",
                    fn_id
                )));
            }
            let conv = core.conv.clone();
            let promise: Result<v8::Global<v8::Promise>, CallError> = (|| {
                deno_core::scope!(scope, core.js_runtime);
                v8::tc_scope!(let try_catch, scope);
                match conv.call_stored(try_catch, fn_id, &args)? {
                    Some(value) => {
                        let promise = PromiseJob::as_promise(try_catch, value)?;
                        Ok(v8::Global::new(try_catch, promise))
                    }
                    None => Err(caught_call_error!(try_catch)),
                }
            })();
            promise.map_err(|err| core.translate_call_error(err))
        }),
    )
}

/// Resume a sync call that returned a pending promise, on the original clock
/// (restarting it would silently double the caller's timeout).
pub(super) fn resume_function_call_job(
    pending: PendingFunctionCall,
    task_locals: Option<TaskLocals>,
    responder: Responder,
    core: &RuntimeCoreState,
) -> PromiseJob {
    // Armed for what is left of the clock, but reporting the original timeout.
    let watchdog = pending.deadline.map(|deadline| {
        let mut token = core.watchdog.arm(
            deadline.saturating_duration_since(Instant::now()),
            "Asynchronous function call timed out",
        );
        if let Some(ms) = pending.timeout_ms {
            token.duration = Duration::from_millis(ms);
        }
        token
    });
    let common = JobCommon::new(
        RuntimeCallKind::CallFunctionAsync,
        TimeoutWording::CALL_FUNCTION,
        pending.timeout_ms,
        pending.start_time,
        pending.deadline,
        task_locals,
        responder,
        watchdog,
    );
    let promise = pending.promise;
    PromiseJob::new(common, Box::new(move |_core| Ok(promise)))
}

type ModuleEvaluation = Pin<Box<dyn Future<Output = Result<(), CoreError>>>>;

/// Async module evaluation: waits on `mod_evaluate`'s Rust future (no promise),
/// then extracts the namespace. Shares only [`JobCommon`] with [`PromiseJob`].
pub(super) struct EvalModuleAsyncJob {
    specifier: String,
    common: JobCommon,
    state: EvalModuleState,
}

enum EvalModuleState {
    Init,
    Evaluating {
        module_id: ModuleId,
        receiver: ModuleEvaluation,
    },
    WaitingNamespace {
        module_id: ModuleId,
    },
    Done,
}

impl EvalModuleAsyncJob {
    pub(super) fn new(
        specifier: String,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
        responder: Responder,
        watchdog: Option<WatchdogToken>,
    ) -> Self {
        Self {
            specifier,
            common: JobCommon::starting(
                RuntimeCallKind::EvalModuleAsync,
                TimeoutWording::EVAL_MODULE,
                timeout_ms,
                task_locals,
                responder,
                watchdog,
            ),
            state: EvalModuleState::Init,
        }
    }
}

impl RuntimeJob for EvalModuleAsyncJob {
    fn common(&self) -> &JobCommon {
        &self.common
    }

    fn poll(&mut self, core: &mut RuntimeCoreState) -> Poll<RuntimeResult<JSValue>> {
        if let Some(err) = self.common.expired(core) {
            return Poll::Ready(Err(err));
        }

        match &mut self.state {
            EvalModuleState::Init => {
                self.common.install_task_locals(core);
                let module_id = core.load_module(&self.specifier)?;
                self.state = EvalModuleState::Evaluating {
                    module_id,
                    receiver: Box::pin(core.js_runtime.mod_evaluate(module_id)),
                };
                Poll::Pending
            }
            EvalModuleState::Evaluating {
                module_id,
                receiver,
            } => {
                // The dispatcher drives the event loop; just check the future.
                let mut cx = std::task::Context::from_waker(futures::task::noop_waker_ref());
                match receiver.as_mut().poll(&mut cx) {
                    Poll::Ready(Err(err)) => {
                        self.state = EvalModuleState::Done;
                        Poll::Ready(Err(core.translate_core_error(err)))
                    }
                    Poll::Ready(Ok(())) => {
                        self.state = EvalModuleState::WaitingNamespace {
                            module_id: *module_id,
                        };
                        Poll::Pending
                    }
                    Poll::Pending => Poll::Pending,
                }
            }
            EvalModuleState::WaitingNamespace { module_id } => {
                let result = core.module_namespace(*module_id);
                self.state = EvalModuleState::Done;
                Poll::Ready(result)
            }
            EvalModuleState::Done => {
                Poll::Ready(Err(RuntimeError::internal("Job already completed")))
            }
        }
    }

    fn finish(self: Box<Self>, core: &mut RuntimeCoreState, result: RuntimeResult<JSValue>) {
        self.common.respond(core, result);
    }
}
