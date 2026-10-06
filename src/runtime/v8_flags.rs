//! Process-global V8 flags, settable once before the first isolate exists.
//!
//! V8 flags are global and are frozen once V8 initialises, so this is only
//! meaningful in a process that has not created a `Runtime` yet. `IsolatedRuntime`
//! uses it in its worker process (`--jitless`, ...), where "global" means "this
//! one disposable guest". Calling it in a host that already runs guests raises.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

static V8_STARTED: AtomicBool = AtomicBool::new(false);
static FLAGS_SET: Mutex<Option<Vec<String>>> = Mutex::new(None);

/// Whether the flags this process gave V8 are exactly `wanted`, in any order. No flags at all is a
/// set too (the empty one). The built-in startup snapshot asks, since V8 refuses a snapshot under
/// flags other than the ones it was made with.
pub(crate) fn flags_are(wanted: &[&str]) -> bool {
    let set = FLAGS_SET.lock().unwrap_or_else(|e| e.into_inner());
    let mut given: Vec<&str> = set.iter().flatten().map(String::as_str).collect();
    let mut wanted = wanted.to_vec();
    given.sort_unstable();
    wanted.sort_unstable();
    given == wanted
}

/// Record that an isolate is being created; flags can no longer change.
pub(crate) fn mark_v8_started() {
    V8_STARTED.store(true, Ordering::SeqCst);
}

/// Flags deno_core's platform initialisation (`setup.rs`, `v8_init`, deno_core 0.412) sets *after*
/// anything set here and before V8 starts, with the value it sets. V8 keeps the last value, so a
/// flag here that disagrees is silently undone: `--no-harmony-temporal` would be reported as
/// applied while `Temporal` stays. Re-check this list on every deno_core bump.
const SET_BY_DENO_CORE: &[(&str, bool)] = &[
    ("validate-asm", false),
    ("turbo-fast-api-calls", true),
    ("harmony-temporal", true),
    ("js-float16array", true),
    ("js-explicit-resource-management", true),
    ("js-source-phase-imports", true),
    ("js-defer-import-eval", true),
    ("enable-queue-microtask", true),
];

/// The deno_core-controlled flag `flag` would change, if any. V8 spells a boolean flag
/// `--name`, `--noname` or `--no-name`, with `_` and `-` interchangeable. It refuses
/// `--name=value` for a boolean flag by itself (that comes back unrecognised, and the worker then
/// refuses to start), so that spelling is not a mention here.
fn overridden_by_deno_core(flag: &str) -> Option<&'static str> {
    let name = flag.trim_start_matches('-').replace('_', "-");
    if name.contains('=') {
        return None;
    }
    let (bare, negated) = match name.strip_prefix("no") {
        Some(rest)
            if SET_BY_DENO_CORE
                .iter()
                .any(|(n, _)| *n == rest.trim_start_matches('-')) =>
        {
            (rest.trim_start_matches('-').to_string(), true)
        }
        _ => (name, false),
    };
    SET_BY_DENO_CORE
        .iter()
        .find(|(n, _)| *n == bare)
        .filter(|(_, value)| *value == negated)
        .map(|(n, _)| *n)
}

/// The flags in `flags` that deno_core's start-up would silently undo, by name. The parent calls
/// this before it starts a worker, so a refused flag is a `ValueError` there, not a dead worker.
#[pyfunction]
pub fn _v8_flags_undone_by_engine(flags: Vec<String>) -> Vec<String> {
    flags
        .iter()
        .filter_map(|flag| overridden_by_deno_core(flag))
        .map(str::to_string)
        .collect()
}

/// Pass `flags` to V8. Returns the ones V8 did not recognise.
///
/// Raises `RuntimeError` once any `Runtime` has been created in this process, and `ValueError`
/// for a flag deno_core would silently undo (see `SET_BY_DENO_CORE`): a restriction the caller
/// asked for must not be reported as applied when it is not.
#[pyfunction]
pub fn _set_v8_flags(flags: Vec<String>) -> PyResult<Vec<String>> {
    if V8_STARTED.load(Ordering::SeqCst) {
        return Err(PyRuntimeError::new_err(
            "V8 flags must be set before the first Runtime is created in this process",
        ));
    }
    let undone = _v8_flags_undone_by_engine(flags.clone());
    if !undone.is_empty() {
        return Err(PyValueError::new_err(format!(
            "these V8 flags cannot take effect: the engine's own start-up sets {undone:?} \
             afterwards and V8 keeps that value"
        )));
    }
    FLAGS_SET
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get_or_insert_with(Vec::new)
        .extend(flags.iter().cloned());
    // V8 ignores argv[0], so give it one and strip it from what comes back.
    let mut argv = vec!["pydeno".to_string()];
    argv.extend(flags.iter().cloned());
    let mut unknown = deno_core::v8_set_flags(argv);
    unknown.retain(|arg| arg != "pydeno");
    Ok(unknown)
}

#[cfg(test)]
mod tests {
    use super::overridden_by_deno_core;

    #[test]
    fn flags_deno_core_would_undo_are_detected_in_every_spelling() {
        for flag in [
            "--no-harmony-temporal",
            "--noharmony-temporal",
            "--no-harmony_temporal",
            "--no-js-explicit-resource-management",
            "--validate-asm",
            "-no-enable-queue-microtask",
        ] {
            assert!(overridden_by_deno_core(flag).is_some(), "{flag}");
        }
    }

    #[test]
    fn agreeing_and_unrelated_flags_pass() {
        for flag in [
            "--harmony-temporal",
            "--no-validate-asm",
            "--jitless",
            "--freeze-flags-after-init",
            "--random-seed=4",
            "--no-expose-gc",
            "--node-snapshot",
            // V8 refuses `=value` on a boolean flag by itself (reported as unrecognised, so the
            // worker refuses to start), so it needs no verdict here, agreeing or not.
            "--harmony-temporal=false",
            "--harmony-temporal=true",
            "--validate-asm=false",
        ] {
            assert!(overridden_by_deno_core(flag).is_none(), "{flag}");
        }
    }
}
