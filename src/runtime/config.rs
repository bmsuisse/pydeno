//! Runtime configuration for isolate-per-tenant execution.

use crate::runtime::js_value::{SerializationLimits, MAX_JS_BYTES, MAX_JS_DEPTH};
use crate::runtime::python::utils::validate_timeout_seconds;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::net::SocketAddr;
use std::time::Duration;

/// Suggested [`RuntimeConfig::force_kill_grace`]: ~60x the measured worst-case
/// polite-kill latency (see BENCHMARKS.md). Not a default, since enabling
/// force-kill costs ~10% per call (see `RuntimeHandle::recv_result`).
pub const SUGGESTED_FORCE_KILL_GRACE: Duration = Duration::from_millis(100);

fn parse_socket_addr(host: &str, port: u16) -> PyResult<SocketAddr> {
    if host.trim().is_empty() {
        return Err(PyValueError::new_err("Inspector host cannot be empty"));
    }
    if port == 0 {
        return Err(PyValueError::new_err(
            "Inspector port must be a positive integer",
        ));
    }

    let candidate = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    };

    candidate.parse::<SocketAddr>().map_err(|err| {
        PyValueError::new_err(format!("Invalid inspector address '{candidate}': {err}"))
    })
}

fn conflict_error() -> PyErr {
    PyValueError::new_err("snapshot and bootstrap cannot be used together")
}

fn check_callable(callback: &Py<PyAny>) -> PyResult<()> {
    Python::attach(|py| {
        if callback.bind(py).is_callable() {
            Ok(())
        } else {
            Err(PyValueError::new_err("on_console must be callable"))
        }
    })
}

fn snapshot_bytes(data: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let bytes = data.extract::<Vec<u8>>()?;
    if bytes.is_empty() {
        return Err(PyValueError::new_err("Snapshot bytes cannot be empty"));
    }
    Ok(bytes)
}

fn positive(value: usize, name: &str) -> PyResult<usize> {
    if value == 0 {
        return Err(PyValueError::new_err(format!(
            "{name} must be a positive integer"
        )));
    }
    Ok(value)
}

/// Inspector configuration for Chrome DevTools Protocol debugging.
///
/// Configures the WebSocket server that enables debugging via Chrome DevTools
/// or compatible debuggers. The inspector runs on a separate thread from the
/// runtime thread.
#[pyclass(module = "pydeno")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InspectorConfig {
    /// Socket address (IP and port) for the inspector server.
    pub address: SocketAddr,
    /// If true, runtime waits for debugger connection before executing code.
    pub wait_for_connection: bool,
    /// If true, execution pauses at the first statement for debugging.
    pub break_on_next_statement: bool,
    /// Optional target URL identifier (e.g., module name or script path).
    pub target_url: Option<String>,
    /// Optional display name shown in DevTools (e.g., "Main Runtime").
    pub display_name: Option<String>,
}

impl Default for InspectorConfig {
    fn default() -> Self {
        Self {
            address: SocketAddr::from(([127, 0, 0, 1], 9229)),
            wait_for_connection: false,
            break_on_next_statement: false,
            target_url: None,
            display_name: None,
        }
    }
}

impl InspectorConfig {
    pub fn socket_addr(&self) -> SocketAddr {
        self.address
    }
}

