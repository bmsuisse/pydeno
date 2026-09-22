//! Python bindings exposing the runtime to Python callers.

use crate::runtime::config::RuntimeConfig;
use crate::runtime::conversion::{
    js_value_to_python, python_to_js_value, python_to_js_value_tracked,
};
use crate::runtime::handle::{BoundObjectProperty, RuntimeHandle};
use crate::runtime::js_value::{JSValue, LimitTracker, SerializationLimits};
use crate::runtime::ops::{OpToken, PythonOpMode};
use crate::runtime::runner::{FunctionCallResult, TerminationController};
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::BoundObject;
use pyo3_async_runtimes::tokio as pyo3_tokio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use super::bridge::bridge_js_future;
use super::error::{runtime_error_to_py, runtime_error_with_context};
use super::stats::{InspectorEndpoints, RuntimeStats};
use super::utils::normalize_timeout_to_ms;

#[pyclass(unsendable, weakref)]
pub struct Runtime {
    handle: std::cell::RefCell<Option<RuntimeHandle>>,
}

impl Runtime {
    /// Spawn the runtime thread, releasing the GIL for the handshake.
    ///
    /// `RuntimeHandle::spawn` blocks on `init_rx.recv()` until the runtime
    /// thread reports its isolate is built (`spawn_runtime_thread`,
    /// src/runtime/runner.rs). Holding the GIL across that wait deadlocks any
    /// construction-time work that calls back into Python -- which
    /// `on_console` plus a `bootstrap` script that logs does: the runtime
    /// thread blocks in `Python::attach` for a GIL this thread holds while it
    /// waits for the runtime thread. Detaching for the wait fixes that, and
    /// lets other Python threads run during the ~2.5ms isolate setup anyway.
    fn init_with_config(py: Python<'_>, config: RuntimeConfig) -> PyResult<Self> {
        let handle = py
            .detach(|| RuntimeHandle::spawn(config))
            .map_err(|err| runtime_error_with_context("Failed to spawn runtime", err))?;
        Ok(Self {
            handle: std::cell::RefCell::new(Some(handle)),
        })
    }

    /// The blocking half of [`Runtime::close`], split out so it can run with
    /// the GIL released. Touches no Python object.
    fn close_blocking(runtime: RuntimeHandle) -> PyResult<()> {
        let mut runtime = runtime;
        for stream_id in runtime.drain_tracked_js_stream_ids() {
            if runtime.is_shutdown() {
                break;
            }
            if let Err(err) = runtime.stream_release(stream_id) {
                log::debug!(
                    "Runtime.close failed to release stream id {}: {}",
                    stream_id,
                    err
                );
            }
        }
        for stream_id in runtime.drain_tracked_py_stream_ids() {
            runtime.cancel_py_stream_async(stream_id);
        }
        for fn_id in runtime.drain_tracked_function_ids() {
            if runtime.is_shutdown() {
                break;
            }
            if let Err(err) = runtime.release_function(fn_id) {
                log::debug!(
                    "Runtime.close failed to release function id {}: {}",
                    fn_id,
                    err
                );
            }
        }
        runtime
            .close()
            .map_err(|e| runtime_error_with_context("Shutdown failed", e))
    }

    fn detect_async(py: Python<'_>, handler: &Py<PyAny>) -> PyResult<bool> {
        let inspect = py.import("inspect")?;
        let mut is_async: bool = inspect
            .call_method1("iscoroutinefunction", (handler.bind(py),))?
            .extract()?;
        if !is_async {
            let handler_bound = handler.bind(py);
            if handler_bound.hasattr("__call__")? {
                let call_attr = handler_bound.getattr("__call__")?;
                is_async = inspect
                    .call_method1("iscoroutinefunction", (call_attr,))?
                    .extract()?;
            }
        }
        Ok(is_async)
    }

    fn checked_mode(py: Python<'_>, mode: &str, handler: &Py<PyAny>) -> PyResult<PythonOpMode> {
        let is_async = Self::detect_async(py, handler)?;
        match mode {
            "sync" if is_async => Err(PyRuntimeError::new_err(
                "Handler is async but mode='sync'; use mode='async'",
            )),
            "async" if !is_async => Err(PyRuntimeError::new_err(
                "Handler is sync but mode='async'; use mode='sync'",
            )),
            "sync" => Ok(PythonOpMode::Sync),
            "async" => Ok(PythonOpMode::Async),
            other => Err(PyRuntimeError::new_err(format!(
                "Invalid mode '{}', expected 'sync' or 'async'",
                other
            ))),
        }
    }
}

