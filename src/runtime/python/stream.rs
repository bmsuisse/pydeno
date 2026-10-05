//! Stream bindings: `JsStream` (JS -> Python) and `PyStreamSource` (Python -> JS).

use crate::runtime::conversion::js_value_to_python;
use crate::runtime::handle::RuntimeHandle;
use crate::runtime::js_value::{on_runtime_thread, RuntimeOwner};
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration};
use pyo3::prelude::*;
use pyo3_async_runtimes::tokio as pyo3_tokio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use super::error::{context, runtime_error_to_py};
use super::utils::attach_finalizer;

struct StreamSharedState {
    handle: Mutex<Option<RuntimeHandle>>,
    stream_id: u32,
    closed: AtomicBool,
}

impl StreamSharedState {
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
            if on_runtime_thread() {
                // A finalizer run by a collection inside a host function: waiting for the
                // runtime thread from itself never ends.
                handle.stream_cancel_detached(self.stream_id);
            } else if let Err(err) = handle.stream_cancel(self.stream_id) {
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
        let state = Arc::new(StreamSharedState {
            handle: Mutex::new(Some(handle.clone())),
            stream_id,
            closed: AtomicBool::new(false),
        });
        let py_obj = Py::new(
            py,
            Self {
                state: state.clone(),
            },
        )?;
        attach_finalizer(py, &py_obj, JsStreamFinalizer { state })?;
        // Rollback owns the ID until both wrapper and finalizer exist.
        handle.track_js_stream_id(stream_id);
        Ok(py_obj)
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
            .clone()
            .ok_or_else(|| PyRuntimeError::new_err("Runtime has been shut down"))?;
        let state = slf.state.clone();
        let future = async move {
            let mut chunk = handle
                .stream_read(state.stream_id)
                .await
                .map_err(runtime_error_to_py)?;
            if chunk.done {
                state.mark_remote_closed();
                return Err(PyErr::new::<PyStopAsyncIteration, _>(""));
            }
            let chunk_value = chunk.value.take();
            Python::attach(|py| match chunk_value {
                Some(value) => js_value_to_python(py, &value, Some(&handle)),
                None => Ok(py.None()),
            })
        };
        pyo3_tokio::future_into_py(py, future)
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.state.cancel());
        Ok(())
    }

    fn __repr__(&self) -> String {
        if self.state.is_closed() {
            "<JsStream (closed)>".to_string()
        } else {
            format!("<JsStream id={}>", self.state.stream_id)
        }
    }
}

#[pyclass(module = "_pydeno", name = "_JsStreamFinalizer")]
pub(crate) struct JsStreamFinalizer {
    state: Arc<StreamSharedState>,
}

#[pymethods]
impl JsStreamFinalizer {
    fn __call__(&self, py: Python<'_>) {
        py.detach(|| self.state.cancel());
    }
}

/// Refusal for a source passed to a runtime other than the one that created it (issue #98).
const OTHER_RUNTIME: &str = "this stream source belongs to a different runtime; a stream source \
                             can only be passed to the runtime that created it";
/// Refusal for a source whose runtime has been closed.
const OWNER_CLOSED: &str =
    "the runtime that created this stream source has been closed or terminated";

/// Not `unsendable`: a host function runs on the runtime thread and may return a stream source,
/// which is then converted there (issue #58), so its state is behind a `Mutex` and an atomic.
#[pyclass(module = "_pydeno", weakref)]
pub struct PyStreamSource {
    handle: Mutex<Option<RuntimeHandle>>,
    stream_id: u32,
    closed: AtomicBool,
    /// The runtime that allocated `stream_id`. Stream ids are per runtime, so the same id in
    /// another runtime names a different stream (issue #98).
    owner: Option<RuntimeOwner>,
}

impl PyStreamSource {
    pub fn new(py: Python<'_>, handle: RuntimeHandle, iterable: Py<PyAny>) -> PyResult<Py<Self>> {
        let owner = handle.serialization_limits().owner;
        let task_locals = pyo3_tokio::get_current_locals(py)?;
        let stream_id = handle
            .register_py_stream(iterable, task_locals)
            .map_err(context("Stream registration failed"))?;
        let finalizer = PyStreamFinalizer {
            handle: Mutex::new(Some(handle.clone())),
            stream_id,
        };
        let py_obj = Py::new(
            py,
            Self {
                handle: Mutex::new(Some(handle)),
                stream_id,
                closed: AtomicBool::new(false),
                owner,
            },
        )?;
        attach_finalizer(py, &py_obj, finalizer)?;
        Ok(py_obj)
    }

    /// The stream id to send to the runtime identified by `target`, refused unless that is the
    /// runtime that created this source and it is still open.
    pub(crate) fn stream_id_for_transfer(&self, target: Option<RuntimeOwner>) -> PyResult<u32> {
        if self.closed.load(Ordering::SeqCst) {
            return Err(PyRuntimeError::new_err("Stream has been closed"));
        }
        {
            let handle = self.handle.lock().unwrap_or_else(PoisonError::into_inner);
            match handle.as_ref() {
                None => return Err(PyRuntimeError::new_err("Runtime has been shut down")),
                Some(handle) if handle.is_shutdown_nonblocking() => {
                    return Err(PyRuntimeError::new_err(OWNER_CLOSED))
                }
                Some(_) => {}
            }
        }
        if target.is_none() || target != self.owner {
            return Err(PyRuntimeError::new_err(OTHER_RUNTIME));
        }
        Ok(self.stream_id)
    }
}

#[pymethods]
impl PyStreamSource {
    #[pyo3(name = "close")]
    fn close_py(&self, py: Python<'_>) {
        if self.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        // Take the handle out first so the lock is not held while cancelling.
        let handle = self
            .handle
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        if let Some(handle) = handle {
            handle.cancel_py_stream(py, self.stream_id);
        }
    }

    fn __repr__(&self) -> String {
        if self.closed.load(Ordering::SeqCst) {
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

#[pymethods]
impl PyStreamFinalizer {
    fn __call__(&self, py: Python<'_>) {
        if let Some(handle) = self.handle.lock().unwrap().take() {
            handle.cancel_py_stream(py, self.stream_id);
        }
    }
}
