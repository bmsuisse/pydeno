//! Stream bindings: `JsStream` (JS -> Python) and `PyStreamSource` (Python -> JS).

use crate::runtime::conversion::js_value_to_python;
use crate::runtime::handle::RuntimeHandle;
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration};
use pyo3::prelude::*;
use pyo3_async_runtimes::tokio as pyo3_tokio;
use std::cell::{Cell, RefCell};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

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
        let state = Arc::new(StreamSharedState {
            handle: Mutex::new(Some(handle)),
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

    fn close(&self) -> PyResult<()> {
        self.state.cancel();
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
    fn __call__(&self) {
        self.state.cancel();
    }
}

#[pyclass(module = "_pydeno", unsendable, weakref)]
pub struct PyStreamSource {
    handle: RefCell<Option<RuntimeHandle>>,
    stream_id: u32,
    closed: Cell<bool>,
}

impl PyStreamSource {
    pub fn new(py: Python<'_>, handle: RuntimeHandle, iterable: Py<PyAny>) -> PyResult<Py<Self>> {
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
                handle: RefCell::new(Some(handle)),
                stream_id,
                closed: Cell::new(false),
            },
        )?;
        attach_finalizer(py, &py_obj, finalizer)?;
        Ok(py_obj)
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
}

#[pymethods]
impl PyStreamSource {
    #[pyo3(name = "close")]
    fn close_py(&self) {
        if self.closed.replace(true) {
            return;
        }
        if let Some(handle) = self.handle.borrow_mut().take() {
            handle.cancel_py_stream_async(self.stream_id);
        }
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

#[pymethods]
impl PyStreamFinalizer {
    fn __call__(&self) {
        if let Some(handle) = self.handle.lock().unwrap().take() {
            handle.cancel_py_stream_async(self.stream_id);
        }
    }
}
