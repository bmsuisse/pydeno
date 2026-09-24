//! Tokio-based JavaScript runtime: each runtime owns one V8 isolate on a
//! dedicated OS thread with a Tokio event loop.

pub mod config;
pub mod conversion;
pub mod error;
pub mod handle;
pub mod inspector;
pub mod js_value;
pub mod loader;
pub mod ops;
pub mod python;
pub mod runner;
pub mod snapshot;
pub mod stats;
pub mod stream;

#[allow(unused_imports)] // Re-exported for downstream crates.
pub use config::RuntimeConfig;
#[allow(unused_imports)]
pub use error::{JsExceptionDetails, JsFrameSummary, RuntimeError, RuntimeResult};
#[allow(unused_imports)] // Re-exported for downstream crates.
pub use handle::RuntimeHandle;

#[cfg(test)]
mod tests {
    use super::js_value::JSValue;
    use super::*;
    use std::thread;

    fn spawn(config: RuntimeConfig) -> RuntimeHandle {
        RuntimeHandle::spawn(config).unwrap()
    }

    #[test]
    fn test_runtime_lifecycle() {
        let mut handle = spawn(RuntimeConfig::default());
        assert!(!handle.is_shutdown());
        assert!(matches!(
            handle.eval_sync("40 + 2").unwrap(),
            JSValue::Int(42)
        ));
        handle.close().unwrap();
        assert!(handle.is_shutdown());
    }

    #[test]
    fn test_multiple_runtimes_sequential() {
        for i in 0..3 {
            let mut handle = spawn(RuntimeConfig::default());
            let result = handle.eval_sync(&format!("{} * 2", i)).unwrap();
            assert!(matches!(result, JSValue::Int(val) if val == i * 2));
            handle.close().unwrap();
        }
    }

    #[test]
    fn test_concurrent_runtimes() {
        let handles: Vec<_> = (0..3).map(|_| spawn(RuntimeConfig::default())).collect();
        let threads: Vec<_> = handles
            .into_iter()
            .enumerate()
            .map(|(i, handle)| {
                thread::spawn(move || {
                    let result = handle.eval_sync(&format!("{} + 100", i)).unwrap();
                    let expected = (i + 100) as i64;
                    assert!(matches!(result, JSValue::Int(val) if val == expected));
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
    }

    #[test]
    fn test_runtime_with_heap_limits() {
        let handle = spawn(RuntimeConfig {
            max_heap_size: Some(10 * 1024 * 1024),
            initial_heap_size: Some(1024 * 1024),
            ..RuntimeConfig::default()
        });
        let result = handle.eval_sync("'hello'").unwrap();
        assert!(matches!(result, JSValue::String(s) if s == "hello"));
    }

    #[test]
    fn test_runtime_terminates_when_heap_limit_exceeded() {
        // Headroom for deno_core's bootstrap; many small allocations let the
        // near-heap-limit termination win over V8's own RangeError.
        let mut handle = spawn(RuntimeConfig {
            max_heap_size: Some(10 * 1024 * 1024),
            initial_heap_size: Some(4 * 1024 * 1024),
            ..RuntimeConfig::default()
        });
        let result = handle
            .eval_sync("let arr = []; while (true) { arr.push(new Array(100000).fill('x')); }");
        assert!(matches!(result, Err(RuntimeError::Terminated { .. })));
        handle.close().unwrap();
    }

    #[test]
    fn test_runtime_with_bootstrap() {
        let handle = spawn(RuntimeConfig {
            bootstrap_script: Some("globalThis.VERSION = '1.0.0';".to_string()),
            ..RuntimeConfig::default()
        });
        let result = handle.eval_sync("globalThis.VERSION").unwrap();
        assert!(matches!(result, JSValue::String(s) if s == "1.0.0"));
    }

    #[test]
    fn test_runtime_state_persistence() {
        let handle = spawn(RuntimeConfig::default());
        for (code, expected) in [
            ("var counter = 0; counter", 0),
            ("++counter", 1),
            ("counter", 1),
        ] {
            assert!(matches!(handle.eval_sync(code).unwrap(), JSValue::Int(v) if v == expected));
        }
    }

    #[test]
    fn test_runtime_with_snapshot_bytes() {
        let mut builder =
            snapshot::SnapshotBuilder::new(snapshot::SnapshotBuilderConfig::default()).unwrap();
        builder
            .execute_script("init.js", "globalThis.answer = 42;")
            .unwrap();
        let handle = spawn(RuntimeConfig {
            snapshot: Some(builder.build().unwrap()),
            ..RuntimeConfig::default()
        });
        assert!(matches!(
            handle.eval_sync("answer").unwrap(),
            JSValue::Int(42)
        ));
    }
}