#[pymethods]
impl InspectorConfig {
    #[new]
    #[pyo3(signature = (
        host = "127.0.0.1",
        port = 9229,
        wait_for_connection = false,
        break_on_next_statement = false,
        target_url = None,
        display_name = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        host: &str,
        port: u16,
        wait_for_connection: bool,
        break_on_next_statement: bool,
        target_url: Option<String>,
        display_name: Option<String>,
    ) -> PyResult<Self> {
        let address = parse_socket_addr(host, port)?;
        Ok(Self {
            address,
            wait_for_connection,
            break_on_next_statement,
            target_url,
            display_name,
        })
    }

    #[getter]
    fn host(&self) -> String {
        self.address.ip().to_string()
    }

    #[getter]
    fn port(&self) -> u16 {
        self.address.port()
    }

    #[getter]
    fn wait_for_connection(&self) -> bool {
        self.wait_for_connection
    }

    #[setter]
    fn set_wait_for_connection(&mut self, wait: bool) {
        self.wait_for_connection = wait;
    }

    #[getter]
    fn break_on_next_statement(&self) -> bool {
        self.break_on_next_statement
    }

    #[setter]
    fn set_break_on_next_statement(&mut self, should_break: bool) {
        self.break_on_next_statement = should_break;
    }

    #[getter]
    fn target_url(&self) -> Option<String> {
        self.target_url.clone()
    }

    #[setter]
    fn set_target_url(&mut self, url: Option<String>) {
        self.target_url = url;
    }

    #[getter]
    fn display_name(&self) -> Option<String> {
        self.display_name.clone()
    }

    #[setter]
    fn set_display_name(&mut self, name: Option<String>) {
        self.display_name = name;
    }

    fn endpoint(&self) -> String {
        self.address.to_string()
    }

    fn __repr__(&self) -> String {
        format!(
            "InspectorConfig(host={:?}, port={}, wait_for_connection={}, break_on_next_statement={}, target_url={:?}, display_name={:?})",
            self.host(),
            self.port(),
            self.wait_for_connection,
            self.break_on_next_statement,
            self.target_url,
            self.display_name,
        )
    }
}

/// A Python callable in a config value; `Clone` goes through the GIL so
/// [`RuntimeConfig`] can keep `#[derive(Clone)]`.
#[derive(Debug)]
pub struct ConsoleCallback(pub Py<PyAny>);

impl Clone for ConsoleCallback {
    fn clone(&self) -> Self {
        Python::attach(|py| Self(self.0.clone_ref(py)))
    }
}

/// Runtime configuration for a single JavaScript isolate.
///
/// Defines heap limits, optional bootstrap code, inspector settings, and
/// serialization constraints for a V8 runtime instance.
#[pyclass(module = "pydeno")]
#[derive(Debug, Clone)]
pub struct RuntimeConfig {
    /// Maximum heap size in bytes (None = V8 default ~1.4 GB).
    pub max_heap_size: Option<usize>,

    /// Initial heap size in bytes (None = V8 default).
    pub initial_heap_size: Option<usize>,

    /// Optional timeout for script execution.
    pub execution_timeout: Option<Duration>,

    /// Bootstrap script to execute on startup (before user code).
    pub bootstrap_script: Option<String>,

    /// Enable console.log/error output (default: false).
    ///
    /// This controls only whether `deno_core`'s bootstrap `console` global is
    /// left intact (writing to the *process's* stdout/stderr) or stubbed to
    /// no-ops. It is orthogonal to [`Self::on_console`], which is what routes
    /// console output back to the Python caller. See `on_console` for how the
    /// two compose.
    pub enable_console: Option<bool>,

    /// Optional Python callable invoked as `on_console(level, args)` for every
    /// `console.*` call made by guest JS.
    ///
    /// `level` is the method name (`"log"`, `"info"`, `"warn"`, `"error"`,
    /// `"debug"`, `"trace"`); `args` is the list of arguments that call passed,
    /// converted with the same Python<->JS rules as any other host callback.
    ///
    /// Composition with [`Self::enable_console`] (both are independent knobs):
    ///
    /// | `enable_console` | `on_console` | behaviour                                     |
    /// |------------------|--------------|-----------------------------------------------|
    /// | `false` (default)| `None`       | console is stubbed to no-ops; output discarded |
    /// | `true`           | `None`       | output goes to the process's stdout/stderr     |
    /// | `false`          | set          | output goes *only* to the callback             |
    /// | `true`           | set          | callback fires, *then* the process's console    |
    ///
    /// The callback must be synchronous: `console.log` is synchronous in JS and
    /// an async callback would have nothing to await it. It is invoked on the
    /// runtime's own thread while guest JS is on the stack.
    pub on_console: Option<ConsoleCallback>,

    /// Optional inspector configuration for debugging.
    pub inspector: Option<InspectorConfig>,

