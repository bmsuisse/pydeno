//! Python bindings entry point that ties together the submodules.
use crate::runtime::runner;
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyRuntimeError};
use pyo3::prelude::*;
use std::sync::OnceLock;

mod bridge;
pub(crate) mod error;
mod function;
pub(crate) mod runtime;
pub(crate) mod snapshot;
pub(crate) mod stats;
mod stream;
pub(crate) mod utils;

pub(crate) use error::runtime_error_to_py;
pub use function::JsFunction;
pub(crate) use function::JsFunctionFinalizer;
pub use runtime::{Runtime, TerminationHandle};
pub use snapshot::SnapshotBuilderPy;
pub use stats::{InspectorEndpoints, RuntimeStats};
pub use stream::{JsStream, PyStreamSource};
pub(crate) use stream::{JsStreamFinalizer, PyStreamFinalizer};

create_exception!(crate::runtime::python, JavaScriptError, PyException);
create_exception!(crate::runtime::python, RuntimeTerminated, PyRuntimeError);
// A force-kill *is* a termination, so `except RuntimeTerminated` still catches it.
create_exception!(
    crate::runtime::python,
    RuntimeForceKilled,
    RuntimeTerminated
);
// Subclasses `RuntimeError` so pre-0.4.1 `except RuntimeError` handlers still
// catch timeouts. Not named `TimeoutError`: the builtin derives from `OSError`.
create_exception!(crate::runtime::python, RuntimeTimeout, PyRuntimeError);

#[pyfunction]
pub fn _debug_active_runtime_threads() -> usize {
    runner::active_runtime_threads()
}

#[pyclass(module = "_pydeno")]
pub struct JsUndefined;

#[pymethods]
impl JsUndefined {
    #[new]
    fn __new__() -> PyResult<Self> {
        Err(PyRuntimeError::new_err(
            "JsUndefined is a singleton; use pydeno.undefined",
        ))
    }

    fn __repr__(&self) -> &'static str {
        "JsUndefined"
    }

    fn __str__(&self) -> &'static str {
        "undefined"
    }

    fn __bool__(&self) -> bool {
        false
    }
}

static JS_UNDEFINED_SINGLETON: OnceLock<Py<JsUndefined>> = OnceLock::new();

pub(crate) fn get_js_undefined(py: Python<'_>) -> PyResult<Py<JsUndefined>> {
    if let Some(existing) = JS_UNDEFINED_SINGLETON.get() {
        return Ok(existing.clone_ref(py));
    }
    let value = Py::new(py, JsUndefined)?;
    Ok(JS_UNDEFINED_SINGLETON.get_or_init(|| value).clone_ref(py))
}
