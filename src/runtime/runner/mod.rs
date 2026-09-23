//! Runtime thread backed by `deno_core::JsRuntime`.
//!
//! The engine runs on a dedicated OS thread with a single-threaded Tokio
//! runtime; commands from Python arrive as [`RuntimeCommand`]s.
//!
//! - [`termination`]: termination state and the deadline watchdog thread
//! - [`dispatcher`]: the main loop and command handling
//! - [`jobs`]: async job state machines
//! - [`core`]: the isolate and its synchronous entry points
//! - [`convert`]: V8 <-> [`JSValue`] conversion

mod convert;
mod core;
mod dispatcher;
mod jobs;
mod termination;

pub use termination::TerminationController;

use crate::runtime::config::RuntimeConfig;
use crate::runtime::error::{RuntimeError, RuntimeResult};
use crate::runtime::handle::BoundObjectProperty;
use crate::runtime::inspector::{InspectorConnectionState, InspectorMetadata};
use crate::runtime::js_value::{record_stack_anchor, JSValue, RUNTIME_THREAD_STACK_SIZE};
use crate::runtime::ops::{OpToken, PythonOpMode};
use crate::runtime::stats::RuntimeStatsSnapshot;
use crate::runtime::stream::PyStreamRegistry;
use core::RuntimeCoreState;
use dispatcher::RuntimeDispatcher;
use pyo3::prelude::Py;
use pyo3::PyAny;
use pyo3_async_runtimes::TaskLocals;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::Sender;
use std::thread;
use tokio::sync::mpsc;
use tokio::sync::oneshot;

type InspectorInfo = Option<(InspectorMetadata, InspectorConnectionState)>;
type RuntimeInitResult = RuntimeResult<(TerminationController, InspectorInfo, PyStreamRegistry)>;
type SpawnRuntimeResult = (
    mpsc::UnboundedSender<RuntimeCommand>,
    TerminationController,
    InspectorInfo,
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

/// Outcome of calling a JS function synchronously.
pub enum FunctionCallResult {
    /// Function returned a non-promise value immediately.
    Immediate(JSValue),
    /// Function returned a promise; call ID can be used to resume.
    Pending { call_id: u64 },
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

pub fn spawn_runtime_thread(config: RuntimeConfig) -> RuntimeResult<SpawnRuntimeResult> {
    let (cmd_tx, cmd_rx) = mpsc::unbounded_channel::<RuntimeCommand>();
    let (init_tx, init_rx) = std::sync::mpsc::channel::<RuntimeInitResult>();

    thread::Builder::new()
        .name("pydeno-deno-runtime".to_string())
        // Owns the isolate and runs the recursive JSValue serializers; see
        // `RUNTIME_THREAD_STACK_SIZE`.
        .stack_size(RUNTIME_THREAD_STACK_SIZE)
        .spawn(move || {
            let _thread_guard = RuntimeThreadGuard::new();
            // Recorded while the stack is still shallow, so `LimitTracker` can
            // measure real stack use by deep conversions.
            record_stack_anchor();
            let tokio_rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("failed to build tokio runtime");

            // `JsRuntime::new` must see an entered tokio runtime: otherwise V8
            // background compilation of large scripts (~148KB+) reaches
            // deno_core's `spawn_delayed_task` with no handle and aborts.
            let tokio_enter = tokio_rt.enter();
            let core = match RuntimeCoreState::new(config) {
                Ok(core) => {
                    let _ = init_tx.send(Ok((
                        core.termination.clone(),
                        core.inspector_info(),
                        core.py_stream_registry.clone(),
                    )));
                    core
                }
                Err(err) => {
                    let _ = init_tx.send(Err(err));
                    return;
                }
            };
            drop(tokio_enter);

            tokio_rt.block_on(RuntimeDispatcher::new(core, cmd_rx).run());
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

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