#[pymethods]
impl Runtime {
    #[new]
    #[pyo3(signature = (config = None))]
    fn py_new(py: Python<'_>, config: Option<&RuntimeConfig>) -> PyResult<Self> {
        let runtime_config = match config {
            Some(config_py) => config_py.clone(),
            None => RuntimeConfig::default(),
        };
        Self::init_with_config(py, runtime_config)
    }

    #[pyo3(signature = (code, /, *, timeout=None))]
    fn eval_async<'py>(
        &self,
        py: Python<'py>,
        code: String,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        let timeout_ms = normalize_timeout_to_ms(timeout)?;

        let task_locals = pyo3_tokio::get_current_locals(py)?;
        let handle_for_conversion = handle.clone();
        let eval_task_locals = Some(task_locals.clone());

        let future = async move { handle.eval_async(&code, timeout_ms, eval_task_locals).await };

        bridge_js_future(
            py,
            task_locals,
            future,
            handle_for_conversion,
            "Evaluation failed",
        )
    }

    fn eval(&self, py: Python<'_>, code: &str) -> PyResult<Py<PyAny>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();
        let code_owned = code.to_owned();
        let js_value = py
            .detach(|| handle.eval_sync(&code_owned))
            .map_err(|e| runtime_error_with_context("Evaluation failed", e))?;
        js_value_to_python(py, &js_value, Some(&handle))
    }

    fn is_closed(&self) -> bool {
        self.handle
            .borrow()
            .as_ref()
            .map(|handle| handle.is_shutdown())
            .unwrap_or(true)
    }

    fn get_stats(&self, py: Python<'_>) -> PyResult<RuntimeStats> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();
        let snapshot = py
            .detach(|| handle.get_stats())
            .map_err(|e| runtime_error_with_context("Failed to obtain runtime stats", e))?;
        Ok(RuntimeStats::from_snapshot(snapshot))
    }

    fn inspector_endpoints(&self) -> PyResult<Option<InspectorEndpoints>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        Ok(handle.inspector_metadata().map(InspectorEndpoints::from))
    }

    fn _debug_tracked_function_count(&self) -> PyResult<usize> {
        let handle = self.handle.borrow();
        Ok(handle
            .as_ref()
            .map(|handle| handle.tracked_function_count())
            .unwrap_or(0))
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let runtime = self.handle.borrow_mut().take();
        match runtime {
            Some(runtime) => py.detach(move || Self::close_blocking(runtime)),
            None => Ok(()),
        }
    }

    fn terminate(&self) -> PyResult<()> {
        let handle = self.handle.borrow().as_ref().cloned();

        if let Some(handle) = handle {
            handle
                .terminate()
                .map_err(|e| runtime_error_with_context("Termination failed", e))
        } else {
            Ok(())
        }
    }

    /// Hand out a `TerminationHandle`: a separate, non-`unsendable` pyclass
    /// wrapping only the `Send + Sync` `v8::IsolateHandle`.
    ///
    /// `Runtime` itself is `#[pyclass(unsendable)]` because it owns the V8
    /// isolate/JS context directly, which is genuinely not safe to touch from
    /// any thread but the one that created it -- so PyO3 panics
    /// (`pyo3_runtime.PanicException`, "Runtime is unsendable, but sent to
    /// another thread") on *any* cross-thread call to a `Runtime` method,
    /// including `terminate()`, even though `terminate()`'s own body just
    /// calls `IsolateHandle::terminate_execution()`, which `deno_core`/`v8`
    /// document as safe to call from another thread -- that's the whole
    /// point of `Isolate::thread_safe_handle()`. `TerminationHandle` exposes
    /// exactly that safe subset, so a watchdog thread can call
    /// `.terminate()` on it to kill a runaway `eval()` without panicking.
    fn termination_handle(&self) -> PyResult<TerminationHandle> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();
        Ok(TerminationHandle {
            termination: handle.termination_controller(),
        })
    }

    #[pyo3(signature = (name, handler, /, *, mode="sync"))]
    fn register_op(
        &self,
        py: Python<'_>,
        name: String,
        handler: Py<PyAny>,
        mode: &str,
    ) -> PyResult<OpToken> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        let handler_clone = handler.clone_ref(py);
        let mode_enum = Self::checked_mode(py, mode, &handler_clone)?;

        let op_id = handle
            .register_op(name, mode_enum, handler_clone)
            .map_err(|e| runtime_error_with_context("Op registration failed", e))?;

        // `register_op`'s whole contract is "here is a token guest JS can
        // call", so handing the token to the caller *is* the bind step.
        handle
            .set_op_exposure(op_id, true)
            .map_err(|e| runtime_error_with_context("Op registration failed", e))?;

        Ok(op_id)
    }

    /// Revoke an op capability, by the token `register_op`/`bind_function`
    /// returned.
    #[pyo3(signature = (op_id))]
    fn revoke_op(&self, _py: Python<'_>, op_id: OpToken) -> PyResult<bool> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        handle
            .set_op_exposure(op_id, false)
            .map_err(|e| runtime_error_with_context("Op revocation failed", e))
    }

    #[pyo3(signature = (name, handler))]
    fn bind_function(&self, py: Python<'_>, name: String, handler: Py<PyAny>) -> PyResult<OpToken> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        let handler_clone = handler.clone_ref(py);
        let is_async = Self::detect_async(py, &handler_clone)?;
        let mode_enum = if is_async {
            PythonOpMode::Async
        } else {
            PythonOpMode::Sync
        };

        let op_id = handle
            .register_op(name.clone(), mode_enum, handler_clone)
            .map_err(|e| runtime_error_with_context("Op registration failed", e))?;

        let bridge_name = match mode_enum {
            PythonOpMode::Sync => "__host_op_sync__",
            PythonOpMode::Async => "__host_op_async__",
        };

        let script = format!(
            "globalThis.{name} = (...args) => {bridge}({op_id}, ...args); void 0;",
            name = name,
            bridge = bridge_name,
            op_id = op_id
        );

        // Execute the binding script; ignore the return value ("undefined").
        // Expose only afterwards: if the binding script fails, the handler
        // stays registered but is not dispatchable from guest JS.
        let _ = self.eval(py, script.as_str())?;
        handle
            .set_op_exposure(op_id, true)
            .map_err(|e| runtime_error_with_context("Op registration failed", e))?;
        Ok(op_id)
    }

    #[pyo3(signature = (iterable))]
    fn stream_from_async_iterable(
        &self,
        py: Python<'_>,
        iterable: Py<PyAny>,
    ) -> PyResult<Py<PyStreamSource>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        PyStreamSource::new(py, handle, iterable)
    }

    #[pyo3(signature = (name, obj))]
    fn bind_object(
        &self,
        py: Python<'_>,
        name: String,
        obj: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyDict>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();
        let serialization_limits = handle.serialization_limits();

        let obj_bound = obj.clone();
        let dict = obj_bound
            .cast::<PyDict>()
            .map_err(|_| PyRuntimeError::new_err("bind_object expects a dict with string keys"))?;

        let mut bindings = Vec::with_capacity(dict.len());
        let tokens = PyDict::new(py);

        for (key, value) in dict.iter() {
            let key_str: String = key.extract()?;
            if value.is_callable() {
                let handler_py = value.unbind();
                let is_async = Self::detect_async(py, &handler_py)?;
                let mode_enum = if is_async {
                    PythonOpMode::Async
                } else {
                    PythonOpMode::Sync
                };
                let op_name = format!("{name}.{key_str}");
                let op_id = handle
                    .register_op(op_name, mode_enum, handler_py)
                    .map_err(|e| runtime_error_with_context("Op registration failed", e))?;
                tokens.set_item(&key_str, op_id)?;
                bindings.push(BoundObjectProperty::Op {
                    key: key_str,
                    op_id,
                    mode: mode_enum,
                });
            } else {
                let js_value = python_to_js_value(value, &serialization_limits)?;
                bindings.push(BoundObjectProperty::Value {
                    key: key_str,
                    value: js_value,
                });
            }
        }

        // `bind_object` exposes the op capabilities itself, but only after
        // `__pydeno_bind_object` has actually installed them (see runner.rs).
        handle
            .bind_object(name, bindings)
            .map_err(|e| runtime_error_with_context("Failed to bind object", e))?;
        Ok(tokens.unbind())
    }

    fn set_module_resolver(&self, _py: Python<'_>, resolver: Py<PyAny>) -> PyResult<()> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        handle
            .set_module_resolver(resolver)
            .map_err(|e| runtime_error_with_context("Failed to set module resolver", e))?;
        Ok(())
    }

    fn set_module_loader(&self, _py: Python<'_>, loader: Py<PyAny>) -> PyResult<()> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        handle
            .set_module_loader(loader)
            .map_err(|e| runtime_error_with_context("Failed to set module loader", e))?;
        Ok(())
    }

    fn add_static_module(&self, _py: Python<'_>, name: String, source: String) -> PyResult<()> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        handle
            .add_static_module(name, source)
            .map_err(|e| runtime_error_with_context("Failed to add static module", e))?;
        Ok(())
    }

    fn eval_module(&self, py: Python<'_>, specifier: &str) -> PyResult<Py<PyAny>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();
        let specifier_owned = specifier.to_owned();
        let js_value = py
            .detach(|| handle.eval_module_sync(&specifier_owned))
            .map_err(|e| runtime_error_with_context("Module evaluation failed", e))?;
        js_value_to_python(py, &js_value, Some(&handle))
    }

    #[pyo3(signature = (specifier, /, *, timeout=None))]
    fn eval_module_async<'py>(
        &self,
        py: Python<'py>,
        specifier: String,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))?
            .clone();

        let timeout_ms = normalize_timeout_to_ms(timeout)?;

        let task_locals = pyo3_tokio::get_current_locals(py)?;
        let handle_for_conversion = handle.clone();
        let module_task_locals = Some(task_locals.clone());

        let future = async move {
            handle
                .eval_module_async(&specifier, timeout_ms, module_task_locals)
                .await
        };

        bridge_js_future(
            py,
            task_locals,
            future,
            handle_for_conversion,
            "Module evaluation failed",
        )
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __exit__(
        &self,
        py: Python<'_>,
        _exc_type: Option<&Bound<'_, PyAny>>,
        _exc_value: Option<&Bound<'_, PyAny>>,
        _traceback: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        self.close(py)?;
        Ok(false)
    }
}

