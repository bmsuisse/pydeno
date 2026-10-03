//! Process-global V8 flags, settable once before the first isolate exists.
//!
//! V8 flags are global and are frozen once V8 initialises, so this is only
//! meaningful in a process that has not created a `Runtime` yet. `IsolatedRuntime`
//! uses it in its worker process (`--jitless`, ...), where "global" means "this
//! one disposable guest". Calling it in a host that already runs guests raises.

use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

static V8_STARTED: AtomicBool = AtomicBool::new(false);

/// Record that an isolate is being created; flags can no longer change.
pub(crate) fn mark_v8_started() {
    V8_STARTED.store(true, Ordering::SeqCst);
}

/// Pass `flags` to V8. Returns the ones V8 did not recognise.
///
/// Raises `RuntimeError` once any `Runtime` has been created in this process.
#[pyfunction]
pub fn _set_v8_flags(flags: Vec<String>) -> PyResult<Vec<String>> {
    if V8_STARTED.load(Ordering::SeqCst) {
        return Err(PyRuntimeError::new_err(
            "V8 flags must be set before the first Runtime is created in this process",
        ));
    }
    // V8 ignores argv[0], so give it one and strip it from what comes back.
    let mut argv = vec!["pydeno".to_string()];
    argv.extend(flags);
    let mut unknown = deno_core::v8_set_flags(argv);
    unknown.retain(|arg| arg != "pydeno");
    Ok(unknown)
}
