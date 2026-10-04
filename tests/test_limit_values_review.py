"""Review round on #79 (slice B of #75): limit types and ranges, the console deadline allowance,
the console in-flight exemption, SessionPool's durations, and the front door's default printer."""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import time
from decimal import Decimal
from fractions import Fraction

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    AsyncPydeno,
    AsyncSandboxPool,
    InMemoryJournalStore,
    IsolatedRuntime,
    Pydeno,
    PydenoTimeoutError,
    RuntimeConfig,
    RuntimeTimeout,
    SandboxPool,
    SessionPool,
)
from pydeno._isolated import _session_options

KEY = b"k" * 32


class _Count:
    """An integer-like that is not an `int` (as numpy integers are): has `__index__`."""

    def __init__(self, n: int) -> None:
        self.n = n

    def __index__(self) -> int:
        return self.n


# -- 5. numeric types that are numbers keep working -----------------------------------------------


@pytest.mark.parametrize("value", [Fraction(3, 2), Decimal("2.5"), 2, 2.5])
def test_real_numbers_are_accepted_as_seconds(value: object) -> None:
    opts = _session_options(request_timeout=value, max_host_wait=value)
    assert opts["_request_timeout"] == float(value)  # type: ignore[arg-type]


def test_integer_likes_are_accepted_as_counts() -> None:
    opts = _session_options(max_host_calls=_Count(5), max_inflight_host_calls=_Count(2))
    assert opts["_max_host_calls"] == 5
    assert opts["_max_inflight"] == 2
    rt = AsyncIsolatedRuntime(max_memory=_Count(256 * 2**20), prewarm=False)
    assert rt._max_memory == 256 * 2**20  # noqa: SLF001


# -- 6. durations past what a timed wait can take are a ValueError ------------------------------------


@pytest.mark.parametrize(
    "value",
    [1e300, 10**400, Decimal("1e400")],
    ids=["1e300", "10**400", "Decimal1e400"],
)
@pytest.mark.parametrize("name", ["request_timeout", "max_host_wait", "timeout_grace"])
def test_huge_durations_are_a_value_error(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        _session_options(**{name: value})


def test_a_huge_per_call_timeout_is_refused_in_the_parent() -> None:
    async def main() -> None:
        with IsolatedRuntime() as rt:
            for bad in (1e300, math.nan):
                with pytest.raises(ValueError):
                    await rt.eval_async("1", timeout=bad)
            assert await rt.eval_async("1 + 1") == 2

    asyncio.run(main())


async def test_a_huge_or_nan_per_call_timeout_is_refused_by_the_async_runtime() -> None:
    async with AsyncIsolatedRuntime() as rt:
        for bad in (1e300, math.nan):
            with pytest.raises(ValueError):
                await rt.eval("1", timeout=bad)
        assert await rt.eval("1 + 1") == 2


# -- 7. TypeError for a wrong type, ValueError for a bad value ------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        lambda: _session_options(max_host_calls=True),
        lambda: _session_options(max_host_calls=1.5),
        lambda: _session_options(request_timeout="3"),
        lambda: _session_options(timeout_grace=None),
        lambda: AgentSandbox({}, max_journal_bytes=True),
        lambda: AgentSandbox({}, max_tool_calls=True),
        lambda: AgentSandbox({}, max_output_bytes=True),
        lambda: AgentSandbox({}, max_result_bytes=1.5),
        lambda: Pydeno(max_tool_threads=True),
        lambda: Pydeno(min_processes=1.5),
        lambda: Pydeno(limits={"max_memory": "1"}),
        lambda: Pydeno(limits={"max_suspensions": True}),
        lambda: Pydeno(limits={"max_feed_duration_secs": "30"}),
        lambda: SandboxPool(size=True),
        lambda: SessionPool(InMemoryJournalStore(), KEY, {}, max_sessions=True),
        lambda: SessionPool(InMemoryJournalStore(), KEY, {}, ttl="60"),
    ],
)
def test_wrong_types_raise_type_error(make) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError):
        make()


@pytest.mark.parametrize(
    "make",
    [
        lambda: _session_options(max_host_calls=-1),
        lambda: _session_options(request_timeout=math.nan),
        lambda: AgentSandbox({}, max_journal_bytes=0),
        lambda: AgentSandbox({}, max_tool_calls=-1),
        lambda: AgentSandbox({}, timeout=math.inf),
        lambda: Pydeno(limits={"max_memory": 0}),
        lambda: Pydeno(limits={"max_feed_duration_secs": math.nan}),
        lambda: SessionPool(InMemoryJournalStore(), KEY, {}, max_sessions=0),
    ],
)
def test_bad_values_raise_value_error(make) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        make()


