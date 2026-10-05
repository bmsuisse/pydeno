//! Helpers shared by multiple bindings (timeout normalization, finalizers, exit guard).
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

/// Set by the `atexit` hook: background threads stop entering Python from then on.
static INTERPRETER_EXITING: AtomicBool = AtomicBool::new(false);
/// Background threads currently inside (or about to enter) [`attach_unless_exiting`].
static BACKGROUND_ATTACHED: AtomicUsize = AtomicUsize::new(0);
/// How long the `atexit` hook waits for background threads to leave Python.
const EXIT_DRAIN_LIMIT: Duration = Duration::from_secs(2);

struct BackgroundAttachGuard;

impl Drop for BackgroundAttachGuard {
    fn drop(&mut self) {
        BACKGROUND_ATTACHED.fetch_sub(1, Ordering::SeqCst);
    }
}

/// `Python::attach` for a thread Python does not know about (a Tokio worker), or `None` once
/// the interpreter has started to exit.
///
/// CPython before 3.14 ends a thread that takes the GIL after finalization began, inside the
/// GIL wait (`pthread_exit`), and that aborts the process. A call such as
/// `loop.call_soon_threadsafe` releases and retakes the GIL, so such a thread could be caught
/// while the main thread finalizes. The `atexit` hook ([`_wait_for_background_attach`]) runs
/// before finalization: it stops new entries and waits for the ones under way.
pub(crate) fn attach_unless_exiting<F, R>(f: F) -> Option<R>
where
    F: for<'py> FnOnce(Python<'py>) -> R,
{
    BACKGROUND_ATTACHED.fetch_add(1, Ordering::SeqCst);
    let _guard = BackgroundAttachGuard;
    if INTERPRETER_EXITING.load(Ordering::SeqCst) {
        return None;
    }
    Some(Python::attach(f))
}

/// Registered with `atexit` at import: see [`attach_unless_exiting`].
#[pyfunction]
pub(crate) fn _wait_for_background_attach(py: Python<'_>) {
    INTERPRETER_EXITING.store(true, Ordering::SeqCst);
    py.detach(|| {
        let deadline = Instant::now() + EXIT_DRAIN_LIMIT;
        while BACKGROUND_ATTACHED.load(Ordering::SeqCst) > 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
    });
}

pub(crate) fn validate_timeout_seconds(seconds: f64) -> PyResult<()> {
    if !seconds.is_finite() {
        return Err(PyValueError::new_err("Timeout must be finite"));
    }
    if seconds < 0.0 {
        return Err(PyValueError::new_err("Timeout cannot be negative"));
    }
    if seconds == 0.0 {
        return Err(PyValueError::new_err("Timeout cannot be zero"));
    }
    // Deadlines must fit both the millisecond command ABI and the platform clock.
    if seconds >= u64::MAX as f64 / 1000.0
        || Duration::try_from_secs_f64(seconds)
            .ok()
            .and_then(|duration| Instant::now().checked_add(duration))
            .is_none()
    {
        return Err(PyValueError::new_err("Timeout is too large"));
    }
    Ok(())
}

pub(crate) fn normalize_timeout_to_ms(timeout: Option<&Bound<PyAny>>) -> PyResult<Option<u64>> {
    let Some(timeout_value) = timeout else {
        return Ok(None);
    };

    let duration = if let Ok(seconds) = timeout_value.extract::<f64>() {
        validate_timeout_seconds(seconds)?;
        Duration::from_secs_f64(seconds)
    } else if let Ok(seconds) = timeout_value.extract::<u64>() {
        validate_timeout_seconds(seconds as f64)?;
        Duration::from_secs(seconds)
    } else if let Ok(seconds) = timeout_value.extract::<i64>() {
        validate_timeout_seconds(seconds as f64)?;
        Duration::from_secs(seconds as u64)
    } else {
        let py = timeout_value.py();
        let timedelta = py.import("datetime")?.getattr("timedelta")?;
        if !timeout_value.is_instance(&timedelta)? {
            return Err(PyValueError::new_err(
                "Timeout must be a number (seconds), datetime.timedelta, or None",
            ));
        }
        let total_seconds: f64 = timeout_value.getattr("total_seconds")?.call0()?.extract()?;
        validate_timeout_seconds(total_seconds)?;
        Duration::from_secs_f64(total_seconds)
    };

    Ok(Some(
        u64::try_from(duration.as_millis()).unwrap_or(u64::MAX),
    ))
}

/// Register `finalizer` to run via `weakref.finalize` when `target` is collected.
pub(crate) fn attach_finalizer<T, F>(py: Python<'_>, target: &Py<T>, finalizer: F) -> PyResult<()>
where
    F: pyo3::PyClass + Into<pyo3::PyClassInitializer<F>>,
{
    let finalize = py
        .import("weakref")?
        .getattr(pyo3::intern!(py, "finalize"))?;
    finalize.call1((target.clone_ref(py), Py::new(py, finalizer)?))?;
    Ok(())
}