/// A safe, cross-thread-callable handle for killing a runaway `Runtime.eval()`.
///
/// Deliberately NOT `#[pyclass(unsendable)]`: it wraps only
/// `TerminationController`, which in turn wraps only `v8::IsolateHandle`
/// (`Clone + Send + Sync`, obtained via `Isolate::thread_safe_handle()`) plus
/// a couple of atomics/a `Mutex<Option<String>>` -- every field is `Send +
/// Sync`, so PyO3 lets this pyclass be constructed on one thread and its
/// methods called from any other, which is the entire point: a watchdog
/// thread holds one of these and calls `.terminate()` on it while the main
/// thread is blocked inside a synchronous `Runtime.eval()` on the runtime's
/// owning thread.
#[pyclass(module = "_pydeno", frozen)]
pub struct TerminationHandle {
    termination: TerminationController,
}

#[pymethods]
impl TerminationHandle {
    /// Interrupt the isolate's currently running (or next) JS execution.
    ///
    /// Safe to call from any thread, including one other than the thread
    /// that owns the `Runtime`/isolate -- that's `IsolateHandle`'s documented
    /// contract. A blocked synchronous `eval()` on the owning thread returns
    /// an error shortly after this call returns.
    fn terminate(&self) {
        self.termination
            .ensure_reason("Terminated via TerminationHandle from another thread");
        // `request()` flips the shared status flag to REQUESTED first, the
        // same as `RuntimeHandle::terminate()` does internally, so the
        // runtime thread's own error path (`should_reject_new_work` ->
        // `finalize_termination` -> `mark_terminated`) recognizes this as a
        // real termination once the isolate reports an aborted execution,
        // instead of treating the resulting JS error as an ordinary one.
        self.termination.request();
        self.termination.terminate_execution();
    }

