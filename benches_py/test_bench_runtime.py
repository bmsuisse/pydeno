"""pytest-benchmark suite for the Python API. Run: pytest benches_py/ --benchmark-only"""

from __future__ import annotations

import threading
import time

import peno
import pytest


def test_cold_start(benchmark):
    def cold_start():
        runtime = peno.Runtime()
        result = runtime.eval("1 + 41")
        runtime.close()
        return result

    assert benchmark(cold_start) == 42


@pytest.fixture
def warm_runtime():
    runtime = peno.Runtime()
    runtime.eval("1")  # warm up the isolate before timing
    yield runtime
    runtime.close()


def test_steady_state_100_evals(benchmark, warm_runtime):
    def run_100():
        for _ in range(100):
            warm_runtime.eval("1 + 41")

    benchmark(run_100)


def test_steady_state_1000_evals(benchmark, warm_runtime):
    def run_1000():
        for _ in range(1000):
            warm_runtime.eval("1 + 41")

    benchmark(run_1000)


def test_host_callback_round_trip(benchmark, warm_runtime):
    warm_runtime.bind_function("double", lambda x: x * 2)

    def call_host():
        return warm_runtime.eval("double(21)")

    assert benchmark(call_host) == 42


# Watchdog-termination proof: exact scenario from tests/test_termination_handle.py,
# but with a short delay so pedantic rounds finish quickly while still exercising
# the real cross-thread TerminationHandle.terminate() path.
def _watchdog_terminate_scenario(delay_s: float = 0.05) -> float:
    runtime = peno.Runtime()
    handle = runtime.termination_handle()

    def watchdog() -> None:
        time.sleep(delay_s)
        handle.terminate()

    t = threading.Thread(target=watchdog, daemon=True)
    start = time.perf_counter()
    t.start()
    try:
        runtime.eval("while(true){}")
    except Exception:
        pass
    elapsed = time.perf_counter() - start
    t.join(timeout=5)
    runtime.close()
    return elapsed


def test_watchdog_termination_overhead(benchmark):
    elapsed = benchmark.pedantic(_watchdog_terminate_scenario, rounds=20, iterations=1)
    # Overhead beyond the artificial 50ms watchdog delay -- the real termination cost.
    assert elapsed >= 0.05


def test_normal_completing_eval_baseline(benchmark, warm_runtime):
    """Baseline for comparison against the termination path: an eval that just finishes."""
    benchmark(lambda: warm_runtime.eval("1 + 41"))


@pytest.fixture
def warm_timed_runtime():
    """Same as `warm_runtime`, but with `execution_timeout` configured, so
    every `eval` arms a deadline on the persistent per-runtime watchdog
    thread (see `Watchdog` in `src/runtime/runner.rs`) instead of leaving one
    unset."""
    runtime = peno.Runtime(peno.RuntimeConfig(timeout=5.0))
    runtime.eval("1")  # warm up the isolate before timing
    yield runtime
    runtime.close()


def test_timed_eval_baseline(benchmark, warm_timed_runtime):
    """The direct before/after for P1: before, every timed call spawned *and
    joined* a whole OS thread just to arm a deadline, on top of the eval
    itself. After, `arm`/`disarm` on the persistent per-runtime watchdog
    thread only take a mutex. Compare directly against
    `test_normal_completing_eval_baseline` -- the two should land within
    noise of each other, not the ~2-3x spawn/join gap this replaced."""
    benchmark(lambda: warm_timed_runtime.eval("1 + 41"))
