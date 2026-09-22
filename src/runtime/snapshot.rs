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
/// # This isolate is not sandboxed
///
/// [`create_runtime`] below deliberately builds a bare `JsRuntimeForSnapshot`
/// with **no** `python_extension` (see `src/runtime/ops.rs`), so the bridge
/// bootstrap that deletes `Deno`, `__bootstrap` and `__infra` never runs
/// here. Every script passed to [`SnapshotBuilder::new`]'s `bootstrap_script`
/// or to [`SnapshotBuilder::execute_script`] therefore has the raw
/// `Deno.core.ops` table in scope -- including `op_print`, which writes
/// straight to the host process's stdout -- with no timeout, no heap cap and
/// no serialization limits. Snapshot input is host code at the same trust
/// level as the embedder; it must never come from an untrusted source.
///
/// This is not a gap in the guest sandbox: a `Runtime` created from the
/// resulting snapshot *does* run the bridge, which deletes those globals out
/// of the restored heap before any guest code sees them
/// (`tests/test_guest_globals.py` pins that under `RuntimeConfig(snapshot=)`
/// as well as on a fresh runtime).
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
        let runtime = self
            .runtime
            .as_mut()
            .ok_or_else(|| RuntimeError::internal("Snapshot has already been built"))?;
        execute_script(runtime, name, source)
    }

    pub fn build(mut self) -> RuntimeResult<Vec<u8>> {
        let runtime = self
            .runtime
            .take()
            .ok_or_else(|| RuntimeError::internal("Snapshot has already been built"))?;
        Ok(runtime.snapshot().into_vec())
    }
}

fn create_runtime() -> Result<JsRuntimeForSnapshot, CoreError> {
    // `JsRuntimeForSnapshot::try_new` registers its isolate with deno_core's
    // platform via `tokio::runtime::Handle::try_current()` (see
    // `spawn_runtime_thread` in src/runtime/runner.rs for the same fix on the
    // long-lived runtime path). `SnapshotBuilder` is constructed directly on
    // whatever thread calls it from Python, which normally never enters a
    // tokio runtime, so the isolate would otherwise be registered with no
    // handle -- one V8 delayed task away from an uncatchable
    // `std::process::abort()`. Unlike `spawn_runtime_thread`, this is a
    // synchronous one-shot call with no ongoing event loop to drive, so a
    // minimal current-thread runtime that's merely *entered* (no `block_on`)
    // for the duration of isolate creation is enough.
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