    /// Startup snapshot bytes for faster initialization.
    pub snapshot: Option<Vec<u8>>,

    /// Maximum nesting depth for Python<->JS value serialization.
    pub max_serialization_depth: usize,

    /// Maximum serialized payload size for Python<->JS transfers (bytes).
    pub max_serialization_bytes: usize,

    /// How long a blocked caller keeps waiting for the runtime to acknowledge
    /// a termination before it gives up on the runtime thread entirely, or
    /// `None` (the default) to wait forever.
    ///
    /// This exists for one narrow case: a runtime whose *thread* is wedged
    /// inside a host callback that never returns, which no polite kill can
    /// reach. It is **not** needed for runaway JavaScript or for a never
    /// resolving promise -- both of those are already bounded by `timeout=`
    /// and by `TerminationHandle.terminate()`.
    ///
    /// Enabling it makes every synchronous call wait in slices rather than one
    /// block, which measured ~10% slower per call, so it is off unless asked
    /// for. [`SUGGESTED_FORCE_KILL_GRACE`] is a reasonable value. See
    /// `RuntimeHandle::recv_result` for the full semantics and
    /// `docs/termination.md` for what the caller observes.
    pub force_kill_grace: Option<Duration>,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            max_heap_size: None,
            initial_heap_size: None,
            execution_timeout: None,
            bootstrap_script: None,
            enable_console: Some(false),
            on_console: None,
            inspector: None,
            snapshot: None,
            max_serialization_depth: MAX_JS_DEPTH,
            max_serialization_bytes: MAX_JS_BYTES,
            force_kill_grace: None,
        }
    }
}

impl RuntimeConfig {
    /// Accepts seconds (int/float, which `f64` extraction covers) or a `timedelta`.
    fn duration_from_py_timeout(timeout_value: &Bound<'_, PyAny>) -> PyResult<Duration> {
        let seconds = if let Ok(seconds) = timeout_value.extract::<f64>() {
            seconds
        } else {
            let timedelta = timeout_value
                .py()
                .import("datetime")?
                .getattr("timedelta")?;
            if !timeout_value.is_instance(&timedelta)? {
                return Err(PyValueError::new_err(
                    "Timeout must be a number (seconds) or datetime.timedelta object",
                ));
            }
            timeout_value.getattr("total_seconds")?.call0()?.extract()?
        };
        validate_timeout_seconds(seconds)?;
        Ok(Duration::from_secs_f64(seconds))
    }

    fn optional_duration(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Duration>> {
        value.map(Self::duration_from_py_timeout).transpose()
    }

    pub fn serialization_limits(&self) -> SerializationLimits {
        SerializationLimits::new(self.max_serialization_depth, self.max_serialization_bytes)
    }
}

#[pymethods]
impl RuntimeConfig {
    /// Create a new runtime configuration with default settings.
    #[new]
    #[pyo3(signature = (
        max_heap_size = None,
        initial_heap_size = None,
        bootstrap = None,
        timeout = None,
        enable_console = Some(false),
        on_console = None,
        inspector = None,
        snapshot = None,
        max_serialization_depth = None,
        max_serialization_bytes = None,
        force_kill_grace = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        max_heap_size: Option<usize>,
        initial_heap_size: Option<usize>,
        bootstrap: Option<String>,
        timeout: Option<&Bound<'_, PyAny>>,
        enable_console: Option<bool>,
        on_console: Option<Py<PyAny>>,
        inspector: Option<InspectorConfig>,
        snapshot: Option<&Bound<'_, PyAny>>,
        max_serialization_depth: Option<usize>,
        max_serialization_bytes: Option<usize>,
        force_kill_grace: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        if bootstrap.is_some() && snapshot.is_some() {
            return Err(conflict_error());
        }
        let execution_timeout = Self::optional_duration(timeout)?;
        if let Some(callback) = &on_console {
            check_callable(callback)?;
        }
        let snapshot = snapshot.map(snapshot_bytes).transpose()?;
        let defaults = RuntimeConfig::default();
        Ok(RuntimeConfig {
            max_heap_size,
            initial_heap_size,
            execution_timeout,
            bootstrap_script: bootstrap,
            enable_console: enable_console.or(defaults.enable_console),
            on_console: on_console.map(ConsoleCallback),
            inspector,
            snapshot,
            max_serialization_depth: max_serialization_depth
                .map(|d| positive(d, "max_serialization_depth"))
                .transpose()?
                .unwrap_or(defaults.max_serialization_depth),
            max_serialization_bytes: max_serialization_bytes
                .map(|b| positive(b, "max_serialization_bytes"))
                .transpose()?
                .unwrap_or(defaults.max_serialization_bytes),
            force_kill_grace: Self::optional_duration(force_kill_grace)?,
        })
    }

