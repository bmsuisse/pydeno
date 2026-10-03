//! Python-facing handle for interacting with the runtime thread.

use crate::runtime::config::RuntimeConfig;
use crate::runtime::error::{RuntimeError, RuntimeResult};
use crate::runtime::inspector::{InspectorConnectionState, InspectorMetadata};
use crate::runtime::js_value::{JSValue, SerializationLimits};
use crate::runtime::ops::{OpToken, PythonOpMode};
use crate::runtime::runner::{
    spawn_runtime_thread, FunctionCallResult, RuntimeCommand, TerminationController,
};
use crate::runtime::stats::RuntimeStatsSnapshot;
use crate::runtime::stream::{PyStreamRegistry, StreamChunk};
use pyo3::prelude::Py;
use pyo3::PyAny;
use pyo3_async_runtimes::{tokio as pyo3_tokio, TaskLocals};
use std::collections::HashSet;
use std::sync::mpsc;
use std::sync::Arc;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tokio::sync::mpsc as async_mpsc;
use tokio::sync::oneshot;

type IdSet = Arc<Mutex<HashSet<u32>>>;

/// Thread-safe handle for communicating with a JavaScript runtime thread.
///
/// Each `RuntimeHandle` owns a channel to a dedicated runtime thread running a V8 isolate
/// and Tokio event loop. The handle can be cloned to share access across threads, and commands
/// are sent via an unbounded async channel.
///
/// The handle does NOT automatically shut down on drop - callers must explicitly call
/// [`RuntimeHandle::close`] or [`RuntimeHandle::terminate`] to clean up resources.
#[derive(Clone)]
pub struct RuntimeHandle {
    /// Command channel to the runtime thread (None after shutdown).
    tx: Option<async_mpsc::UnboundedSender<RuntimeCommand>>,
    /// Shutdown state shared across clones.
    shutdown: Arc<Mutex<bool>>,
    termination: TerminationController,
    /// Tracked JS function handles / JS stream IDs / Python stream IDs (for cleanup).
    tracked_functions: IdSet,
    tracked_js_streams: IdSet,
    tracked_py_streams: IdSet,
    inspector_metadata: Arc<Mutex<Option<InspectorMetadata>>>,
    inspector_connection: Option<InspectorConnectionState>,
    serialization_limits: SerializationLimits,
    /// Registry for Python async iterables exposed as JS streams.
    py_stream_registry: PyStreamRegistry,
    /// Grace period before a blocked caller abandons an unresponsive runtime,
    /// or `None` to wait forever (the default -- see `recv_result`).
    force_kill_grace: Option<Duration>,
}

/// How often a blocked caller re-checks for a requested termination. A timed
/// park (`recv_timeout`), not a spin; deliberately much smaller than the grace.
const FORCE_KILL_POLL_SLICE: Duration = Duration::from_millis(5);

/// Represents a property assignment when binding Python objects into the JS global namespace.
#[derive(Debug)]
pub(crate) enum BoundObjectProperty {
    Value {
        key: String,
        value: JSValue,
    },
    Op {
        key: String,
        op_id: OpToken,
        mode: PythonOpMode,
    },
}

impl RuntimeHandle {
    /// Spawn a new runtime thread with the given configuration.
    ///
    /// # Errors
    /// Returns an error if the runtime thread fails to start or initialize.
    pub fn spawn(config: RuntimeConfig) -> RuntimeResult<Self> {
        let serialization_limits = config.serialization_limits();
        let force_kill_grace = config.force_kill_grace;
        let (tx, termination, inspector_info, py_stream_registry) = spawn_runtime_thread(config)?;
        let (metadata, connection) = inspector_info.unzip();
        let tracked_py_streams: IdSet = Arc::default();
        let tracked = Arc::downgrade(&tracked_py_streams);
        py_stream_registry.add_release_listener(move |stream_id| {
            if let Some(set) = tracked.upgrade() {
                set.lock().unwrap().remove(&stream_id);
            }
        });
        Ok(Self {
            tx: Some(tx),
            shutdown: Arc::new(Mutex::new(false)),
            termination,
            tracked_functions: Arc::default(),
            tracked_js_streams: Arc::default(),
            tracked_py_streams,
            inspector_metadata: Arc::new(Mutex::new(metadata)),
            inspector_connection: connection,
            serialization_limits,
            py_stream_registry,
            force_kill_grace,
        })
    }