    /// Whether the isolate has fully processed a termination request.
    fn is_terminated(&self) -> bool {
        self.termination.is_terminated()
    }

    fn __repr__(&self) -> String {
        format!("<TerminationHandle terminated={}>", self.is_terminated())
    }
}

/// Python proxy for a JavaScript function.
///
/// This class represents a JavaScript function that can be called from Python.
/// Functions are awaitable by default (async-first design).
#[pyclass(unsendable, weakref)] // allow Python weak references for finalizers
pub struct JsFunction {
    handle: std::cell::RefCell<Option<RuntimeHandle>>,
    fn_id: u32,
    closed: std::cell::Cell<bool>,
    serialization_limits: SerializationLimits,
}

impl JsFunction {
    pub fn new(
        py: Python<'_>,
        handle: RuntimeHandle,
        fn_id: u32,
        serialization_limits: SerializationLimits,
    ) -> PyResult<Py<Self>> {
        handle.track_function_id(fn_id);
        let finalizer_handle = handle.clone();
        let instance = Self {
            handle: std::cell::RefCell::new(Some(handle)),
            fn_id,
            closed: std::cell::Cell::new(false),
            serialization_limits,
        };
        let py_obj = Py::new(py, instance)?;
        Self::attach_finalizer(py, &py_obj, finalizer_handle, fn_id)?;
        Ok(py_obj)
    }

