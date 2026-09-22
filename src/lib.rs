use pyo3::prelude::*;

mod runtime;

// Re-exported so benches/ (an external crate target) can drive the runtime directly.
pub use runtime::ops::PythonOpMode;
pub use runtime::{RuntimeConfig, RuntimeHandle};

/// Python pydeno module
///
/// This module provides Python bindings to the pydeno JavaScript runtime.
#[pymodule]
fn _pydeno(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<runtime::python::Runtime>()?;
    m.add_class::<runtime::python::TerminationHandle>()?;
    m.add_class::<runtime::python::JsFunction>()?;
    m.add_class::<runtime::python::JsStream>()?;
    m.add_class::<runtime::python::JsUndefined>()?;
    m.add_class::<runtime::python::RuntimeStats>()?;
    m.add_class::<runtime::python::InspectorEndpoints>()?;
    m.add_class::<runtime::python::JsFunctionFinalizer>()?;
    m.add_class::<runtime::python::JsStreamFinalizer>()?;
    m.add_class::<runtime::python::PyStreamSource>()?;
    m.add_class::<runtime::python::PyStreamFinalizer>()?;
    m.add_class::<runtime::python::SnapshotBuilderPy>()?;
    m.add_class::<runtime::RuntimeConfig>()?;
    m.add_class::<runtime::config::InspectorConfig>()?;
    let js_error_type = m.py().get_type::<runtime::python::JavaScriptError>();
    js_error_type.setattr("__module__", "pydeno")?;
    m.add("JavaScriptError", js_error_type)?;

    let runtime_terminated_type = m.py().get_type::<runtime::python::RuntimeTerminated>();
    runtime_terminated_type.setattr("__module__", "pydeno")?;
    m.add("RuntimeTerminated", runtime_terminated_type)?;
    let runtime_force_killed_type = m.py().get_type::<runtime::python::RuntimeForceKilled>();
    runtime_force_killed_type.setattr("__module__", "pydeno")?;
    m.add("RuntimeForceKilled", runtime_force_killed_type)?;
    m.add(
        "SUGGESTED_FORCE_KILL_GRACE",
        runtime::config::SUGGESTED_FORCE_KILL_GRACE.as_secs_f64(),
    )?;
    let undefined: Py<PyAny> = runtime::python::get_js_undefined(m.py())?.into();
    m.add("undefined", undefined)?;
    m.add_function(pyo3::wrap_pyfunction!(
        runtime::python::_debug_active_runtime_threads,
        m
    )?)?;
    Ok(())
}
