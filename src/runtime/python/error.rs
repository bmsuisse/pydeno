//! Helper utilities for translating runtime errors into Python exceptions.
use crate::runtime::error::{JsExceptionDetails, RuntimeError};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::{JavaScriptError, RuntimeForceKilled, RuntimeTerminated, RuntimeTimeout};

fn with_prefix(context: Option<&str>, message: String) -> String {
    match context {
        Some(prefix) if !prefix.is_empty() => format!("{prefix}: {message}"),
        _ => message,
    }
}

fn build_js_exception(py: Python<'_>, details: JsExceptionDetails, context: Option<&str>) -> PyErr {
    let py_err = PyErr::new::<JavaScriptError, _>(with_prefix(context, details.summary()));
    let value = py_err.value(py);

    // `None` fields become Python `None`; attribute errors are ignored.
    let _ = value.setattr("name", details.name);
    let _ = value.setattr("message", details.message);
    let _ = value.setattr("stack", details.stack);

    let frames_list = PyList::empty(py);
    for frame in &details.frames {
        let frame_dict = PyDict::new(py);
        if let Some(function_name) = &frame.function_name {
            let _ = frame_dict.set_item("function_name", function_name);
        }
        if let Some(file_name) = &frame.file_name {
            let _ = frame_dict.set_item("file_name", file_name);
        }
        if let Some(line_number) = frame.line_number {
            let _ = frame_dict.set_item("line_number", line_number);
        }
        if let Some(column_number) = frame.column_number {
            let _ = frame_dict.set_item("column_number", column_number);
        }
        let _ = frames_list.append(frame_dict);
    }
    let _ = value.setattr("frames", frames_list);

    py_err
}

/// Build a PyErr from a runtime error, optionally tagging a context string.
fn runtime_error_to_py_with(py: Python<'_>, err: RuntimeError, context: Option<&str>) -> PyErr {
    match err {
        RuntimeError::JavaScript(details) => build_js_exception(py, details, context),
        RuntimeError::Timeout { context: msg } => {
            PyErr::new::<RuntimeTimeout, _>(with_prefix(context, msg))
        }
        RuntimeError::Internal { context: msg } => {
            PyRuntimeError::new_err(with_prefix(context, msg))
        }
        RuntimeError::Terminated { reason } => {
            let base = reason
                .filter(|msg| !msg.is_empty())
                .unwrap_or_else(|| "Runtime terminated".to_string());
            PyErr::new::<RuntimeTerminated, _>(with_prefix(context, base))
        }
        RuntimeError::ForceKilled { context: msg } => {
            PyErr::new::<RuntimeForceKilled, _>(with_prefix(context, msg))
        }
    }
}

pub(crate) fn runtime_error_to_py(err: RuntimeError) -> PyErr {
    Python::attach(|py| runtime_error_to_py_with(py, err, None))
}

/// Include context when converting runtime failures to Python exceptions.
pub(crate) fn runtime_error_with_context(context: &str, err: RuntimeError) -> PyErr {
    Python::attach(|py| runtime_error_to_py_with(py, err, Some(context)))
}

/// `map_err` adapter for [`runtime_error_with_context`].
pub(crate) fn context(context: &'static str) -> impl FnOnce(RuntimeError) -> PyErr {
    move |err| runtime_error_with_context(context, err)
}