    /// Get the function ID for transfer back to JavaScript.
    ///
    /// Validates that the function is not closed and the runtime is still alive.
    /// This prevents cryptic "Function ID not found" errors from the runtime thread.
    pub(crate) fn function_id_for_transfer(&self) -> PyResult<u32> {
        // Check if function has been closed
        if self.closed.get() {
            return Err(PyRuntimeError::new_err("Function has been closed"));
        }

        // Check if runtime handle is still alive
        let handle = self.handle.borrow();
        if handle.is_none() {
            return Err(PyRuntimeError::new_err("Runtime has been shut down"));
        }

        // Additional check: verify runtime is not shutdown
        if let Some(h) = handle.as_ref() {
            if h.is_shutdown() {
                return Err(PyRuntimeError::new_err("Runtime has been shut down"));
            }
        }

        Ok(self.fn_id)
    }

    fn attach_finalizer(
        py: Python<'_>,
        py_obj: &Py<Self>,
        handle: RuntimeHandle,
        fn_id: u32,
    ) -> PyResult<()> {
        let weakref = py.import("weakref")?;
        let finalize = weakref.getattr(pyo3::intern!(py, "finalize"))?;
        let finalizer = Py::new(py, JsFunctionFinalizer::new(handle, fn_id))?;
        finalize.call1((py_obj.clone_ref(py), finalizer))?;
        Ok(())
    }

    /// Convert a call's positional arguments, enforcing
    /// `max_serialization_bytes` as an *aggregate* budget for the whole call.
    ///
    /// Previously this built a fresh `LimitTracker` per argument, so the byte
    /// limit was enforced per-argument: a caller passing N arguments each just
    /// under the limit transferred N times the intended budget. One tracker is
    /// now shared across every argument in the call.
    fn convert_python_args(
        &self,
        args: &Bound<'_, pyo3::types::PyTuple>,
    ) -> PyResult<Vec<JSValue>> {
        let mut tracker = LimitTracker::new(
            self.serialization_limits.max_depth,
            self.serialization_limits.max_bytes,
        );
        let mut js_args = Vec::with_capacity(args.len());
        for arg in args.iter() {
            js_args.push(python_to_js_value_tracked(arg, &mut tracker)?);
        }
        Ok(js_args)
    }
}

