"""Regression tests for the dispatcher parking instead of busy-spinning.

Before pydeno 0.2.0, `RuntimeDispatcher::run` selected between `cmd_rx.recv()` and
`tokio::task::yield_now()`. `yield_now` is always immediately ready, so the loop
never blocked and every live `Runtime` burned CPU for its entire lifetime
whether or not it had any work to do. On an 8-core machine that measured at
~13% of a core per idle runtime, scaling linearly until the machine saturated
(~170% at K=8, ~684% at K=64), and per-call latency degraded ~2.3x once the
spinning threads outnumbered the cores.

These tests would fail if that busy-spin came back.

On flakiness, since a CPU assertion is the obvious thing to get wrong: the
measurement is `resource.getrusage(RUSAGE_SELF)`, which counts **only this
process's** CPU time, not the machine's. Other load on a CI box cannot inflate
it -- it can only *delay* our threads, which lowers the number and so can never
produce a false failure. The gap being asserted is also enormous (0% measured
against a ~50% busy-spin floor at K=4), not a few percent. The latency test is
deliberately written as a ratio against a same-process K=1 baseline rather than
an absolute microsecond bound, because absolute timings are the part that does
move between machines.
"""

import gc
import os
import resource
import statistics
import time

import pytest

import pydeno


IDLE_WINDOW = 2.0


def _cpu_seconds() -> float:
    """CPU seconds consumed by this process (all threads, user + system)."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _make_runtime() -> pydeno.Runtime:
    runtime = pydeno.Runtime()
    runtime.bind_function("host_add", lambda a, b: a + b)
    runtime.eval("host_add(1, 2)")  # warm the bridge so nothing is lazy
    return runtime


def _idle_cpu_percent(
    runtimes: list[pydeno.Runtime], window: float = IDLE_WINDOW
) -> float:
    """Percent of one core this process burns while every runtime sits idle."""
    time.sleep(0.3)  # let construction settle
    cpu_before, wall_before = _cpu_seconds(), time.perf_counter()
    time.sleep(window)
    cpu_after, wall_after = _cpu_seconds(), time.perf_counter()
    return 100.0 * (cpu_after - cpu_before) / (wall_after - wall_before)


def _median_call_us(runtime: pydeno.Runtime, calls: int = 600) -> float:
    for _ in range(300):  # warm up; the first few hundred calls are slower
        runtime.eval("host_add(1, 2)")
    samples = []
    for _ in range(calls):
        start = time.perf_counter()
        runtime.eval("host_add(1, 2)")
        samples.append(time.perf_counter() - start)
    return statistics.median(samples) * 1e6


@pytest.mark.parametrize("count", [1, 4])
def test_idle_runtimes_consume_almost_no_cpu(count: int) -> None:
    """K idle retained runtimes must not burn CPU.

    The busy-spin floor was ~13% of a core *per* runtime, so the threshold is
    set well below what a single spinning runtime would produce while leaving
    generous room for incidental work (GC, the test process itself).
    """
    runtimes = [_make_runtime() for _ in range(count)]
    try:
        idle_cpu = _idle_cpu_percent(runtimes)
    finally:
        for runtime in runtimes:
            runtime.close()
        gc.collect()

    # A single busy-spinning runtime measured ~13%; K spinning runtimes measured
    # ~13% x K. 5% per runtime is comfortably under that floor and far above the
    # 0.0% a parked dispatcher actually produces.
    budget = 5.0 * count
    assert idle_cpu < budget, (
        f"{count} idle runtime(s) burned {idle_cpu:.1f}% CPU "
        f"(budget {budget:.1f}%) -- the dispatcher is not parking when idle"
    )


def test_idle_cpu_does_not_scale_with_runtime_count() -> None:
    """Idle cost must not grow with K.

    This is the shape of the bug rather than its magnitude: a busy-spin makes
    idle CPU proportional to the number of live runtimes, which is what capped
    session affinity at roughly the core count.
    """
    few = [_make_runtime() for _ in range(1)]
    try:
        idle_few = _idle_cpu_percent(few)
    finally:
        for runtime in few:
            runtime.close()
        gc.collect()

    many = [_make_runtime() for _ in range(8)]
    try:
        idle_many = _idle_cpu_percent(many)
    finally:
        for runtime in many:
            runtime.close()
        gc.collect()

    # Busy-spinning would put idle_many at ~8x idle_few (measured 12.9% -> 170%).
    # Parked, both are ~0, so an absolute ceiling is the robust assertion here --
    # a ratio between two near-zero numbers is meaningless.
    assert idle_many < 20.0, (
        f"8 idle runtimes burned {idle_many:.1f}% CPU (1 runtime: {idle_few:.1f}%) "
        "-- idle cost is scaling with runtime count"
    )


@pytest.mark.skipif(
    (os.cpu_count() or 1) > 16,
    reason="needs a machine where 24 runtimes comfortably outnumber the cores",
)
def test_call_latency_survives_more_runtimes_than_cores() -> None:
    """Per-call latency must not collapse once K exceeds the core count.

    With the busy-spin, K runtimes meant K threads competing for the cores, and
    a call on one of them measured ~2.3x its K=1 latency once K passed the core
    count. Parked runtimes do not compete, so latency stays flat.

    Asserted as a ratio against a K=1 baseline measured in this same process, so
    it does not depend on the absolute speed of the machine.
    """
    baseline_rt = _make_runtime()
    try:
        baseline_us = _median_call_us(baseline_rt)
    finally:
        baseline_rt.close()
        gc.collect()

    count = 3 * (os.cpu_count() or 4)
    runtimes = [_make_runtime() for _ in range(count)]
    try:
        loaded_us = _median_call_us(runtimes[0])
    finally:
        for runtime in runtimes:
            runtime.close()
        gc.collect()

    ratio = loaded_us / baseline_us
    # Measured ~2.3x with the busy-spin and ~1.0x parked. 2x is the midpoint and
    # leaves room for scheduling noise on a loaded machine.
    assert ratio < 2.0, (
        f"latency with {count} live runtimes was {loaded_us:.1f}us vs "
        f"{baseline_us:.1f}us at K=1 ({ratio:.2f}x) -- runtimes are competing "
        "for CPU while idle"
    )
