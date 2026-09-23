//! `JsFunction` Python proxy for JavaScript functions.

use crate::runtime::conversion::{js_value_to_python, python_to_js_value_tracked};
use crate::runtime::handle::RuntimeHandle;
use crate::runtime::js_value::{JSValue, LimitTracker, SerializationLimits};
use crate::runtime::runner::FunctionCallResult;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::BoundObject;
use pyo3_async_runtimes::tokio as pyo3_tokio;
use std::cell::{Cell, RefCell};
use std::sync::Mutex;

use super::bridge::bridge_handle_call;
use super::error::context;
use super::utils::{attach_finalizer, normalize_timeout_to_ms};

/// Python proxy for a JavaScript function.
///
/// This class represents a JavaScript function that can be called from Python.
/// Functions are awaitable by default (async-first design).
#[pyclass(unsendable, weakref)] // allow Python weak references for finalizers
pub struct JsFunction {
    handle: RefCell<Option<RuntimeHandle>>,
    fn_id: u32,
    closed: Cell<bool>,
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
        let finalizer = JsFunctionFinalizer {
            handle: Mutex::new(Some(handle.clone())),
            fn_id,
        };
        let py_obj = Py::new(
            py,
            Self {
                handle: RefCell::new(Some(handle)),
                fn_id,
                closed: Cell::new(false),
                serialization_limits,
            },
        )?;
        attach_finalizer(py, &py_obj, finalizer)?;
        Ok(py_obj)
    }

    /// The function ID for transfer back to JavaScript, validated up front to
    /// avoid a cryptic "Function ID not found" from the runtime thread.
    pub(crate) fn function_id_for_transfer(&self) -> PyResult<u32> {
        if self.closed.get() {
            return Err(PyRuntimeError::new_err("Function has been closed"));
        }
        match self.handle.borrow().as_ref() {
            Some(handle) if !handle.is_shutdown() => Ok(self.fn_id),
            _ => Err(PyRuntimeError::new_err("Runtime has been shut down")),
        }
    }

    fn live_handle(&self) -> PyResult<RuntimeHandle> {
        if self.closed.get() {
            return Err(PyRuntimeError::new_err("Function has been closed"));
        }
        self.handle
            .borrow()
            .clone()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been shut down"))
    }

    /// Convert a call's arguments with one shared `LimitTracker`, so
    /// `max_serialization_bytes` is an aggregate budget for the whole call.
    fn convert_python_args(&self, args: &Bound<'_, PyTuple>) -> PyResult<Vec<JSValue>> {
        let mut tracker = LimitTracker::new(
            self.serialization_limits.max_depth,
            self.serialization_limits.max_bytes,
        );
        args.iter()
            .map(|arg| python_to_js_value_tracked(arg, &mut tracker))
            .collect()
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
        args: &Bound<'py, PyTuple>,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.live_handle()?;
        let fn_id = self.fn_id;
        let js_args = self.convert_python_args(args)?;
        let timeout_ms = normalize_timeout_to_ms(timeout)?;

        // Blocking round trip: detach so concurrent JsFunction calls on other
        // threads are not serialized on the GIL.
        let call_result = py
            .detach(|| handle.call_function_sync(fn_id, js_args, timeout_ms))
            .map_err(context("Function call failed"))?;

        match call_result {
            FunctionCallResult::Immediate(value) => {
                Ok(js_value_to_python(py, &value, Some(&handle))?.into_bound(py))
            }
            FunctionCallResult::Pending { call_id } => bridge_handle_call(
                py,
                handle,
                "Function call failed",
                move |h, locals| async move { h.resume_function_call(call_id, locals).await },
            ),
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
        args: &Bound<'py, PyTuple>,
        timeout: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.live_handle()?;
        let fn_id = self.fn_id;
        let js_args = self.convert_python_args(args)?;
        let timeout_ms = normalize_timeout_to_ms(timeout)?;
        bridge_handle_call(
            py,
            handle,
            "Function call failed",
            move |h, locals| async move {
                h.call_function_async(fn_id, js_args, timeout_ms, locals)
                    .await
            },
        )
    }

    /// Close the function handle and release resources.
    ///
    /// After calling close(), the function can no longer be invoked.
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.closed.get() {
            return pyo3_tokio::future_into_py(py, async { Ok(()) });
        }
        let handle = self.live_handle()?;
        self.handle.borrow_mut().take();
        self.closed.set(true);

        let fn_id = self.fn_id;
        let future = async move {
            let result = handle
                .release_function_async(fn_id)
                .await
                .map_err(context("Failed to release function"));
            handle.untrack_function_id(fn_id);
            result
        };
        Ok(pyo3_tokio::future_into_py(py, future)?.into_bound())
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

#[pymethods]
impl JsFunctionFinalizer {
    /// Release the JS function handle without holding the GIL.
    ///
    /// This fires from `weakref.finalize` at arbitrary garbage-collection
    /// points, and `release_function` is a blocking round trip to the runtime
    /// thread -- so holding the GIL here stalls every other Python thread at a
    /// moment none of them can predict.
    fn __call__(&self, py: Python<'_>) {
        let Some(runtime_handle) = self.handle.lock().unwrap().take() else {
            return;
        };
        let fn_id = self.fn_id;
        py.detach(move || {
            if !runtime_handle.is_function_tracked(fn_id) {
                return;
            }
            if !runtime_handle.is_shutdown() {
                if let Err(err) = runtime_handle.release_function(fn_id) {
                    log::debug!(
                        "JsFunction finalizer failed to release function id {fn_id}: {err}"
                    );
                }
            }
            runtime_handle.untrack_function_id(fn_id);
        });
    }
}