#[pymethods]
impl JsFunction {
    /// Call the JavaScript function with the given arguments.
    ///
    /// Returns an awaitable that resolves to the function result.
    ///
    /// Args:
    ///     *args: Arguments to pass to the JavaScript function
    ///     timeout: Optional timeout (seconds as float/int, or datetime.timedelta)
    ///
    /// Returns:
    ///     An awaitable that resolves to the function's return value
    #[pyo3(signature = (*args, timeout=None))]
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        args: &Bound<'py, pyo3::types::PyTuple>,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.closed.get() {
            return Err(PyRuntimeError::new_err("Function has been closed"));
        }

        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been shut down"))?
            .clone();

        let fn_id = self.fn_id;

        let js_args = self.convert_python_args(args)?;
        let timeout_ms = normalize_timeout_to_ms(timeout)?;

        // `call_function_sync` bottoms out in a blocking `result_rx.recv()`
        // (src/runtime/handle.rs), so holding the GIL across it serializes
        // every thread calling a JsFunction -- measured at 0.99x overlap on
        // two threads with their own Runtime, versus 1.96x for Runtime.eval,
        // which already detaches. `js_args` is an owned Vec<JSValue> and the
        // handle is an owned clone, so nothing Python-ish crosses the closure.
        let call_result = py
            .detach(|| handle.call_function_sync(fn_id, js_args, timeout_ms))
            .map_err(|e| runtime_error_with_context("Function call failed", e))?;

        match call_result {
            FunctionCallResult::Immediate(value) => {
                let py_obj = js_value_to_python(py, &value, Some(&handle))?;
                Ok(py_obj.into_bound(py))
            }
            FunctionCallResult::Pending { call_id } => {
                let task_locals = pyo3_tokio::get_current_locals(py)?;
                let call_task_locals = Some(task_locals.clone());
                let handle_for_conversion = handle.clone();
                let future =
                    async move { handle.resume_function_call(call_id, call_task_locals).await };

                bridge_js_future(
                    py,
                    task_locals,
                    future,
                    handle_for_conversion,
                    "Function call failed",
                )
            }
        }
    }

    /// Explicit async invocation that always returns a coroutine.
    ///
    /// The coroutine is accepted by `asyncio.create_task` and
    /// `asyncio.gather`; through 0.2.x this returned a bare `asyncio.Future`,
    /// which `create_task` refuses. The call itself still starts the work on
    /// the runtime thread immediately -- awaiting only collects the result.
    #[pyo3(signature = (*args, timeout=None))]
    fn call_async<'py>(
        &self,
        py: Python<'py>,
        args: &Bound<'py, pyo3::types::PyTuple>,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.closed.get() {
            return Err(PyRuntimeError::new_err("Function has been closed"));
        }

        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been shut down"))?
            .clone();

        let fn_id = self.fn_id;
        let js_args = self.convert_python_args(args)?;
        let timeout_ms = normalize_timeout_to_ms(timeout)?;

        let task_locals = pyo3_tokio::get_current_locals(py)?;
        let call_task_locals = Some(task_locals.clone());
        let handle_for_conversion = handle.clone();

        let future = async move {
            handle
                .call_function_async(fn_id, js_args, timeout_ms, call_task_locals)
                .await
        };

        bridge_js_future(
            py,
            task_locals,
            future,
            handle_for_conversion,
            "Function call failed",
        )
    }

    /// Close the function handle and release resources.
    ///
    /// After calling close(), the function can no longer be invoked.
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.closed.get() {
            // Already closed, return immediately
            return pyo3_tokio::future_into_py(py, async { Ok(()) });
        }

        let handle = self
            .handle
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been shut down"))?
            .clone();
        self.handle.borrow_mut().take();

        let fn_id = self.fn_id;
        self.closed.set(true);

        let untrack_handle = handle.clone();
        let future = async move {
            let result = handle
                .release_function_async(fn_id)
                .await
                .map_err(|e| runtime_error_with_context("Failed to release function", e));
            untrack_handle.untrack_function_id(fn_id);
            result
        };

        let py_future = pyo3_tokio::future_into_py(py, future)?;
        Ok(py_future.into_bound())
    }

    /// String representation of the function.
    fn __repr__(&self) -> String {
        if self.closed.get() {
            "<JsFunction (closed)>".to_string()
        } else {
            format!("<JsFunction id={}>", self.fn_id)
        }
    }
}

#[pyclass(module = "_pydeno", name = "_JsFunctionFinalizer")]
pub(crate) struct JsFunctionFinalizer {
    handle: Mutex<Option<RuntimeHandle>>,
    fn_id: u32,
}

impl JsFunctionFinalizer {
    fn new(handle: RuntimeHandle, fn_id: u32) -> Self {
        Self {
            handle: Mutex::new(Some(handle)),
            fn_id,
        }
    }
}

