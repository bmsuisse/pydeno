use pyo3::prelude::*;

mod runtime;
mod scanner;

// Re-exported so benches/ (an external crate target) can drive the runtime directly.
pub use runtime::ops::PythonOpMode;
pub use runtime::{RuntimeConfig, RuntimeHandle};

/// The engine build this extension carries: crate version, target triple and the V8 version it
/// links. A V8 snapshot is only valid for exactly this combination (see `_snapshot_auth`).
#[pyfunction]
fn _build_identity() -> String {
    format!(
        "pydeno-{}+{}+v8-{}",
        env!("CARGO_PKG_VERSION"),
        env!("PYDENO_BUILD_TARGET"),
        deno_core::v8::V8::get_version()
    )
}

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
    let runtime_timeout_type = m.py().get_type::<runtime::python::RuntimeTimeout>();
    runtime_timeout_type.setattr("__module__", "pydeno")?;
    m.add("RuntimeTimeout", runtime_timeout_type)?;
    m.add(
        "SUGGESTED_FORCE_KILL_GRACE",
        runtime::config::SUGGESTED_FORCE_KILL_GRACE.as_secs_f64(),
    )?;
    // Whether this build includes the DevTools inspector server (cargo feature `inspector`).
    m.add("_INSPECTOR_AVAILABLE", cfg!(feature = "inspector"))?;
    let undefined: Py<PyAny> = runtime::python::get_js_undefined(m.py())?.into();
    m.add("undefined", undefined)?;
    m.add_function(pyo3::wrap_pyfunction!(
        runtime::python::_debug_active_runtime_threads,
        m
    )?)?;
    m.add_function(pyo3::wrap_pyfunction!(runtime::v8_flags::_set_v8_flags, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(
        runtime::python::_set_terse_guest_errors,
        m
    )?)?;
    m.add_function(pyo3::wrap_pyfunction!(
        runtime::v8_flags::_v8_flags_undone_by_engine,
        m
    )?)?;
    m.add_function(pyo3::wrap_pyfunction!(
        runtime::wire::_wire_decode_values,
        m
    )?)?;
    m.add_function(pyo3::wrap_pyfunction!(
        runtime::wire_json::_wire_loads_decoded,
        m
    )?)?;
    m.add_function(pyo3::wrap_pyfunction!(runtime::wire_json::_wire_dumps, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(scanner::_scan_source, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(_build_identity, m)?)?;
    m.add(
        "WireNativeError",
        m.py().get_type::<runtime::wire::WireNativeError>(),
    )?;
    // Before finalization, stop Tokio workers from entering Python and wait for those inside.
    let exit_hook = pyo3::wrap_pyfunction!(runtime::python::utils::_wait_for_background_attach, m)?;
    m.py()
        .import("atexit")?
        .call_method1("register", (exit_hook,))?;
    Ok(())
}