    /// The command sender, or an error if the runtime is terminated or shut down.
    fn sender(&self) -> RuntimeResult<&async_mpsc::UnboundedSender<RuntimeCommand>> {
        if self.termination.is_requested() || self.termination.is_terminated() {
            return Err(self.termination.terminated_error());
        }
        if *self.shutdown.lock().unwrap() {
            return Err(RuntimeError::internal("Runtime has been shut down"));
        }
        self.tx
            .as_ref()
            .ok_or_else(|| RuntimeError::internal("Runtime has been shut down"))
    }

    fn send(&self, command: RuntimeCommand, what: &str) -> RuntimeResult<()> {
        self.sender()?
            .send(command)
            .map_err(|_| RuntimeError::internal(format!("Failed to send {what} command")))
    }

    /// Send a command with a std responder and block on it via [`Self::recv_result`].
    fn request<T>(
        &self,
        what: &str,
        recv_what: &str,
        command: impl FnOnce(mpsc::Sender<RuntimeResult<T>>) -> RuntimeCommand,
    ) -> RuntimeResult<T> {
        let (tx, rx) = mpsc::channel();
        self.send(command(tx), what)?;
        self.recv_result(&rx, recv_what)?
    }

    /// Send a command with a oneshot responder and await its reply.
    async fn request_async<T>(
        &self,
        what: &str,
        recv_error: &'static str,
        command: impl FnOnce(oneshot::Sender<RuntimeResult<T>>) -> RuntimeCommand,
    ) -> RuntimeResult<T> {
        let (tx, rx) = oneshot::channel();
        self.send(command(tx), what)?;
        rx.await.map_err(|_| RuntimeError::internal(recv_error))?
    }

    /// Mark this handle terminated and shut down, abandoning the runtime thread.
    fn force_kill(&self, message: String) -> RuntimeError {
        self.termination.force_mark_terminated();
        *self.shutdown.lock().unwrap() = true;
        RuntimeError::force_killed(message)
    }