#[pymethods]
impl JsFunctionFinalizer {
    /// Release the JS function handle without holding the GIL.
    ///
    /// This fires from `weakref.finalize` at arbitrary garbage-collection
    /// points, and `release_function` is a blocking round trip to the runtime
    /// thread -- so holding the GIL here stalls every other Python thread at a
    /// moment none of them can predict.
    fn __call__(&self, py: Python<'_>) {
        let handle = self.handle.lock().unwrap().take();
        let Some(runtime_handle) = handle else {
            return;
        };
        let fn_id = self.fn_id;
        py.detach(move || {
            if !runtime_handle.is_function_tracked(fn_id) {
                return;
            }
            if runtime_handle.is_shutdown() {
                runtime_handle.untrack_function_id(fn_id);
                return;
            }
            if let Err(err) = runtime_handle.release_function(fn_id) {
                log::debug!(
                    "JsFunction finalizer failed to release function id {}: {}",
                    fn_id,
                    err
                );
            }
            runtime_handle.untrack_function_id(fn_id);
        });
    }
}

struct StreamSharedState {
    handle: Mutex<Option<RuntimeHandle>>,
    stream_id: u32,
    closed: AtomicBool,
}

impl StreamSharedState {
    fn new(handle: RuntimeHandle, stream_id: u32) -> Self {
        Self {
            handle: Mutex::new(Some(handle)),
            stream_id,
            closed: AtomicBool::new(false),
        }
    }

    fn stream_id(&self) -> u32 {
        self.stream_id
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    fn mark_remote_closed(&self) {
        self.closed.store(true, Ordering::SeqCst);
        self.handle.lock().unwrap().take();
    }

    fn cancel(&self) {
        if self.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(handle) = self.handle.lock().unwrap().take() {
            if let Err(err) = handle.stream_cancel(self.stream_id) {
                log::debug!(
                    "JsStream cancel failed for stream id {}: {}",
                    self.stream_id,
                    err
                );
            }
        }
    }
}

#[pyclass(unsendable, weakref)]
pub struct JsStream {
    state: Arc<StreamSharedState>,
}

impl JsStream {
    pub fn new(py: Python<'_>, handle: RuntimeHandle, stream_id: u32) -> PyResult<Py<Self>> {
        handle.track_js_stream_id(stream_id);
        let state = Arc::new(StreamSharedState::new(handle.clone(), stream_id));
        let instance = Self {
            state: state.clone(),
        };
        let py_obj = Py::new(py, instance)?;
        Self::attach_finalizer(py, &py_obj, state)?;
        Ok(py_obj)
    }

    fn attach_finalizer(
        py: Python<'_>,
        py_obj: &Py<Self>,
        state: Arc<StreamSharedState>,
    ) -> PyResult<()> {
        let weakref = py.import("weakref")?;
        let finalize = weakref.getattr(pyo3::intern!(py, "finalize"))?;
        let finalizer = Py::new(py, JsStreamFinalizer::new(state))?;
        finalize.call1((py_obj.clone_ref(py), finalizer))?;
        Ok(())
    }

    fn cancel_with_runtime(&self) {
        self.state.cancel();
    }
}

#[pymethods]
impl JsStream {
    fn __aiter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __anext__<'py>(slf: PyRef<'py, Self>, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if slf.state.is_closed() {
            return Err(PyErr::new::<PyStopAsyncIteration, _>("Stream closed"));
        }

        let handle = slf
            .state
            .handle
            .lock()
            .unwrap()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been shut down"))?
            .clone();
        let stream_id = slf.state.stream_id();
        let state = slf.state.clone();
        let handle_for_conversion = handle.clone();
        let future = async move {
            match handle.stream_read(stream_id).await {
                Ok(mut chunk) => {
                    if chunk.done {
                        state.mark_remote_closed();
                        Err(PyErr::new::<PyStopAsyncIteration, _>(""))
                    } else {
                        let chunk_value = chunk.value.take();
                        let py_value = Python::attach(|py| match chunk_value {
                            Some(value) => {
                                js_value_to_python(py, &value, Some(&handle_for_conversion))
                            }
                            None => Ok(py.None()),
                        })?;
                        Ok(py_value)
                    }
                }
                Err(err) => Err(runtime_error_to_py(err)),
            }
        };

