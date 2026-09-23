//! `Runtime` and `TerminationHandle` Python bindings.

use crate::runtime::config::RuntimeConfig;
use crate::runtime::conversion::{js_value_to_python, python_to_js_value};
use crate::runtime::handle::{BoundObjectProperty, RuntimeHandle};
use crate::runtime::ops::{OpToken, PythonOpMode};
use crate::runtime::runner::TerminationController;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::RefCell;

use super::bridge::bridge_handle_call;
use super::error::context;
use super::stats::{InspectorEndpoints, RuntimeStats};
use super::stream::PyStreamSource;
use super::utils::normalize_timeout_to_ms;

#[pyclass(unsendable, weakref)]
pub struct Runtime {
    handle: RefCell<Option<RuntimeHandle>>,
}

impl Runtime {
    /// Spawn the runtime thread with the GIL released: the runtime thread may
    /// need the GIL during init (e.g. `on_console` + a logging `bootstrap`),
    /// so holding it across the handshake would deadlock.
    fn init_with_config(py: Python<'_>, config: RuntimeConfig) -> PyResult<Self> {
        let handle = py
            .detach(|| RuntimeHandle::spawn(config))
            .map_err(context("Failed to spawn runtime"))?;
        Ok(Self {
            handle: RefCell::new(Some(handle)),
        })
    }

    fn live_handle(&self) -> PyResult<RuntimeHandle> {
        self.handle
            .borrow()
            .clone()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been closed"))
    }

    /// The blocking half of [`Runtime::close`], run with the GIL released.
    fn close_blocking(mut runtime: RuntimeHandle) -> PyResult<()> {
        for stream_id in runtime.drain_tracked_js_stream_ids() {
            if runtime.is_shutdown() {
                break;
            }
            if let Err(err) = runtime.stream_release(stream_id) {
                log::debug!("Runtime.close failed to release stream id {stream_id}: {err}");
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
                log::debug!("Runtime.close failed to release function id {fn_id}: {err}");
            }
        }
        runtime.close().map_err(context("Shutdown failed"))
    }

    /// Op mode from the handler itself (or its `__call__`) being a coroutine function.
    fn detect_mode(py: Python<'_>, handler: &Py<PyAny>) -> PyResult<PythonOpMode> {
        let inspect = py.import("inspect")?;
        let handler = handler.bind(py);
        let mut is_async: bool = inspect
            .call_method1("iscoroutinefunction", (handler,))?
            .extract()?;
        if !is_async && handler.hasattr("__call__")? {
            is_async = inspect
                .call_method1("iscoroutinefunction", (handler.getattr("__call__")?,))?
                .extract()?;
        }
        Ok(if is_async {
            PythonOpMode::Async
        } else {
            PythonOpMode::Sync
        })
    }

    fn checked_mode(py: Python<'_>, mode: &str, handler: &Py<PyAny>) -> PyResult<PythonOpMode> {
        let detected = Self::detect_mode(py, handler)?;
        match (mode, detected) {
            ("sync", PythonOpMode::Async) => Err(PyRuntimeError::new_err(
                "Handler is async but mode='sync'; use mode='async'",
            )),
            ("async", PythonOpMode::Sync) => Err(PyRuntimeError::new_err(
                "Handler is sync but mode='async'; use mode='sync'",
            )),
            ("sync" | "async", mode) => Ok(mode),
            (other, _) => Err(PyRuntimeError::new_err(format!(
                "Invalid mode '{other}', expected 'sync' or 'async'"
            ))),
        }
    }
}

#[pymethods]
impl Runtime {
    #[new]
    #[pyo3(signature = (config = None))]
    fn py_new(py: Python<'_>, config: Option<&RuntimeConfig>) -> PyResult<Self> {
        Self::init_with_config(py, config.cloned().unwrap_or_default())
    }