# -- 2. SessionPool durations and counts ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"ttl": math.nan},
        {"ttl": math.inf},
        {"ttl": -1},
        {"counter_ttl": math.nan},
        {"counter_ttl": 0},
        {"eviction_interval": math.nan},
        {"eviction_interval": 0},
        {"idle_timeout": math.nan},
        {"idle_timeout": -1},
        {"acquire_timeout": math.nan},
        {"acquire_timeout": -1},
        {"acquire_timeout": 1e300},
        {"max_per_owner": 0},
    ],
)
def test_session_pool_refuses_bad_durations_and_counts(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SessionPool(InMemoryJournalStore(), KEY, {}, **kw)


@pytest.mark.parametrize("kw", [{"max_per_owner": True}, {"idle_timeout": "5"}])
def test_session_pool_refuses_wrong_types(kw: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        SessionPool(InMemoryJournalStore(), KEY, {}, **kw)


def test_session_pool_still_takes_its_documented_values() -> None:
    SessionPool(
        InMemoryJournalStore(),
        KEY,
        {},
        ttl=None,
        idle_timeout=None,
        acquire_timeout=0,
        counter_ttl=None,
        max_per_owner=None,
        eviction_interval=0.5,
    )
    SessionPool(InMemoryJournalStore(), KEY, {}, acquire_timeout=None)


async def test_session_pool_get_refuses_a_nan_timeout() -> None:
    async with SessionPool(InMemoryJournalStore(), KEY, {}) as pool:
        with pytest.raises(ValueError):
            await pool.get("owner", "s1", timeout=math.nan)


# -- 9. async pool and async session validation ------------------------------------------------------------


async def test_async_sandbox_pool_checkout_refuses_a_nan_deadline() -> None:
    async with AsyncSandboxPool(size=1) as pool:
        with pytest.raises(ValueError):
            await pool.checkout(request_timeout=math.nan)
        with pytest.raises(ValueError):
            await pool.checkout(max_host_wait=math.inf)


@pytest.mark.parametrize("kw", [{"timeout": math.nan}, {"max_pause": math.inf}])
async def test_async_agent_sandbox_refuses_non_finite_deadlines(
    kw: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        await AsyncAgentSandbox.create({}, **kw)


# -- 1. the console deadline allowance --------------------------------------------------------------------


class TestConsoleAllowance:
    """Console time may pause the hard deadline for at most one deadline in total per command: one
    slow write does not kill a run, and a flood can at most double it."""

    HARD = 1.5

    @staticmethod
    def write(level: str, args: list[object]) -> None:
        time.sleep(0.9)

    def test_sync_two_slow_writes_finish(self) -> None:
        with IsolatedRuntime(
            RuntimeConfig(on_console=self.write), request_timeout=self.HARD
        ) as rt:
            assert rt.eval("console.log('a'); console.log('b'); 42") == 42

    async def test_async_two_slow_writes_finish(self) -> None:
        async with AsyncIsolatedRuntime(
            RuntimeConfig(on_console=self.write), request_timeout=self.HARD
        ) as rt:
            assert await rt.eval("console.log('a'); console.log('b'); 42") == 42

    def test_front_door_two_slow_writes_finish(self) -> None:
        limits = {"max_feed_duration_secs": self.HARD}
        with Pydeno(min_processes=1, limits=limits) as pool, pool.checkout() as session:
            out = session.feed_run(
                "console.log('a'); console.log('b'); 42",
                print_callback=lambda stream, text: time.sleep(0.9),
            )
            assert out == 42

    def test_a_flood_at_most_doubles_the_deadline(self) -> None:
        with IsolatedRuntime(
            RuntimeConfig(on_console=lambda level, args: time.sleep(0.005)),
            request_timeout=self.HARD,
            max_host_wait=60,
        ) as rt:
            t = time.monotonic()
            with pytest.raises(RuntimeTimeout):
                rt.eval("for (;;) console.log('x')")
            assert time.monotonic() - t < 2 * self.HARD + 1.0

    def test_front_door_flood_at_most_doubles_the_feed(self) -> None:
        limits = {"max_feed_duration_secs": self.HARD, "max_host_wait_secs": 60}
        with Pydeno(min_processes=1, limits=limits) as pool, pool.checkout() as session:
            t = time.monotonic()
            with pytest.raises(PydenoTimeoutError):
                session.feed_run(
                    "for (;;) console.log('x')",
                    print_callback=lambda stream, text: time.sleep(0.005),
                )
            assert time.monotonic() - t < 2 * self.HARD + 1.0


async def test_a_slow_async_tool_still_pauses_the_async_runtime_deadline() -> None:
    async def slow() -> int:
        await asyncio.sleep(2.0)
        return 1

    async with AsyncIsolatedRuntime(request_timeout=1.0) as rt:
        await rt.bind_function("slow", slow)
        assert await rt.eval("slow()") == 1


async def test_console_during_an_async_tool_does_not_eat_the_console_allowance() -> (
    None
):
    # While a tool is in flight the deadline is paused anyway; console time then is not charged to
    # the console allowance (it would otherwise be counted twice).
    async def slow() -> int:
        await asyncio.sleep(1.2)
        return 1

    async with AsyncIsolatedRuntime(
        RuntimeConfig(on_console=lambda level, args: time.sleep(0.4)),
        request_timeout=1.0,
    ) as rt:
        await rt.bind_function("slow", slow)
        code = (
            "(async () => { const p = slow(); console.log('a'); console.log('b'); await p;"
            " console.log('c'); return 7 })()"
        )
        assert await rt.eval(code) == 7


# -- 8. console calls are exempt from the in-flight cap ------------------------------------------------------


async def test_console_is_not_refused_by_the_in_flight_cap() -> None:
    seen: list[object] = []

    async def slow() -> int:
        await asyncio.sleep(0.3)
        return 1

    async with AsyncIsolatedRuntime(
        RuntimeConfig(on_console=lambda level, args: seen.append(args)),
        max_inflight_host_calls=1,
    ) as rt:
        await rt.bind_function("slow", slow)
        assert (
            await rt.eval(
                "(async () => { const p = slow(); console.log('during'); return await p })()"
            )
            == 1
        )
    assert seen == [["during"]]


def test_console_is_not_refused_by_the_in_flight_cap_sync() -> None:
    seen: list[object] = []

    async def slow() -> int:
        await asyncio.sleep(0.3)
        return 1

    async def main() -> int:
        with IsolatedRuntime(
            RuntimeConfig(on_console=lambda level, args: seen.append(args)),
            max_inflight_host_calls=1,
        ) as rt:
            rt.bind_function("slow", slow)
            return await rt.eval_async(
                "(async () => { const p = slow(); console.log('during'); return await p })()"
            )

    assert asyncio.run(main()) == 1
    assert seen == [["during"]]


def test_console_still_counts_toward_max_host_calls() -> None:
    with IsolatedRuntime(
        RuntimeConfig(on_console=lambda level, args: None), max_host_calls=3
    ) as rt:
        with pytest.raises(Exception, match="max_host_calls"):
            rt.eval("for (let i = 0; i < 10; i++) console.log(i)")


# -- 3. the front door's default printer is capped per feed -----------------------------------------------------

FLOOD = "const s = 'x'.repeat(65535); for (let i = 0; i < 40; i++) console.log(s); 1"


def test_default_printer_is_capped_at_one_mib_per_feed() -> None:
    out = io.StringIO()
    with Pydeno(min_processes=1) as pool, pool.checkout() as session:
        with contextlib.redirect_stdout(out):
            assert session.feed_run(FLOOD) == 1
            assert session.feed_run("console.log('next feed'); 2") == 2
    text = out.getvalue()
    first, _, second = text.partition("[truncated]")
    assert len(first.encode()) <= 1024 * 1024
    assert text.count("[truncated]") == 1
    assert "next feed" in second  # the cap is per feed


def test_an_explicit_print_callback_gets_everything() -> None:
    got: list[str] = []
    with Pydeno(min_processes=1) as pool, pool.checkout() as session:
        session.feed_run(FLOOD, print_callback=lambda stream, text: got.append(text))
    assert sum(len(t) for t in got) == 40 * 65536


async def test_async_default_printer_is_capped() -> None:
    out = io.StringIO()
    async with AsyncPydeno(min_processes=1) as pool:
        async with pool.checkout() as session:
            with contextlib.redirect_stdout(out):
                assert await session.feed_run(FLOOD) == 1
    text = out.getvalue()
    assert text.count("[truncated]") == 1
    assert len(text.partition("[truncated]")[0].encode()) <= 1024 * 1024