        pyo3_tokio::future_into_py(py, future)
    }

    fn close(&self) -> PyResult<()> {
        self.cancel_with_runtime();
        Ok(())
    }

    fn __repr__(&self) -> String {
        let stream_id = self.state.stream_id();
        if self.state.is_closed() {
            "<JsStream (closed)>".to_string()
        } else {
            format!("<JsStream id={}>", stream_id)
        }
    }
}

#[pyclass(module = "_pydeno", name = "_JsStreamFinalizer")]
pub(crate) struct JsStreamFinalizer {
    state: Arc<StreamSharedState>,
}

impl JsStreamFinalizer {
    fn new(state: Arc<StreamSharedState>) -> Self {
        Self { state }
    }
}

#[pymethods]
impl JsStreamFinalizer {
    fn __call__(&self) {
        self.state.cancel();
    }
}

#[pyclass(module = "_pydeno", unsendable, weakref)]
pub struct PyStreamSource {
    handle: std::cell::RefCell<Option<RuntimeHandle>>,
    stream_id: u32,
    closed: std::cell::Cell<bool>,
}

impl PyStreamSource {
    pub fn new(py: Python<'_>, handle: RuntimeHandle, iterable: Py<PyAny>) -> PyResult<Py<Self>> {
        let task_locals = pyo3_tokio::get_current_locals(py)?;
        let stream_id = handle
            .register_py_stream(iterable, task_locals)
            .map_err(|e| runtime_error_with_context("Stream registration failed", e))?;
        let finalizer_handle = handle.clone();
        let instance = Self {
            handle: std::cell::RefCell::new(Some(handle)),
            stream_id,
            closed: std::cell::Cell::new(false),
        };
        let py_obj = Py::new(py, instance)?;
        Self::attach_finalizer(py, &py_obj, finalizer_handle, stream_id)?;
        Ok(py_obj)
    }

    fn attach_finalizer(
        py: Python<'_>,
        py_obj: &Py<Self>,
        handle: RuntimeHandle,
        stream_id: u32,
    ) -> PyResult<()> {
        let weakref = py.import("weakref")?;
        let finalize = weakref.getattr(pyo3::intern!(py, "finalize"))?;
        let finalizer = Py::new(py, PyStreamFinalizer::new(handle, stream_id))?;
        finalize.call1((py_obj.clone_ref(py), finalizer))?;
        Ok(())
    }

    pub(crate) fn stream_id_for_transfer(&self) -> PyResult<u32> {
        if self.closed.get() {
            return Err(PyRuntimeError::new_err("Stream has been closed"));
        }
        if self.handle.borrow().is_none() {
            return Err(PyRuntimeError::new_err("Runtime has been shut down"));
        }
        Ok(self.stream_id)
    }

    fn mark_closed(&self) {
        if self.closed.replace(true) {
            return;
        }
        if let Some(handle) = self.handle.borrow_mut().take() {
            handle.cancel_py_stream_async(self.stream_id);
        }
    }
}

#[pymethods]
impl PyStreamSource {
    #[pyo3(name = "close")]
    fn close_py(&self) {
        self.mark_closed();
    }

    fn __repr__(&self) -> String {
        if self.closed.get() {
            "<PyStreamSource (closed)>".to_string()
        } else {
            format!("<PyStreamSource id={}>", self.stream_id)
        }
    }
}

#[pyclass(module = "_pydeno", name = "_PyStreamFinalizer")]
pub(crate) struct PyStreamFinalizer {
    handle: Mutex<Option<RuntimeHandle>>,
    stream_id: u32,
}

impl PyStreamFinalizer {
    fn new(handle: RuntimeHandle, stream_id: u32) -> Self {
        Self {
            handle: Mutex::new(Some(handle)),
            stream_id,
        }
    }
}

#[pymethods]
impl PyStreamFinalizer {
    fn __call__(&self) {
        let mut handle = self.handle.lock().unwrap();
        if let Some(runtime_handle) = handle.take() {
            runtime_handle.cancel_py_stream_async(self.stream_id);
        }
    }
}