    #[pyo3(signature = (code, /, *, timeout=None))]
    fn eval_async<'py>(
        &self,
        py: Python<'py>,
        code: String,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.live_handle()?;
        let timeout_ms = normalize_timeout_to_ms(timeout)?;
        bridge_handle_call(
            py,
            handle,
            "Evaluation failed",
            move |h, locals| async move { h.eval_async(&code, timeout_ms, locals).await },
        )
    }

    fn eval(&self, py: Python<'_>, code: &str) -> PyResult<Py<PyAny>> {
        let handle = self.live_handle()?;
        let js_value = py
            .detach(|| handle.eval_sync(code))
            .map_err(context("Evaluation failed"))?;
        js_value_to_python(py, &js_value, Some(&handle))
    }

    fn is_closed(&self) -> bool {
        self.handle
            .borrow()
            .as_ref()
            .is_none_or(|handle| handle.is_shutdown())
    }

    fn get_stats(&self, py: Python<'_>) -> PyResult<RuntimeStats> {
        let handle = self.live_handle()?;
        let snapshot = py
            .detach(|| handle.get_stats())
            .map_err(context("Failed to obtain runtime stats"))?;
        Ok(RuntimeStats::from_snapshot(snapshot))
    }

    fn inspector_endpoints(&self) -> PyResult<Option<InspectorEndpoints>> {
        Ok(self
            .live_handle()?
            .inspector_metadata()
            .map(InspectorEndpoints::from))
    }

    fn _debug_tracked_function_count(&self) -> PyResult<usize> {
        Ok(self
            .handle
            .borrow()
            .as_ref()
            .map_or(0, |handle| handle.tracked_function_count()))
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        match self.handle.borrow_mut().take() {
            Some(runtime) => py.detach(move || Self::close_blocking(runtime)),
            None => Ok(()),
        }
    }

    fn terminate(&self) -> PyResult<()> {
        match self.handle.borrow().clone() {
            Some(handle) => handle.terminate().map_err(context("Termination failed")),
            None => Ok(()),
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
        Ok(TerminationHandle {
            termination: self.live_handle()?.termination_controller(),
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
        let handle = self.live_handle()?;
        let mode = Self::checked_mode(py, mode, &handler)?;
        let op_id = handle
            .register_op(name, mode, handler)
            .map_err(context("Op registration failed"))?;
        // Handing the token to the caller *is* the bind step, so expose it now.
        handle
            .set_op_exposure(op_id, true)
            .map_err(context("Op registration failed"))?;
        Ok(op_id)
    }

    /// Revoke an op capability, by the token `register_op`/`bind_function`
    /// returned.
    #[pyo3(signature = (op_id))]
    fn revoke_op(&self, _py: Python<'_>, op_id: OpToken) -> PyResult<bool> {
        self.live_handle()?
            .set_op_exposure(op_id, false)
            .map_err(context("Op revocation failed"))
    }

    #[pyo3(signature = (name, handler))]
    fn bind_function(&self, py: Python<'_>, name: String, handler: Py<PyAny>) -> PyResult<OpToken> {
        let handle = self.live_handle()?;
        let mode = Self::detect_mode(py, &handler)?;
        let op_id = handle
            .register_op(name.clone(), mode, handler)
            .map_err(context("Op registration failed"))?;
        let bridge = match mode {
            PythonOpMode::Sync => "__host_op_sync__",
            PythonOpMode::Async => "__host_op_async__",
        };
        let script =
            format!("globalThis.{name} = (...args) => {bridge}({op_id}, ...args); void 0;");

        // Expose only after the binding script succeeded, so a failed binding
        // leaves the handler registered but not dispatchable.
        self.eval(py, &script)?;
        handle
            .set_op_exposure(op_id, true)
            .map_err(context("Op registration failed"))?;
        Ok(op_id)
    }

    #[pyo3(signature = (iterable))]
    fn stream_from_async_iterable(
        &self,
        py: Python<'_>,
        iterable: Py<PyAny>,
    ) -> PyResult<Py<PyStreamSource>> {
        PyStreamSource::new(py, self.live_handle()?, iterable)
    }

    #[pyo3(signature = (name, obj))]
    fn bind_object(
        &self,
        py: Python<'_>,
        name: String,
        obj: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyDict>> {
        let handle = self.live_handle()?;
        let serialization_limits = handle.serialization_limits();
        let dict = obj
            .cast::<PyDict>()
            .map_err(|_| PyRuntimeError::new_err("bind_object expects a dict with string keys"))?;

        let mut bindings = Vec::with_capacity(dict.len());
        let tokens = PyDict::new(py);
        for (key, value) in dict.iter() {
            let key: String = key.extract()?;
            if value.is_callable() {
                let handler = value.unbind();
                let mode = Self::detect_mode(py, &handler)?;
                let op_id = handle
                    .register_op(format!("{name}.{key}"), mode, handler)
                    .map_err(context("Op registration failed"))?;
                tokens.set_item(&key, op_id)?;
                bindings.push(BoundObjectProperty::Op { key, op_id, mode });
            } else {
                let value = python_to_js_value(value, &serialization_limits)?;
                bindings.push(BoundObjectProperty::Value { key, value });
            }
        }

        // The runner exposes the ops only once `__pydeno_bind_object` installed them.
        handle
            .bind_object(name, bindings)
            .map_err(context("Failed to bind object"))?;
        Ok(tokens.unbind())
    }

    fn set_module_resolver(&self, _py: Python<'_>, resolver: Py<PyAny>) -> PyResult<()> {
        self.live_handle()?
            .set_module_resolver(resolver)
            .map_err(context("Failed to set module resolver"))
    }

    fn set_module_loader(&self, _py: Python<'_>, loader: Py<PyAny>) -> PyResult<()> {
        self.live_handle()?
            .set_module_loader(loader)
            .map_err(context("Failed to set module loader"))
    }

    fn add_static_module(&self, _py: Python<'_>, name: String, source: String) -> PyResult<()> {
        self.live_handle()?
            .add_static_module(name, source)
            .map_err(context("Failed to add static module"))
    }

    fn eval_module(&self, py: Python<'_>, specifier: &str) -> PyResult<Py<PyAny>> {
        let handle = self.live_handle()?;
        let js_value = py
            .detach(|| handle.eval_module_sync(specifier))
            .map_err(context("Module evaluation failed"))?;
        js_value_to_python(py, &js_value, Some(&handle))
    }

    #[pyo3(signature = (specifier, /, *, timeout=None))]
    fn eval_module_async<'py>(
        &self,
        py: Python<'py>,
        specifier: String,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.live_handle()?;
        let timeout_ms = normalize_timeout_to_ms(timeout)?;
        bridge_handle_call(
            py,
            handle,
            "Module evaluation failed",
            move |h, locals| async move { h.eval_module_async(&specifier, timeout_ms, locals).await },
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
        // Flag REQUESTED first (as `RuntimeHandle::terminate` does) so the
        // runtime thread treats the aborted execution as a real termination.
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
