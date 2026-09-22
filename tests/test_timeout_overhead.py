"""Regression tests for arming `timeout=` being nearly free.

Through pydeno 0.2.0, `SyncWatchdog` discovered its own cancellation by polling
an `AtomicBool` from a `thread::sleep(10ms)` loop, while the runtime thread
cancelled it and then *joined* it. The join therefore blocked until the
watchdog's current sleep chunk elapsed, so every call with a deadline armed
paid a fixed ~13ms (macOS overshoots a 10ms sleep request) on top of its real
work -- a ~200x penalty on a warm host tool call, and one that did not depend
on the deadline's *value* at all: 0.5s and 300s cost the same.

That made the library's whole advertised fast path unreachable in any safe
configuration, because a production caller must arm a timeout -- it is the only
kill switch for runaway guest code. Every tool-calling latency figure ever
quoted for pydeno had been measured on the unarmed path.

0.2.1 replaces the poll loop with a `Condvar`, so a cancel is *signalled* and
the join returns immediately.

On flakiness, since a timing assertion is the obvious thing to get wrong:

- The observable is a **ratio** against the unarmed path measured in the same
  process, moments apart, on the same warm runtime. General load on a CI box
  slows both paths together and largely cancels; an absolute microsecond bound
  is the part that genuinely moves between machines.
- The statistic is the **median** of many samples, not the mean or the min.
  The mean is hostage to a single scheduler stall. The min is actively wrong
  here: under the bug the armed path's *minimum* was ~0.3ms, because now and
  then the cancel happened to land just as the watchdog completed a sleep
  chunk, so a min-based assertion would have passed while the bug was present.
- The threshold is set more than an order of magnitude away from both sides.
  Measured: ~1.2x healthy, ~200x bugged. Asserting `< 15x` cannot fire from
  jitter, and cannot miss a return of a fixed multi-millisecond floor.
- The armed path does one extra thread spawn + join per call, which is real
  work and the reason the bound is 15x rather than 2x.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from pydeno import Runtime, ToolBridge

#: Samples per arm. Enough that the median is stable, small enough to stay fast.
SAMPLES = 60

#: Warmup calls, to get the isolate and the bound tool onto their hot paths.
WARMUP = 10

#: A fixed ~13ms floor against a ~0.06ms call is ~200x. Healthy is ~1.2x.
MAX_ARMED_OVERHEAD_RATIO = 15.0


async def _median_call_ms(rt: Runtime, timeout: float | None) -> float:
    """Median wall time of one host tool call, in milliseconds."""
    for _ in range(WARMUP):
        await rt.eval_async("tools.add(1, 2)", timeout=timeout)

    samples = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        await rt.eval_async("tools.add(1, 2)", timeout=timeout)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


async def test_arming_a_timeout_does_not_materially_slow_a_host_call() -> None:
    """An armed deadline must not cost a fixed tick of latency.

    Both arms run on one warm runtime so the comparison isolates exactly one
    variable: whether a watchdog was armed for the call.
    """
    bridge = ToolBridge({"add": lambda a, b: a + b})
    with Runtime() as rt:
        bridge.attach(rt)

        unarmed_ms = await _median_call_ms(rt, None)
        armed_ms = await _median_call_ms(rt, 30.0)

    ratio = armed_ms / unarmed_ms
    assert ratio < MAX_ARMED_OVERHEAD_RATIO, (
        f"a host call with timeout= armed took {armed_ms:.3f}ms vs "
        f"{unarmed_ms:.3f}ms unarmed ({ratio:.1f}x). A fixed multi-millisecond "
        "floor on the armed path is back -- most likely the watchdog is being "
        "polled for cancellation again instead of signalled."
    )


async def test_armed_call_cost_is_independent_of_the_deadline_value() -> None:
    """The distinguishing signature of the 0.2.0 bug.

    A cost that tracked the deadline would mean calls were genuinely waiting on
    their deadline. A cost identical at 0.5s and 300s means a fixed polling
    interval, which is what this asserts is gone. Both values are far larger
    than the call, so neither can legitimately be reached.
    """
    bridge = ToolBridge({"add": lambda a, b: a + b})
    with Runtime() as rt:
        bridge.attach(rt)

        unarmed_ms = await _median_call_ms(rt, None)
        short_ms = await _median_call_ms(rt, 0.5)
        long_ms = await _median_call_ms(rt, 300.0)

    budget = unarmed_ms * MAX_ARMED_OVERHEAD_RATIO
    assert short_ms < budget and long_ms < budget, (
        f"armed calls cost {short_ms:.3f}ms at timeout=0.5s and "
        f"{long_ms:.3f}ms at timeout=300s against {unarmed_ms:.3f}ms unarmed. "
        "A deadline-independent penalty means a fixed polling interval, not a "
        "deadline actually being hit."
    )


async def test_an_armed_deadline_still_fires() -> None:
    """The fix must not have cancelled the watchdog's actual job.

    A watchdog that never fires would make both tests above pass trivially.
    """
    with Runtime() as rt:
        with pytest.raises(Exception) as excinfo:
            await rt.eval_async("while (true) {}", timeout=0.25)

    assert "timed out" in str(excinfo.value).lower(), (
        f"expected a timeout error, got: {excinfo.value!r}"
    )


async def test_repeated_armed_calls_do_not_leak_watchdog_threads() -> None:
    """Each armed call spawns a watchdog; each must be joined before returning.

    `resolve_sync_watchdog` joins, so a leak here would mean an early-return
    path that skips it. Thread count is the direct observable.
    """
    import threading

    bridge = ToolBridge({"add": lambda a, b: a + b})
    with Runtime() as rt:
        bridge.attach(rt)
        await rt.eval_async("tools.add(1, 2)", timeout=30.0)
        baseline = threading.active_count()

        for _ in range(50):
            await rt.eval_async("tools.add(1, 2)", timeout=30.0)

        # Allow a just-joined thread a moment to be reaped by the OS.
        await asyncio.sleep(0.05)
        after = threading.active_count()

    assert after <= baseline, (
        f"thread count grew from {baseline} to {after} over 50 armed calls -- "
        "watchdog threads are not being joined"
    )