    /// Get maximum heap size in bytes.
    #[getter]
    fn max_heap_size(&self) -> Option<usize> {
        self.max_heap_size
    }

    /// Set maximum heap size in bytes.
    #[setter]
    fn set_max_heap_size(&mut self, bytes: usize) {
        self.max_heap_size = Some(bytes);
    }

    /// Get initial heap size in bytes.
    #[getter]
    fn initial_heap_size(&self) -> Option<usize> {
        self.initial_heap_size
    }

    /// Set initial heap size in bytes.
    #[setter]
    fn set_initial_heap_size(&mut self, bytes: usize) {
        self.initial_heap_size = Some(bytes);
    }

    /// Get bootstrap script.
    #[getter]
    fn bootstrap(&self) -> Option<String> {
        self.bootstrap_script.clone()
    }

    /// Set bootstrap script.
    #[setter]
    fn set_bootstrap(&mut self, source: String) -> PyResult<()> {
        if self.snapshot.is_some() {
            return Err(conflict_error());
        }
        self.bootstrap_script = Some(source);
        Ok(())
    }

    /// Get execution timeout in seconds.
    #[getter]
    fn timeout(&self) -> Option<f64> {
        self.execution_timeout.map(|d| d.as_secs_f64())
    }

    /// Set execution timeout.
    /// Accepts float/int as seconds or datetime.timedelta object.
    #[setter]
    fn set_timeout<'py>(&mut self, timeout: &Bound<'py, PyAny>) -> PyResult<()> {
        self.execution_timeout = Some(Self::duration_from_py_timeout(timeout)?);
        Ok(())
    }

    /// Get enable console.
    #[getter]
    fn enable_console(&self) -> Option<bool> {
        self.enable_console
    }

    /// Get the console callback, if one is configured.
    #[getter]
    fn on_console(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.on_console.as_ref().map(|cb| cb.0.clone_ref(py))
    }

    /// Set the console callback (`None` clears it).
    #[setter]
    fn set_on_console(&mut self, callback: Option<Py<PyAny>>) -> PyResult<()> {
        if let Some(cb) = &callback {
            check_callable(cb)?;
        }
        self.on_console = callback.map(ConsoleCallback);
        Ok(())
    }

    /// Get inspector configuration if enabled.
    #[getter]
    fn inspector(&self) -> Option<InspectorConfig> {
        self.inspector.clone()
    }

    /// Set inspector configuration.
    #[setter]
    fn set_inspector(&mut self, inspector: Option<InspectorConfig>) {
        self.inspector = inspector;
    }

    /// Get snapshot bytes (copied).
    ///
    /// Warning: this clones the entire snapshot. For large snapshots, avoid
    /// calling this repeatedly.
    #[getter]
    fn snapshot(&self) -> Option<Vec<u8>> {
        self.snapshot.clone()
    }

    /// Set snapshot bytes.
    #[setter]
    fn set_snapshot(&mut self, data: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        let Some(data) = data else {
            self.snapshot = None;
            return Ok(());
        };
        if self.bootstrap_script.is_some() {
            return Err(conflict_error());
        }
        self.snapshot = Some(snapshot_bytes(data)?);
        Ok(())
    }

    /// Maximum serialization depth for Python<->JS transfers.
    #[getter]
    fn max_serialization_depth(&self) -> usize {
        self.max_serialization_depth
    }

    /// Set maximum serialization depth for Python<->JS transfers.
    #[setter]
    fn set_max_serialization_depth(&mut self, depth: usize) -> PyResult<()> {
        self.max_serialization_depth = positive(depth, "max_serialization_depth")?;
        Ok(())
    }

    /// Maximum serialized byte size for Python<->JS transfers.
    #[getter]
    fn max_serialization_bytes(&self) -> usize {
        self.max_serialization_bytes
    }

    /// Get the force-kill grace period in seconds, or `None` if disabled.
    #[getter]
    fn force_kill_grace(&self) -> Option<f64> {
        self.force_kill_grace.map(|d| d.as_secs_f64())
    }

    /// Set the force-kill grace period.
    /// Accepts float/int as seconds or a `datetime.timedelta`; `None` disables.
    #[setter]
    fn set_force_kill_grace(&mut self, grace: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.force_kill_grace = Self::optional_duration(grace)?;
        Ok(())
    }

    /// Set maximum serialized byte size for Python<->JS transfers.
    #[setter]
    fn set_max_serialization_bytes(&mut self, bytes: usize) -> PyResult<()> {
        self.max_serialization_bytes = positive(bytes, "max_serialization_bytes")?;
        Ok(())
    }

    fn __repr__(&self) -> String {
        format!("RuntimeConfig({:?})", self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyFloat;
    use std::f64;

    #[test]
    fn test_default_config() {
        let config = RuntimeConfig::default();
        assert!(config.max_heap_size.is_none());
        assert!(config.initial_heap_size.is_none());
        assert!(config.execution_timeout.is_none());
        assert!(config.bootstrap_script.is_none());
        assert_eq!(config.enable_console, Some(false));
        assert!(config.on_console.is_none());
        assert!(config.inspector.is_none());
        assert!(config.snapshot.is_none());
        assert_eq!(config.max_serialization_depth, MAX_JS_DEPTH);
        assert_eq!(config.max_serialization_bytes, MAX_JS_BYTES);
    }

    #[allow(clippy::field_reassign_with_default)]
    #[test]
    fn test_config_builder() {
        let mut config = RuntimeConfig::default();
        config.max_heap_size = Some(100 * 1024 * 1024);
        config.execution_timeout = Some(Duration::from_secs(30));

        assert_eq!(config.max_heap_size, Some(100 * 1024 * 1024));
        assert_eq!(config.execution_timeout, Some(Duration::from_secs(30)));
    }

    #[test]
    fn test_inspector_config_defaults() {
        let inspector = InspectorConfig::default();
        assert_eq!(inspector.host(), "127.0.0.1");
        assert_eq!(inspector.port(), 9229);
        assert!(!inspector.wait_for_connection);
        assert!(!inspector.break_on_next_statement);
        assert!(inspector.target_url().is_none());
        assert!(inspector.display_name().is_none());
    }

    #[test]
    fn test_runtime_config_with_inspector() {
        let mut inspector = InspectorConfig::default();
        inspector.set_wait_for_connection(true);
        inspector.set_break_on_next_statement(true);
        inspector.set_target_url(Some("module:main".to_string()));

        let config = RuntimeConfig {
            inspector: Some(inspector.clone()),
            ..RuntimeConfig::default()
        };

        let configured = config.inspector().expect("inspector config");
        assert!(configured.wait_for_connection());
        assert!(configured.break_on_next_statement());
        assert_eq!(configured.target_url(), Some("module:main".to_string()));
    }

    #[test]
    fn rejects_negative_timeout() {
        Python::attach(|py| {
            let timeout = PyFloat::new(py, -1.0).into_any();
            let err = RuntimeConfig::duration_from_py_timeout(&timeout);
            assert!(err.is_err());
        });
    }

    #[test]
    fn rejects_infinite_timeout() {
        Python::attach(|py| {
            let timeout = PyFloat::new(py, f64::INFINITY).into_any();
            let err = RuntimeConfig::duration_from_py_timeout(&timeout);
            assert!(err.is_err());
        });
    }
}
