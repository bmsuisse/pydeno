//! Rust-level Criterion benches for the core runtime, bypassing the Python API.
//! Run with: cargo bench --features bench
use _peno::{PythonOpMode, RuntimeConfig, RuntimeHandle};
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use pyo3::prelude::*;
use std::hint::black_box;
use std::time::Duration;

fn bench_isolate_creation(c: &mut Criterion) {
    c.bench_function("isolate_creation_and_close", |b| {
        b.iter(|| {
            let mut handle = RuntimeHandle::spawn(RuntimeConfig::default()).unwrap();
            handle.close().unwrap();
        });
    });
}

fn bench_eval_throughput(c: &mut Criterion) {
    let handle = RuntimeHandle::spawn(RuntimeConfig::default()).unwrap();
    c.bench_function("simple_eval_throughput", |b| {
        b.iter(|| black_box(handle.eval_sync("1 + 41").unwrap()));
    });
}

// op dispatch needs a real Python callable, so this bench embeds an interpreter
// (see the "bench" Cargo feature: pyo3/auto-initialize instead of extension-module).
fn bench_op_dispatch(c: &mut Criterion) {
    Python::initialize();
    let handle = RuntimeHandle::spawn(RuntimeConfig::default()).unwrap();

    let op_id = Python::attach(|py| {
        let handler: Py<PyAny> = py
            .eval(
                std::ffi::CString::new("lambda x: x * 2")
                    .unwrap()
                    .as_c_str(),
                None,
                None,
            )
            .unwrap()
            .into();
        handle
            .register_op("hostFn".to_string(), PythonOpMode::Sync, handler)
            .unwrap()
    });
    handle
        .eval_sync(&format!(
            "globalThis.hostFn = (...args) => __host_op_sync__({op_id}, ...args); void 0;"
        ))
        .unwrap();

    c.bench_function("host_callback_op_dispatch", |b| {
        b.iter(|| black_box(handle.eval_sync("hostFn(21)").unwrap()));
    });
}

// P1's direct before/after: every timed call used to spawn *and join* a
// whole OS thread just to arm a deadline, on top of the eval itself. Compare
// directly against `simple_eval_throughput` above (same eval, no timeout
// configured) -- after the persistent per-runtime watchdog thread
// (`Watchdog` in `src/runtime/runner.rs`), `arm`/`disarm` only take a mutex,
// so the two should land within noise of each other.
fn bench_timed_eval_throughput(c: &mut Criterion) {
    let config = RuntimeConfig {
        execution_timeout: Some(Duration::from_secs(5)),
        ..Default::default()
    };
    let handle = RuntimeHandle::spawn(config).unwrap();
    c.bench_function("timed_eval_throughput", |b| {
        b.iter(|| black_box(handle.eval_sync("1 + 41").unwrap()));
    });
}

fn bench_termination_handle(c: &mut Criterion) {
    let mut group = c.benchmark_group("termination_handle");

    group.bench_with_input(
        BenchmarkId::new("is_terminated_check", "idle"),
        &(),
        |b, _| {
            let handle = RuntimeHandle::spawn(RuntimeConfig::default()).unwrap();
            let ctrl = handle.termination_controller();
            b.iter(|| black_box(ctrl.is_terminated()));
        },
    );

    group.bench_with_input(
        BenchmarkId::new("terminate_round_trip", "idle_runtime"),
        &(),
        |b, _| {
            b.iter_with_setup(
                || RuntimeHandle::spawn(RuntimeConfig::default()).unwrap(),
                |handle: RuntimeHandle| {
                    handle.terminate().unwrap();
                    black_box(())
                },
            );
        },
    );

    group.finish();
}

criterion_group!(
    benches,
    bench_isolate_creation,
    bench_eval_throughput,
    bench_timed_eval_throughput,
    bench_op_dispatch,
    bench_termination_handle
);
criterion_main!(benches);