    /// Block for a command's result, escalating to a force-kill if the runtime
    /// stops answering after a termination has been requested.
    ///
    /// Both polite kill tiers (V8 `terminate_execution()` and the dispatcher's
    /// termination-flag check) need the runtime thread to run, so neither fires
    /// when it is wedged in a host call that never returns (e.g. a blocking
    /// sync `bind_function` handler). With `force_kill_grace` set this waits in
    /// [`FORCE_KILL_POLL_SLICE`] slices, and once a termination is requested
    /// gives the runtime `force_kill_grace` to acknowledge it before returning
    /// [`RuntimeError::ForceKilled`].
    ///
    /// Opt-in because `recv_timeout` measurably slows this hot path (~10% per
    /// bound-function call); with `None` it is a plain `rx.recv()`.
    ///
    /// The wedged thread is abandoned, not reclaimed (a V8 isolate cannot be
    /// dropped from another thread), and no replacement isolate is swapped in:
    /// the `Runtime` is permanently dead and the caller must create a new one.
    fn recv_result<T>(&self, rx: &mpsc::Receiver<T>, what: &str) -> RuntimeResult<T> {
        let recv_failed = || RuntimeError::internal(format!("Failed to receive {what} result"));
        let Some(grace) = self.force_kill_grace else {
            return rx.recv().map_err(|_| recv_failed());
        };

        let mut termination_seen_at = None;
        loop {
            match rx.recv_timeout(FORCE_KILL_POLL_SLICE) {
                Ok(value) => return Ok(value),
                Err(mpsc::RecvTimeoutError::Disconnected) => return Err(recv_failed()),
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if !self.termination.is_requested() {
                        continue;
                    }
                    let started = *termination_seen_at.get_or_insert_with(Instant::now);
                    if started.elapsed() < grace {
                        continue;
                    }
                    log::warn!(
                        "Runtime did not acknowledge termination within {grace:?}; abandoning \
                         the runtime thread and failing {what} with ForceKilled"
                    );
                    return Err(self.force_kill(format!(
                        "Runtime did not acknowledge termination within {grace:?} and was \
                         force-killed during {what}; the runtime thread has been abandoned \
                         and this Runtime is no longer usable -- create a new one"
                    )));
                }
            }
        }
    }

    /// Evaluate JavaScript code synchronously in the global scope.
    ///
    /// # Errors
    /// Returns an error if the code throws an exception or the runtime is shut down.
    pub fn eval_sync(&self, code: &str) -> RuntimeResult<JSValue> {
        self.request("eval", "eval", |responder| RuntimeCommand::Eval {
            code: code.to_string(),
            responder,
        })
    }

    /// Evaluate JavaScript code asynchronously with optional timeout, awaiting
    /// a returned promise. `task_locals` provides the asyncio context for Python ops.
    ///
    /// # Errors
    /// Returns an error if the code throws, times out, or the runtime is shut down.
    pub async fn eval_async(
        &self,
        code: &str,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
    ) -> RuntimeResult<JSValue> {
        self.request_async(
            "eval_async",
            "Failed to receive async eval result",
            |responder| RuntimeCommand::EvalAsync {
                code: code.to_string(),
                timeout_ms,
                task_locals,
                responder,
            },
        )
        .await
    }

    /// Register a Python callable as an op and return its capability token.
    ///
    /// The op is **not** callable from JavaScript until [`Self::set_op_exposure`]
    /// blesses the token -- see the `ops` module docs.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down or registration fails.
    pub fn register_op(
        &self,
        name: String,
        mode: PythonOpMode,
        handler: Py<PyAny>,
    ) -> RuntimeResult<OpToken> {
        self.request("register_op", "op registration", |responder| {
            RuntimeCommand::RegisterPythonOp {
                name,
                mode,
                handler,
                responder,
            }
        })
    }

    /// Expose or revoke an op capability.
    ///
    /// The bind paths expose *after* the binding is installed, so a half-failed
    /// binding leaves nothing callable. Revoking drops the handler and makes any
    /// global left by `bind_function` inert. Returns `false` if the token was
    /// not registered on this runtime.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down or the command fails.
    pub fn set_op_exposure(&self, op_id: OpToken, exposed: bool) -> RuntimeResult<bool> {
        let (responder, rx) = mpsc::channel();
        self.send(
            RuntimeCommand::SetPythonOpExposure {
                op_id,
                exposed,
                responder,
            },
            "op exposure",
        )?;
        rx.recv()
            .map_err(|_| RuntimeError::internal("Failed to receive op exposure result"))?
    }

    /// Set a custom Python resolver for module specifier resolution.
    ///
    /// The resolver receives `(specifier, referrer)` and returns a resolved URL or None.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down or the command fails.
    pub fn set_module_resolver(&self, handler: Py<PyAny>) -> RuntimeResult<()> {
        let what = "set_module_resolver";
        self.request(what, what, |responder| RuntimeCommand::SetModuleResolver {
            handler,
            responder,
        })
    }

    /// Set a custom Python loader for fetching module source code.
    ///
    /// The loader receives a resolved specifier and returns the module source.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down or the command fails.
    pub fn set_module_loader(&self, handler: Py<PyAny>) -> RuntimeResult<()> {
        let what = "set_module_loader";
        self.request(what, what, |responder| RuntimeCommand::SetModuleLoader {
            handler,
            responder,
        })
    }

    /// Register a static ES module that can be imported without a custom loader.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down or the command fails.
    pub fn add_static_module(&self, name: String, source: String) -> RuntimeResult<()> {
        let what = "add_static_module";
        self.request(what, what, |responder| RuntimeCommand::AddStaticModule {
            name,
            source,
            responder,
        })
    }

    /// Bind a Python object (values and ops) to the JavaScript global namespace.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down or the command fails.
    pub(crate) fn bind_object(
        &self,
        name: String,
        properties: Vec<BoundObjectProperty>,
    ) -> RuntimeResult<()> {
        let what = "bind_object";
        self.request(what, what, |responder| RuntimeCommand::BindObject {
            name,
            properties,
            responder,
        })
    }

    /// Evaluate an ES module synchronously and return its namespace object.
    ///
    /// # Errors
    /// Returns an error if the module fails to load/evaluate or the runtime is shut down.
    pub fn eval_module_sync(&self, specifier: &str) -> RuntimeResult<JSValue> {
        let what = "eval_module";
        self.request(what, what, |responder| RuntimeCommand::EvalModule {
            specifier: specifier.to_string(),
            responder,
        })
    }

    /// Evaluate an ES module asynchronously with optional timeout, waiting for
    /// top-level await if present.
    ///
    /// # Errors
    /// Returns an error if the module fails, times out, or the runtime is shut down.
    pub async fn eval_module_async(
        &self,
        specifier: &str,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
    ) -> RuntimeResult<JSValue> {
        self.request_async(
            "eval_module_async",
            "Failed to receive async eval_module result",
            |responder| RuntimeCommand::EvalModuleAsync {
                specifier: specifier.to_string(),
                timeout_ms,
                task_locals,
                responder,
            },
        )
        .await
    }

    /// Call a JavaScript function synchronously with optional timeout.
    ///
    /// If the function returns a promise, returns `FunctionCallResult::Pending`
    /// with a call ID for [`Self::resume_function_call`].
    ///
    /// # Errors
    /// Returns an error if the function throws or the runtime is shut down.
    pub fn call_function_sync(
        &self,
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
    ) -> RuntimeResult<FunctionCallResult> {
        self.request("call_function_sync", "function call", |responder| {
            RuntimeCommand::CallFunctionSync {
                fn_id,
                args,
                timeout_ms,
                responder,
            }
        })
    }

    /// Call a JavaScript function asynchronously with optional timeout,
    /// including promise resolution.
    ///
    /// # Errors
    /// Returns an error if the function throws, times out, or the runtime is shut down.
    pub async fn call_function_async(
        &self,
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
        task_locals: Option<TaskLocals>,
    ) -> RuntimeResult<JSValue> {
        self.request_async(
            "call_function",
            "Failed to receive function call result",
            |responder| RuntimeCommand::CallFunctionAsync {
                fn_id,
                args,
                timeout_ms,
                task_locals,
                responder,
            },
        )
        .await
    }

    /// Resume polling a pending function call returned by `call_function_sync`.
    ///
    /// # Errors
    /// Returns an error if the call ID is invalid or the runtime is shut down.
    pub async fn resume_function_call(
        &self,
        call_id: u64,
        task_locals: Option<TaskLocals>,
    ) -> RuntimeResult<JSValue> {
        self.request_async(
            "resume_function_call",
            "Failed to receive resumed function call result",
            |responder| RuntimeCommand::ResumeFunctionCall {
                call_id,
                task_locals,
                responder,
            },
        )
        .await
    }

    /// Release a function handle so the underlying V8 global can be dropped.
    pub fn release_function(&self, fn_id: u32) -> RuntimeResult<()> {
        let (responder, rx) = oneshot::channel();
        self.send(
            RuntimeCommand::ReleaseFunction { fn_id, responder },
            "release_function",
        )?;
        rx.blocking_recv()
            .map_err(|_| RuntimeError::internal("Failed to receive release result"))?
    }

    /// Async variant of [`Self::release_function`].
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down.
    pub async fn release_function_async(&self, fn_id: u32) -> RuntimeResult<()> {
        self.request_async(
            "release_function",
            "Failed to receive release result",
            |responder| RuntimeCommand::ReleaseFunction { fn_id, responder },
        )
        .await
    }

    /// Read the next chunk from a JavaScript ReadableStream (`done=true` at the end).
    ///
    /// # Errors
    /// Returns an error if the stream ID is invalid or reading fails.
    pub async fn stream_read(&self, stream_id: u32) -> RuntimeResult<StreamChunk> {
        let chunk_value = self
            .request_async(
                "stream_read",
                "Failed to receive stream chunk",
                |responder| RuntimeCommand::StreamRead {
                    stream_id,
                    responder,
                },
            )
            .await?;
        let chunk = StreamChunk::from_js_value(chunk_value)?;
        if chunk.done {
            self.untrack_js_stream_id(stream_id);
        }
        Ok(chunk)
    }

    /// Release a JavaScript stream handle (drops the V8 global and reader).
    ///
    /// # Errors
    /// Returns an error if the stream ID is invalid or the runtime is shut down.
    pub fn stream_release(&self, stream_id: u32) -> RuntimeResult<()> {
        let what = "stream_release";
        self.request(what, what, |responder| RuntimeCommand::StreamRelease {
            stream_id,
            responder,
        })?;
        self.untrack_js_stream_id(stream_id);
        Ok(())
    }

    /// Cancel a JavaScript stream and release its handle.
    ///
    /// # Errors
    /// Returns an error if the stream ID is invalid or the runtime is shut down.
    pub fn stream_cancel(&self, stream_id: u32) -> RuntimeResult<()> {
        let what = "stream_cancel";
        self.request(what, what, |responder| RuntimeCommand::StreamCancel {
            stream_id,
            responder,
        })?;
        self.untrack_js_stream_id(stream_id);
        Ok(())
    }

    /// Register a Python async iterable as a stream and return its stream ID.
    ///
    /// # Errors
    /// Returns an error if registration fails.
    pub fn register_py_stream(
        &self,
        iterable: Py<PyAny>,
        task_locals: TaskLocals,
    ) -> RuntimeResult<u32> {
        let stream_id = self
            .py_stream_registry
            .register_iterable(iterable, task_locals)?;
        self.track_py_stream_id(stream_id);
        Ok(stream_id)
    }

    /// Cancel a Python stream on a background task without blocking.
    pub fn cancel_py_stream_async(&self, stream_id: u32) {
        let registry = self.py_stream_registry.clone();
        pyo3_tokio::get_runtime().spawn(async move {
            if let Err(err) = registry.cancel(stream_id).await {
                log::debug!("PyStream cancellation for id {} failed: {}", stream_id, err);
            }
        });
        self.untrack_py_stream_id(stream_id);
    }

    /// Remove a Python stream from the registry and untrack it.
    pub fn release_py_stream(&self, stream_id: u32) {
        self.py_stream_registry.release(stream_id);
        self.untrack_py_stream_id(stream_id);
    }

    /// Get current runtime statistics snapshot.
    ///
    /// # Errors
    /// Returns an error if the runtime is shut down.
    pub fn get_stats(&self) -> RuntimeResult<RuntimeStatsSnapshot> {
        self.request("get_stats", "stats", |responder| RuntimeCommand::GetStats {
            responder,
        })
    }

    pub fn inspector_connection(&self) -> Option<InspectorConnectionState> {
        self.inspector_connection.clone()
    }

    /// Check if the runtime has been shut down or terminated.
    pub fn is_shutdown(&self) -> bool {
        self.termination.is_requested()
            || self.termination.is_terminated()
            || *self.shutdown.lock().unwrap()
    }

    /// Clone of the `Send + Sync` `TerminationController`. Unlike the
    /// `unsendable` `Runtime` pyclass, this is safe to hand to another thread,
    /// which is what lets a Python watchdog thread call `TerminationHandle.terminate()`.
    pub fn termination_controller(&self) -> TerminationController {
        self.termination.clone()
    }

    /// Forcefully terminate the runtime by canceling V8 execution.
    ///
    /// # Errors
    /// Returns an error if sending the termination command fails.
    pub fn terminate(&self) -> RuntimeResult<()> {
        if self.termination.is_terminated() {
            return Ok(());
        }
        let Some(tx) = self.tx.as_ref() else {
            *self.shutdown.lock().unwrap() = true;
            return Ok(());
        };

        self.termination.ensure_reason("Terminated by host request");
        if !self.termination.request() {
            // Someone else is already terminating; wait for them, bounded by
            // the same grace so a second caller cannot hang on a wedged thread.
            let started = Instant::now();
            while !self.termination.is_terminated() {
                if let Some(grace) = self.force_kill_grace {
                    if started.elapsed() >= grace {
                        return Err(self.force_kill(format!(
                            "Runtime did not acknowledge an in-progress termination within \
                             {grace:?} and was force-killed; the runtime thread has been \
                             abandoned and this Runtime is no longer usable -- create a new one"
                        )));
                    }
                }
                thread::sleep(Duration::from_millis(1));
            }
            return Ok(());
        }

        let (responder, rx) = mpsc::channel();
        tx.send(RuntimeCommand::Terminate { responder })
            .map_err(|_| RuntimeError::internal("Failed to send terminate command"))?;
        self.termination.terminate_execution();

        // `request()` already set the flag, so `recv_result`'s grace clock is running.
        let result = self.recv_result(&rx, "terminate confirmation")?;
        if result.is_ok() {
            *self.shutdown.lock().unwrap() = true;
        }
        result
    }

    /// Gracefully shut down the runtime thread and wait for it to exit.
    ///
    /// # Errors
    /// Returns an error if the shutdown command fails to send or confirm.
    pub fn close(&mut self) -> RuntimeResult<()> {
        let mut shutdown_guard = self.shutdown.lock().unwrap();
        log::debug!(
            "RuntimeHandle::close invoked (shutdown={}, termination_requested={}, terminated={})",
            *shutdown_guard,
            self.termination.is_requested(),
            self.termination.is_terminated()
        );
        if *shutdown_guard {
            return Ok(());
        }
        if self.termination.is_requested() || self.termination.is_terminated() {
            self.tx.take();
            *shutdown_guard = true;
            return Ok(());
        }

        if let Some(tx) = self.tx.take() {
            let (responder, rx) = mpsc::channel();
            if tx.send(RuntimeCommand::Shutdown { responder }).is_err() {
                return Err(RuntimeError::internal("Failed to send shutdown command"));
            }
            if rx.recv().is_err() {
                log::warn!("RuntimeHandle::close failed to confirm runtime shutdown");
                return Err(RuntimeError::internal("Failed to confirm runtime shutdown"));
            }
            *shutdown_guard = true;
            log::debug!("RuntimeHandle::close completed shutdown");
        }

        log::debug!("RuntimeHandle::close exit (shutdown={})", *shutdown_guard);
        Ok(())
    }

    pub fn track_function_id(&self, fn_id: u32) {
        self.tracked_functions.lock().unwrap().insert(fn_id);
    }

    pub fn untrack_function_id(&self, fn_id: u32) {
        self.tracked_functions.lock().unwrap().remove(&fn_id);
    }

    pub fn drain_tracked_function_ids(&self) -> Vec<u32> {
        self.tracked_functions.lock().unwrap().drain().collect()
    }

    pub fn track_js_stream_id(&self, stream_id: u32) {
        self.tracked_js_streams.lock().unwrap().insert(stream_id);
    }

    pub fn untrack_js_stream_id(&self, stream_id: u32) {
        self.tracked_js_streams.lock().unwrap().remove(&stream_id);
    }

    pub fn drain_tracked_js_stream_ids(&self) -> Vec<u32> {
        self.tracked_js_streams.lock().unwrap().drain().collect()
    }

    pub fn track_py_stream_id(&self, stream_id: u32) {
        self.tracked_py_streams.lock().unwrap().insert(stream_id);
    }

    pub fn untrack_py_stream_id(&self, stream_id: u32) {
        self.tracked_py_streams.lock().unwrap().remove(&stream_id);
    }

    pub fn drain_tracked_py_stream_ids(&self) -> Vec<u32> {
        self.tracked_py_streams.lock().unwrap().drain().collect()
    }

    pub fn is_function_tracked(&self, fn_id: u32) -> bool {
        self.tracked_functions.lock().unwrap().contains(&fn_id)
    }

    pub fn tracked_function_count(&self) -> usize {
        self.tracked_functions.lock().unwrap().len()
    }

    pub fn inspector_metadata(&self) -> Option<InspectorMetadata> {
        self.inspector_metadata.lock().unwrap().clone()
    }

    pub fn serialization_limits(&self) -> SerializationLimits {
        self.serialization_limits
    }
}
