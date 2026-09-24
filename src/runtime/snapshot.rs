//! Snapshot builder utilities built on top of `deno_core::JsRuntimeForSnapshot`.

use crate::runtime::error::{JsExceptionDetails, RuntimeError, RuntimeResult};
use deno_core::error::CoreError;
use deno_core::JsRuntimeForSnapshot;
use deno_core::RuntimeOptions;

#[derive(Debug, Clone)]
pub struct SnapshotBuilderConfig {
    pub bootstrap_script: Option<String>,
    pub enable_console: Option<bool>,
}

impl Default for SnapshotBuilderConfig {
    fn default() -> Self {
        Self {
            bootstrap_script: None,
            enable_console: Some(false),
        }
    }
}

/// Builds a V8 startup snapshot from host-supplied JavaScript.
///
/// Not sandboxed: the isolate has no `python_extension`, so scripts see the raw
/// `Deno.core.ops` (incl. `op_print`) with no timeout, heap cap or limits.
/// Snapshot input must be trusted host code. Runtimes restored from the
/// snapshot still strip those globals (`tests/test_guest_globals.py`).
pub struct SnapshotBuilder {
    runtime: Option<JsRuntimeForSnapshot>,
}

impl SnapshotBuilder {
    pub fn new(config: SnapshotBuilderConfig) -> RuntimeResult<Self> {
        let mut runtime = create_runtime().map_err(|err| {
            RuntimeError::internal(format!("Failed to initialize snapshot runtime: {err}"))
        })?;

        if config.enable_console == Some(false) {
            disable_console(&mut runtime)?;
        }

        if let Some(script) = config.bootstrap_script {
            execute_script(&mut runtime, "<bootstrap>", &script)?;
        }

        Ok(Self {
            runtime: Some(runtime),
        })
    }

    pub fn execute_script(&mut self, name: &str, source: &str) -> RuntimeResult<()> {
        execute_script(
            self.runtime.as_mut().ok_or_else(already_built)?,
            name,
            source,
        )
    }

    pub fn build(mut self) -> RuntimeResult<Vec<u8>> {
        let runtime = self.runtime.take().ok_or_else(already_built)?;
        Ok(runtime.snapshot().into_vec())
    }
}

fn already_built() -> RuntimeError {
    RuntimeError::internal("Snapshot has already been built")
}

fn create_runtime() -> Result<JsRuntimeForSnapshot, CoreError> {
    // `try_new` registers the isolate with the current tokio handle; without
    // one (plain Python caller thread) a V8 delayed task later aborts the
    // process. Merely entering a current-thread runtime is enough here.
    let tokio_rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("failed to build tokio runtime");
    let _tokio_enter = tokio_rt.enter();
    JsRuntimeForSnapshot::try_new(RuntimeOptions {
        is_main: true,
        ..Default::default()
    })
}

fn disable_console(runtime: &mut JsRuntimeForSnapshot) -> RuntimeResult<()> {
    execute_script(
        runtime,
        "<disable_console>",
        r#"
        (() => {
            const noop = () => {};
            const stub = new Proxy(Object.create(null), { get: () => noop });
            const existing = globalThis.console;
            if (typeof existing === "object" && existing !== null) {
                for (const key of Reflect.ownKeys(existing)) {
                    try { existing[key] = noop; } catch (_) {}
                }
                return;
            }
            globalThis.console = stub;
        })();
        "#,
    )
}

fn execute_script(
    runtime: &mut JsRuntimeForSnapshot,
    name: &str,
    source: &str,
) -> RuntimeResult<()> {
    runtime
        .execute_script(name.to_string(), source.to_string())
        .map(|_| ())
        .map_err(|err| RuntimeError::javascript(JsExceptionDetails::from_js_error(*err)))
}
