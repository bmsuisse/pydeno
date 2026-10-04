"""Limit values are validated where they are accepted (slice B of #75).

A limit is a comparison: `elapsed > deadline`, `rss > max_memory`. Against NaN every comparison is
false, so a NaN deadline, memory ceiling or call cap is a limit that never fires, and the runtime
says nothing. Python's `json` module parses `NaN` and `Infinity`, so such a value can arrive from
a configuration file. Every layer that takes a limit refuses one that is not a finite number in
range, before any worker starts."""

from __future__ import annotations

import math
import time
from datetime import timedelta

import pytest

from pydeno import (
    AgentSandbox,
    AsyncIsolatedRuntime,
    IsolatedRuntime,
    Pydeno,
    PydenoTimeoutError,
    RuntimeConfig,
    RuntimeTimeout,
    SandboxPool,
)
from pydeno._isolated import _session_options

NON_FINITE = [math.nan, math.inf, -math.inf]
SECONDS = ["request_timeout", "max_host_wait", "write_stall_timeout"]


@pytest.mark.parametrize("name", SECONDS)
@pytest.mark.parametrize("value", [*NON_FINITE, 0, -1, -0.5, True, "3"])
def test_deadline_values_must_be_positive_finite_numbers(
    name: str, value: object
) -> None:
    with pytest.raises((ValueError, TypeError)):
        _session_options(**{name: value})


@pytest.mark.parametrize("value", [*NON_FINITE, -1, True, "2"])
def test_timeout_grace_must_be_a_finite_non_negative_number(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        _session_options(timeout_grace=value)


@pytest.mark.parametrize("value", [math.nan, 1.5, True, -1, "5"])
def test_max_host_calls_must_be_a_non_negative_int(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        _session_options(max_host_calls=value)


@pytest.mark.parametrize("value", [math.nan, 1.5, True, 0, "5"])
def test_max_inflight_host_calls_must_be_a_positive_int(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        _session_options(max_inflight_host_calls=value)


def test_valid_values_still_pass() -> None:
    opts = _session_options(
        request_timeout=timedelta(seconds=2),
        timeout_grace=0,
        max_host_calls=0,
        max_host_wait=0.5,
        max_inflight_host_calls=1,
        write_stall_timeout=None,
    )
    assert opts["_request_timeout"] == 2.0
    assert opts["_grace"] == 0.0
    assert opts["_stall"] is None
    assert _session_options(request_timeout=None)["_request_timeout"] is None


@pytest.mark.parametrize("value", [*NON_FINITE, 1.5e9, True, 0, -1])
@pytest.mark.parametrize("cls", [IsolatedRuntime, AsyncIsolatedRuntime])
def test_max_memory_must_be_a_positive_int(cls: type, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        cls(max_memory=value, prewarm=False)


@pytest.mark.parametrize("cls", [IsolatedRuntime, AsyncIsolatedRuntime])
def test_runtimes_refuse_a_nan_deadline(cls: type) -> None:
    with pytest.raises(ValueError):
        cls(request_timeout=math.nan, prewarm=False)


@pytest.mark.parametrize(
    "kw", [{"timeout": math.nan}, {"max_pause": math.inf}, {"timeout": -1}]
)
def test_agent_sandbox_refuses_non_finite_deadlines(kw: dict[str, float]) -> None:
    with pytest.raises((ValueError, TypeError)):
        AgentSandbox({}, **kw)


@pytest.mark.parametrize("kw", [{"timeout": math.nan}, {"max_pause": -math.inf}])
def test_agent_sandbox_on_an_adopted_runtime_refuses_non_finite_deadlines(
    kw: dict[str, float],
) -> None:
    rt = IsolatedRuntime(capture_console=True, random_seed=1)
    try:
        with pytest.raises((ValueError, TypeError)):
            AgentSandbox({}, runtime=rt, **kw)
    finally:
        rt.close()


def test_pool_checkout_refuses_a_nan_deadline() -> None:
    with SandboxPool(size=1) as pool:
        with pytest.raises(ValueError):
            pool.checkout(request_timeout=math.nan)
        with pytest.raises(ValueError):
            pool.checkout(max_host_wait=math.inf)


class TestConsoleCountsAgainstTheDeadline:
    """Console output is the guest's own work, not a tool call: the time the host spends handling
    it must not pause the hard deadline. Otherwise a guest that floods `console.*` stretches a
    1.5 s deadline by however slow the host's console handling is, up to `max_host_wait`."""

    HARD = 1.5
    CEILING = 5.0  # the deadline, start-up and slack; a paused deadline measured ~4-6x the limit

    @staticmethod
    def slow_console(level: str, args: list[object]) -> None:
        time.sleep(0.002)

    def test_sync(self) -> None:
        cfg = RuntimeConfig(on_console=self.slow_console)
        with IsolatedRuntime(cfg, request_timeout=self.HARD, max_host_wait=60) as rt:
            t = time.monotonic()
            with pytest.raises(RuntimeTimeout):
                rt.eval("for (;;) console.log('x')")
            assert time.monotonic() - t < self.CEILING

    async def test_async(self) -> None:
        cfg = RuntimeConfig(on_console=self.slow_console)
        async with AsyncIsolatedRuntime(
            cfg, request_timeout=self.HARD, max_host_wait=60
        ) as rt:
            t = time.monotonic()
            with pytest.raises(RuntimeTimeout):
                await rt.eval("for (;;) console.log('x')")
            assert time.monotonic() - t < self.CEILING

    def test_front_door_feed(self) -> None:
        limits = {"max_feed_duration_secs": self.HARD, "max_host_wait_secs": 60}
        with Pydeno(min_processes=1, limits=limits) as pool, pool.checkout() as session:
            t = time.monotonic()
            with pytest.raises(PydenoTimeoutError):
                session.feed_run(
                    "for (;;) console.log('x')",
                    print_callback=lambda stream, text: time.sleep(0.002),
                )
            assert time.monotonic() - t < self.CEILING

    def test_a_slow_tool_still_pauses_the_deadline(self) -> None:
        def slow() -> int:
            time.sleep(self.HARD + 0.5)
            return 1

        with IsolatedRuntime(request_timeout=self.HARD) as rt:
            rt.bind_function("slow", slow)
            assert rt.eval("slow()") == 1
