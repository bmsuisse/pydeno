use std::time::Duration;

use crate::runtime::stream::StreamStatsSnapshot;
use deno_core::stats::{RuntimeActivity, RuntimeActivitySnapshot};

/// JavaScript entry point that was executed; discriminants index
/// [`RuntimeExecutionCounters`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeCallKind {
    EvalSync,
    EvalAsync,
    EvalModuleSync,
    EvalModuleAsync,
    CallFunctionAsync,
    CallFunctionSync,
}

impl RuntimeCallKind {
    pub fn as_str(self) -> &'static str {
        match self {
            RuntimeCallKind::EvalSync => "eval_sync",
            RuntimeCallKind::EvalAsync => "eval_async",
            RuntimeCallKind::EvalModuleSync => "eval_module_sync",
            RuntimeCallKind::EvalModuleAsync => "eval_module_async",
            RuntimeCallKind::CallFunctionAsync => "call_function_async",
            RuntimeCallKind::CallFunctionSync => "call_function_sync",
        }
    }
}

/// Per-kind call counters, indexed by `RuntimeCallKind as usize`.
pub type RuntimeExecutionCounters = [u64; 6];

pub type ExecutionSnapshot = RuntimeStatsState;

#[derive(Debug, Default, Clone)]
pub struct RuntimeStatsState {
    total_execution: Duration,
    last_execution: Option<Duration>,
    last_call_kind: Option<RuntimeCallKind>,
    counters: RuntimeExecutionCounters,
}

impl RuntimeStatsState {
    pub fn record(&mut self, kind: RuntimeCallKind, elapsed: Duration) {
        self.total_execution = self.total_execution.saturating_add(elapsed);
        self.last_execution = Some(elapsed);
        self.last_call_kind = Some(kind);
        let count = &mut self.counters[kind as usize];
        *count = count.saturating_add(1);
    }

    pub fn snapshot(&self) -> ExecutionSnapshot {
        self.clone()
    }
}

#[derive(Debug, Default, Clone)]
pub struct HeapSnapshot {
    pub heap_total_bytes: u64,
    pub heap_used_bytes: u64,
    pub external_memory_bytes: u64,
    pub physical_total_bytes: u64,
}

#[derive(Debug, Default, Clone)]
pub struct ActivitySummary {
    pub active_async_ops: u64,
    pub open_resources: u64,
    pub active_timers: u64,
    pub active_intervals: u64,
}

impl ActivitySummary {
    pub fn from_snapshot(snapshot: RuntimeActivitySnapshot) -> Self {
        let mut summary = ActivitySummary::default();
        for activity in snapshot.active {
            let count = match activity {
                RuntimeActivity::AsyncOp(..) => &mut summary.active_async_ops,
                RuntimeActivity::Resource(..) => &mut summary.open_resources,
                RuntimeActivity::Timer(..) => &mut summary.active_timers,
                RuntimeActivity::Interval(..) => &mut summary.active_intervals,
            };
            *count = count.saturating_add(1);
        }
        summary
    }
}

#[derive(Debug, Clone)]
pub struct RuntimeStatsSnapshot {
    pub heap_total_bytes: u64,
    pub heap_used_bytes: u64,
    pub external_memory_bytes: u64,
    pub physical_total_bytes: u64,
    pub total_execution_time_ms: u64,
    pub last_execution_time_ms: u64,
    pub last_execution_kind: Option<RuntimeCallKind>,
    pub eval_sync_count: u64,
    pub eval_async_count: u64,
    pub eval_module_sync_count: u64,
    pub eval_module_async_count: u64,
    pub call_function_async_count: u64,
    pub call_function_sync_count: u64,
    pub active_async_ops: u64,
    pub open_resources: u64,
    pub active_timers: u64,
    pub active_intervals: u64,
    pub active_js_streams: u64,
    pub active_py_streams: u64,
    pub total_js_streams: u64,
    pub total_py_streams: u64,
    pub bytes_streamed_js_to_py: u64,
    pub bytes_streamed_py_to_js: u64,
}

impl RuntimeStatsSnapshot {
    pub fn new(
        heap: HeapSnapshot,
        execution: ExecutionSnapshot,
        activity: ActivitySummary,
        streams: StreamStatsSnapshot,
    ) -> Self {
        let [eval_sync_count, eval_async_count, eval_module_sync_count, eval_module_async_count, call_function_async_count, call_function_sync_count] =
            execution.counters;
        RuntimeStatsSnapshot {
            heap_total_bytes: heap.heap_total_bytes,
            heap_used_bytes: heap.heap_used_bytes,
            external_memory_bytes: heap.external_memory_bytes,
            physical_total_bytes: heap.physical_total_bytes,
            total_execution_time_ms: duration_to_u64_ms(execution.total_execution),
            last_execution_time_ms: execution.last_execution.map_or(0, duration_to_u64_ms),
            last_execution_kind: execution.last_call_kind,
            eval_sync_count,
            eval_async_count,
            eval_module_sync_count,
            eval_module_async_count,
            call_function_async_count,
            call_function_sync_count,
            active_async_ops: activity.active_async_ops,
            open_resources: activity.open_resources,
            active_timers: activity.active_timers,
            active_intervals: activity.active_intervals,
            active_js_streams: streams.active_js_streams,
            active_py_streams: streams.active_py_streams,
            total_js_streams: streams.total_js_streams,
            total_py_streams: streams.total_py_streams,
            bytes_streamed_js_to_py: streams.bytes_streamed_js_to_py,
            bytes_streamed_py_to_js: streams.bytes_streamed_py_to_js,
        }
    }
}

/// Whole milliseconds (floored), saturating at `u64::MAX`.
fn duration_to_u64_ms(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_stats_state_record_and_snapshot() {
        let mut state = RuntimeStatsState::default();
        state.record(RuntimeCallKind::EvalSync, Duration::from_millis(10));
        state.record(RuntimeCallKind::EvalAsync, Duration::from_millis(25));

        let snapshot = state.snapshot();
        assert_eq!(snapshot.total_execution, Duration::from_millis(35));
        assert_eq!(snapshot.last_call_kind, Some(RuntimeCallKind::EvalAsync));
        assert_eq!(snapshot.last_execution, Some(Duration::from_millis(25)));
        assert_eq!(snapshot.counters[RuntimeCallKind::EvalSync as usize], 1);
        assert_eq!(snapshot.counters[RuntimeCallKind::EvalAsync as usize], 1);

        let rendered = RuntimeStatsSnapshot::new(
            HeapSnapshot::default(),
            snapshot,
            ActivitySummary::default(),
            StreamStatsSnapshot::default(),
        );
        assert_eq!(rendered.eval_async_count, 1);
        assert_eq!(rendered.active_js_streams, 0);
    }
}
