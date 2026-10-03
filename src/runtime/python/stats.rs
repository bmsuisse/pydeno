//! Runtime statistics and inspector endpoint bindings.
use crate::runtime::inspector::InspectorMetadata;
use crate::runtime::stats::RuntimeStatsSnapshot;
use pyo3::prelude::*;

/// Declares a getter-only pyclass whose `$ty` fields are copied 1:1 from a
/// same-named field of `$src`, plus any `$extra` fields computed by `$init`.
macro_rules! mirror_pyclass {
    (
        #[pyclass($($attr:tt)*)] $(#[$meta:meta])*
        struct $name:ident from $src:ty => $ty:ty { $($field:ident),* $(,)? }
        $(extra $extra:ident: $extra_ty:ty = |$s:ident| $init:expr;)?
    ) => {
        #[pyclass($($attr)*)]
        $(#[$meta])*
        pub struct $name {
            $(#[pyo3(get)] $extra: $extra_ty,)?
            $(#[pyo3(get)] $field: $ty,)*
        }

        impl From<$src> for $name {
            fn from(src: $src) -> Self {
                Self {
                    $($extra: { let $s = &src; $init },)?
                    $($field: src.$field,)*
                }
            }
        }
    };
}

mirror_pyclass! {
    #[pyclass(module = "_pydeno")]
    struct RuntimeStats from RuntimeStatsSnapshot => u64 {
        heap_total_bytes, heap_used_bytes, external_memory_bytes, physical_total_bytes,
        total_execution_time_ms, last_execution_time_ms, eval_sync_count, eval_async_count,
        eval_module_sync_count, eval_module_async_count, call_function_async_count,
        call_function_sync_count, active_async_ops, open_resources, active_timers,
        active_intervals, active_js_streams, active_py_streams, total_js_streams,
        total_py_streams, bytes_streamed_js_to_py, bytes_streamed_py_to_js,
    }
    extra last_execution_kind: Option<String> =
        |s| s.last_execution_kind.map(|kind| kind.as_str().to_string());
}

impl RuntimeStats {
    pub fn from_snapshot(snapshot: RuntimeStatsSnapshot) -> Self {
        snapshot.into()
    }
}

#[pymethods]
impl RuntimeStats {
    fn __repr__(&self) -> String {
        format!(
            "RuntimeStats(heap_used_bytes={}, total_execution_time_ms={}, last_execution_kind={}, active_async_ops={}, open_resources={}, eval_sync_count={}, call_function_async_count={}, call_function_sync_count={}, active_js_streams={}, active_py_streams={})",
            self.heap_used_bytes,
            self.total_execution_time_ms,
            self.last_execution_kind
                .as_deref()
                .unwrap_or("None"),
            self.active_async_ops,
            self.open_resources,
            self.eval_sync_count,
            self.call_function_async_count,
            self.call_function_sync_count,
            self.active_js_streams,
            self.active_py_streams
        )
    }
}

mirror_pyclass! {
    #[pyclass(module = "pydeno")]
    #[derive(Clone)]
    struct InspectorEndpoints from InspectorMetadata => String {
        id, websocket_url, devtools_frontend_url, title, description, target_url,
        favicon_url, host,
    }
}

#[pymethods]
impl InspectorEndpoints {
    fn __repr__(&self) -> String {
        format!(
            "InspectorEndpoints(id={}, websocket_url={}, devtools_frontend_url={})",
            self.id, self.websocket_url, self.devtools_frontend_url
        )
    }
}
