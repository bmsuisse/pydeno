//! Async bridge between Tokio futures on the runtime thread and Python asyncio futures.
use crate::runtime::conversion::js_value_to_python;
use crate::runtime::error::RuntimeResult;
use crate::runtime::handle::RuntimeHandle;
use crate::runtime::js_value::JSValue;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};
use pyo3_async_runtimes::{tokio as pyo3_tokio, TaskLocals};
use std::future::Future;
use tokio::sync::oneshot;

use super::error::runtime_error_with_context;

fn python_future_flag(
    future: &Bound<'_, PyAny>,
    name: &Bound<'_, pyo3::types::PyString>,
) -> PyResult<bool> {
    future.getattr(name)?.call0()?.is_truthy()
}

fn python_future_cancelled(future: &Bound<'_, PyAny>) -> PyResult<bool> {
    python_future_flag(future, pyo3::intern!(future.py(), "cancelled"))
}

fn python_future_done(future: &Bound<'_, PyAny>) -> PyResult<bool> {
    python_future_flag(future, pyo3::intern!(future.py(), "done"))
}

#[pyclass]
/// Callback object that relays Python-side cancellation back to the Rust task.
struct JsAsyncCancelCallback {
    cancel_tx: Option<oneshot::Sender<()>>,
}

#[pymethods]
impl JsAsyncCancelCallback {
    /// Forward cancellation notifications from Python to the waiting Rust future.
    fn __call__(&mut self, future: &Bound<PyAny>) -> PyResult<()> {
        if python_future_cancelled(future)? {
            if let Some(tx) = self.cancel_tx.take() {
                let _ = tx.send(());
            }
        }
        Ok(())
    }
}

#[pyclass]
/// Helper that runs on the Python event loop to complete the awaiting future.
struct JsAsyncResultSetter {
    future: Py<PyAny>,
    result: Option<RuntimeResult<JSValue>>,
    handle: RuntimeHandle,
    error_context: &'static str,
}

#[pymethods]
impl JsAsyncResultSetter {
    /// Execute the deferred conversion and resolve the Python `asyncio.Future`.
    fn __call__(&mut self, py: Python<'_>) -> PyResult<()> {
        let future = self.future.bind(py);
        if python_future_done(future)? || python_future_cancelled(future)? {
            return Ok(());
        }

        let result = self
            .result
            .take()
            .expect("JsAsyncResultSetter invoked more than once");
        match result {
            Ok(value) => {
                let py_value = js_value_to_python(py, &value, Some(&self.handle))?;
                future.call_method1(pyo3::intern!(py, "set_result"), (py_value.into_bound(py),))?;
            }
            Err(err) => {
                let exception = runtime_error_with_context(self.error_context, err).into_value(py);
                future.call_method1(pyo3::intern!(py, "set_exception"), (exception,))?;
            }
        }
        Ok(())
    }
}

/// Queue the conversion onto Python's event loop (`call_soon_threadsafe`).
fn schedule_js_future_result(
    py: Python<'_>,
    locals: &TaskLocals,
    future: &Py<PyAny>,
    result: RuntimeResult<JSValue>,
    handle: RuntimeHandle,
    error_context: &'static str,
) -> PyResult<()> {
    let event_loop = locals.event_loop(py);
    let context = locals.context(py);
    let setter = Py::new(
        py,
        JsAsyncResultSetter {
            future: future.clone_ref(py),
            result: Some(result),
            handle,
            error_context,
        },
    )?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "context"), context)?;
    event_loop.call_method(
        pyo3::intern!(py, "call_soon_threadsafe"),
        (setter.into_bound(py),),
        Some(&kwargs),
    )?;
    Ok(())
}

/// Immediately propagate a PyErr to the awaiting future if scheduling cannot be completed.
fn set_future_exception_immediate(py: Python<'_>, future: &Py<PyAny>, err: PyErr) -> PyResult<()> {
    let future = future.bind(py);
    if python_future_done(future)? {
        err.restore(py);
        return Ok(());
    }
    future.call_method1(pyo3::intern!(py, "set_exception"), (err.into_value(py),))?;
    Ok(())
}

/// Wrap an `asyncio.Future` in a coroutine, so `asyncio.create_task` accepts it
/// (it rejects bare futures). See `python/pydeno/_awaitable.py`.
fn as_coroutine<'py>(py: Python<'py>, future: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    py.import(pyo3::intern!(py, "pydeno._awaitable"))?
        .getattr(pyo3::intern!(py, "as_coroutine"))?
        .call1((future,))
}

/// Convert a Tokio future returning `JSValue` into a Python coroutine resolved on the loop thread.
fn bridge_js_future<'py, Fut>(
    py: Python<'py>,
    locals: TaskLocals,
    future: Fut,
    handle: RuntimeHandle,
    error_context: &'static str,
) -> PyResult<Bound<'py, PyAny>>
where
    Fut: Future<Output = RuntimeResult<JSValue>> + Send + 'static,
{
    let python_future = locals
        .event_loop(py)
        .call_method0(pyo3::intern!(py, "create_future"))?;

    let (cancel_tx, mut cancel_rx) = oneshot::channel::<()>();
    let cancel_callback = Py::new(
        py,
        JsAsyncCancelCallback {
            cancel_tx: Some(cancel_tx),
        },
    )?;
    python_future.call_method1(pyo3::intern!(py, "add_done_callback"), (cancel_callback,))?;

    let py_future: Py<PyAny> = python_future.clone().unbind();

    pyo3_tokio::get_runtime().spawn(async move {
        let scoped_future = pyo3_tokio::scope(locals.clone(), future);
        tokio::pin!(scoped_future);

        let result = tokio::select! {
            res = &mut scoped_future => res,
            _ = &mut cancel_rx => return,
        };

        Python::attach(|py| {
            if let Err(err) =
                schedule_js_future_result(py, &locals, &py_future, result, handle, error_context)
            {
                if let Err(set_err) = set_future_exception_immediate(py, &py_future, err) {
                    log::error!(
                        "Failed to propagate async error to Python future: {}",
                        set_err
                    );
                }
            }
        });
    });

    as_coroutine(py, python_future)
}

/// Run `make(handle, task_locals)` for the current asyncio task and bridge
/// its result back as a Python coroutine.
pub(crate) fn bridge_handle_call<'py, Fut>(
    py: Python<'py>,
    handle: RuntimeHandle,
    error_context: &'static str,
    make: impl FnOnce(RuntimeHandle, Option<TaskLocals>) -> Fut,
) -> PyResult<Bound<'py, PyAny>>
where
    Fut: Future<Output = RuntimeResult<JSValue>> + Send + 'static,
{
    let task_locals = pyo3_tokio::get_current_locals(py)?;
    let future = make(handle.clone(), Some(task_locals.clone()));
    bridge_js_future(py, task_locals, future, handle, error_context)
}
