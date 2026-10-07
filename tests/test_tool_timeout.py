"""`tool_timeout=`: an opt-in deadline for one host-function call that runs in the parent (#45).

What is pinned here:

- off by default: no thread is moved, nothing is counted, a slow tool is simply waited for;
- a call that outlasts the deadline fails in the guest with a catchable `TimeoutError` whose text
  is always "host function timed out" (also with `redact_host_errors=False`), and the command goes
  on; async handlers are cancelled, synchronous ones are abandoned on their thread and what they
  return later is discarded;
- time in the tool still counts toward `max_host_wait`;
- more than `MAX_ABANDONED_TOOL_CALLS` abandoned calls still running end the session;
- nothing leaks (tasks, threads, descriptors) once the abandoned calls have ended;
- the same for `AsyncIsolatedRuntime`, `SandboxPool` checkouts, agent sessions (where a timed-out
  call is journaled as a failure and replayed as exactly that failure, without running the tool
  again) and the front door.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import timedelta
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    AsyncPydeno,
    AsyncSandboxPool,
    IsolatedRuntime,
    Pydeno,
    PydenoCrashedError,
    RuntimeConfig,
    RuntimeTimeout,
    SandboxPool,
    ToolBridge,
)
from pydeno._isolated import (
    MAX_ABANDONED_TOOL_CALLS,
    TOOL_TIMEOUT_MESSAGE,
    WorkerCrashed,
)

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"
KEY = b"0123456789abcdef0123456789abcdef"
LIMIT = 0.2
CAUGHT = "e.name + ':' + e.message"
TIMED_OUT = f"TimeoutError:{TOOL_TIMEOUT_MESSAGE}"


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


def _named(prefix: str) -> int:
    return sum(t.name.startswith(prefix) for t in threading.enumerate())


def _settle(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


async def _asettle(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return bool(predicate())


def _guest(call: str) -> str:
    return f"try {{ {call}; 'returned' }} catch (e) {{ {CAUGHT} }}"


def _agent_guest(call: str) -> str:
    return f"try {{ await {call}; return 'returned' }} catch (e) {{ return {CAUGHT} }}"


# ---------------------------------------------------------------------------
# validation and defaults
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("bad", [True, False, "1", [1], object()])
    def test_a_wrong_type_is_a_type_error(self, bad: Any) -> None:
        with pytest.raises(TypeError, match="tool_timeout"):
            IsolatedRuntime(tool_timeout=bad, prewarm=False)

    @pytest.mark.parametrize(
        "bad", [0, 0.0, -1, -0.5, float("nan"), float("inf"), timedelta(0)]
    )
    def test_a_bad_number_is_a_value_error(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="tool_timeout"):
            IsolatedRuntime(tool_timeout=bad, prewarm=False)

    @pytest.mark.parametrize("bad", [True, "1", 0, -3])
    def test_the_async_runtime_refuses_the_same(self, bad: Any) -> None:
        with pytest.raises((TypeError, ValueError), match="tool_timeout"):
            AsyncIsolatedRuntime(tool_timeout=bad)

    def test_numbers_and_timedeltas_are_accepted(self) -> None:
        for value, expected in ((2, 2.0), (0.25, 0.25), (timedelta(seconds=3), 3.0)):
            rt = IsolatedRuntime(tool_timeout=value, prewarm=False)
            try:
                assert rt._tool_timeout == expected  # noqa: SLF001
            finally:
                rt.close()
            art = AsyncIsolatedRuntime(tool_timeout=value)
            assert art._tool_timeout == expected  # noqa: SLF001

    def test_agent_sessions_validate_before_starting_anything(self) -> None:
        for bad in (True, 0, -1, "x"):
            with pytest.raises((TypeError, ValueError), match="tool_timeout"):
                AgentSandbox({"f": lambda: 1}, tool_timeout=bad)
            with pytest.raises((TypeError, ValueError), match="tool_timeout"):
                AsyncAgentSandbox({"f": lambda: 1}, tool_timeout=bad)

    def test_pools_validate_at_construction_and_at_checkout(self) -> None:
        with pytest.raises((TypeError, ValueError), match="tool_timeout"):
            SandboxPool(size=1, tool_timeout=0)
        with SandboxPool(size=1) as pool:
            with pytest.raises((TypeError, ValueError), match="tool_timeout"):
                pool.checkout(tool_timeout=True)

    def test_the_front_door_validates_its_limit(self) -> None:
        for bad in (True, 0, -1, "x"):
            with pytest.raises((TypeError, ValueError), match="tool_timeout_secs"):
                Pydeno(sandbox=MODE, min_processes=1, limits={"tool_timeout_secs": bad})

    def test_it_is_a_session_option_of_the_pools(self) -> None:
        assert "tool_timeout" in SandboxPool.SESSION_OPTIONS
        assert "tool_timeout" in AsyncSandboxPool.SESSION_OPTIONS


class TestOffByDefault:
    def test_nothing_changes_without_it(self) -> None:
        seen: list[int] = []

        def slow() -> str:
            seen.append(threading.get_ident())
            time.sleep(LIMIT * 2)
            return "done"

        with IsolatedRuntime() as rt:
            assert rt._tool_timeout is None  # noqa: SLF001
            rt.bind_function("slow", slow)
            assert rt.eval("slow()") == "done"
        # Still on the thread that drives the command, as before.
        assert seen == [threading.get_ident()]
        assert _named("pydeno-tool") == 0

    async def test_the_async_runtime_waits_for_a_slow_tool_too(self) -> None:
        async with AsyncIsolatedRuntime() as rt:
            assert rt._tool_timeout is None  # noqa: SLF001
            await rt.bind_function("slow", lambda: time.sleep(LIMIT * 2) or "done")
            assert await rt.eval("slow()") == "done"

    def test_a_none_checkout_override_keeps_the_pool_default_off(self) -> None:
        with SandboxPool(size=1) as pool, pool.checkout() as rt:
            assert rt._tool_timeout is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# IsolatedRuntime
# ---------------------------------------------------------------------------


class TestSyncRuntime:
    def test_a_sync_tool_that_outlasts_it_fails_in_the_guest_and_the_command_goes_on(
        self,
    ) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=LIMIT) as rt:
                rt.bind_function("slow", lambda: gate.wait(30))
                rt.bind_function("fast", lambda x: x + 1)
                start = time.monotonic()
                got = rt.eval(
                    "(() => { let a; try { slow() } catch (e) { a = e.name + ':' + e.message }"
                    " return [a, fast(41)] })()"
                )
                assert got == [TIMED_OUT, 42]
                assert time.monotonic() - start < 5
        finally:
            gate.set()

    def test_the_guest_error_is_a_catchable_timeout_error(self) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=LIMIT) as rt:
                rt.bind_function("slow", lambda: gate.wait(30))
                assert rt.eval(_guest("slow()")) == TIMED_OUT
        finally:
            gate.set()

    @pytest.mark.parametrize("redact", [True, False])
    def test_the_message_is_the_same_whatever_redact_host_errors_says(
        self, redact: bool
    ) -> None:
        gate = threading.Event()

        def boom() -> None:
            raise RuntimeError("secret detail")

        try:
            with IsolatedRuntime(tool_timeout=LIMIT, redact_host_errors=redact) as rt:
                rt.bind_function("slow", lambda: gate.wait(30))
                rt.bind_function("boom", boom)
                assert rt.eval(_guest("slow()")) == TIMED_OUT
                # Errors the tool raises itself are still redacted (or not) as before.
                text = rt.eval(_guest("boom()"))
                assert text == (
                    "RuntimeError:host function failed"
                    if redact
                    else "RuntimeError:secret detail"
                )
        finally:
            gate.set()

    def test_calls_within_the_deadline_are_untouched(self) -> None:
        with IsolatedRuntime(tool_timeout=5) as rt:
            rt.bind_function("add", lambda a, b: a + b)
            rt.bind_function("fail", lambda: 1 / 0)
            assert rt.eval("add(40, 2)") == 42
            assert rt.eval(_guest("fail()")).startswith("ZeroDivisionError:")

    def test_the_handler_runs_on_a_thread_of_its_own_and_sees_the_callers_context(
        self,
    ) -> None:
        import contextvars

        var: contextvars.ContextVar[str] = contextvars.ContextVar("v", default="no")
        seen: list[tuple[int, str]] = []

        def tool() -> int:
            seen.append((threading.get_ident(), var.get()))
            return 1

        with IsolatedRuntime(tool_timeout=5) as rt:
            rt.bind_function("tool", tool)
            var.set("yes")
            assert rt.eval("tool()") == 1
        assert seen[0][0] != threading.get_ident()
        assert seen[0][1] == "yes"

    def test_a_bound_object_method_is_covered(self) -> None:
        gate = threading.Event()

        class Db:
            def slow(self) -> bool:
                return gate.wait(30)

            def get(self, k: str) -> str:
                return k.upper()

        db = Db()
        try:
            with IsolatedRuntime(tool_timeout=LIMIT) as rt:
                rt.bind_object("db", {"slow": db.slow, "get": db.get, "name": "x"})
                assert rt.eval(_guest("db.slow()")) == TIMED_OUT
                assert rt.eval("db.get('a')") == "A"
        finally:
            gate.set()

    def test_tool_bridge_tools_are_covered(self) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=LIMIT) as rt:
                ToolBridge(
                    {"slow": lambda: gate.wait(30), "one": lambda: 1}, namespace="tools"
                ).attach(rt)
                assert rt.eval(_guest("tools.slow()")) == TIMED_OUT
                assert rt.eval("tools.one()") == 1
        finally:
            gate.set()

    def test_console_is_not_a_tool_call(self) -> None:
        lines: list[Any] = []

        def on_console(level: str, args: list[Any]) -> None:
            time.sleep(LIMIT * 2)
            lines.append(args)

        with IsolatedRuntime(
            RuntimeConfig(on_console=on_console), tool_timeout=LIMIT
        ) as rt:
            assert rt.eval("console.log('hi'); 7") == 7
        assert lines == [["hi"]]

    def test_an_async_handler_is_cancelled(self) -> None:
        state: dict[str, Any] = {}

        async def slow() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def go() -> Any:
            with IsolatedRuntime(tool_timeout=LIMIT) as rt:
                rt.bind_function("slow", slow)
                first = await rt.eval_async(
                    f"(async () => {{ try {{ await slow() }} catch (e) {{ return {CAUGHT} }} }})()"
                )
                return first, rt._outlasted.count  # noqa: SLF001

        first, outstanding = asyncio.run(go())
        assert first == TIMED_OUT
        assert state == {"cancelled": True}
        assert outstanding == 0

    def test_an_async_handler_that_ignores_cancellation_is_abandoned_then_forgotten(
        self,
    ) -> None:
        async def stubborn() -> str:
            for _ in range(40):
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    continue
            return "late"

        async def go() -> Any:
            with IsolatedRuntime(tool_timeout=LIMIT) as rt:
                rt.bind_function("stubborn", stubborn)
                out = await rt.eval_async(
                    f"(async () => {{ try {{ await stubborn() }} catch (e) {{ return {CAUGHT} }} }})()"
                )
                return out, rt._outlasted.count  # noqa: SLF001

        # Cancellation is ignored, so after the grace period it counts as abandoned.
        out, outstanding = asyncio.run(go())
        assert out == TIMED_OUT
        assert outstanding in (0, 1)

    def test_the_late_result_is_discarded(self) -> None:
        release = threading.Event()
        finished = threading.Event()

        def late() -> str:
            release.wait(30)
            finished.set()
            return "late"

        with IsolatedRuntime(tool_timeout=LIMIT) as rt:
            rt.bind_function("late", late)
            rt.bind_function("other", lambda: "other")
            assert rt.eval(_guest("late()")) == TIMED_OUT
            assert rt._outlasted.count == 1  # noqa: SLF001
            release.set()
            assert finished.wait(5)
            assert _settle(lambda: rt._outlasted.count == 0)  # noqa: SLF001
            # Nothing was sent for the abandoned call: the next call is answered by its own
            # reply, and the session is healthy.
            assert rt.eval("other()") == "other"
            assert rt.eval("1 + 1") == 2

    def test_the_abandoned_thread_goes_away_once_the_handler_ends(self) -> None:
        release = threading.Event()
        with IsolatedRuntime(tool_timeout=LIMIT) as rt:
            rt.bind_function("hang", lambda: release.wait(30))
            before = _named("pydeno-tool")
            for _ in range(3):
                assert rt.eval(_guest("hang()")) == TIMED_OUT
            assert _named("pydeno-tool") == before + 3
            release.set()
            assert _settle(lambda: _named("pydeno-tool") == before)

    def test_time_in_the_tool_counts_toward_max_host_wait(self) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=5, max_host_wait=LIMIT) as rt:
                rt.bind_function("slow", lambda: gate.wait(30))
                with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                    rt.eval("slow()")
        finally:
            gate.set()

    def test_max_host_wait_still_applies_to_timed_out_calls(self) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=LIMIT, max_host_wait=LIMIT * 4) as rt:
                rt.bind_function("slow", lambda: gate.wait(30))
                with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                    rt.eval(
                        "for (let i = 0; i < 100; i++) { try { slow() } catch (e) {} }"
                    )
        finally:
            gate.set()

    def test_max_host_calls_counts_timed_out_calls(self) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=LIMIT, max_host_calls=2) as rt:
                rt.bind_function("slow", lambda: gate.wait(30))
                with pytest.raises(WorkerCrashed, match="max_host_calls=2"):
                    rt.eval(
                        "for (let i = 0; i < 5; i++) { try { slow() } catch (e) {} }"
                    )
        finally:
            gate.set()

    def test_the_cap_on_abandoned_calls_kills_the_session(self) -> None:
        gate = threading.Event()
        try:
            with IsolatedRuntime(tool_timeout=0.05) as rt:
                rt.bind_function("hang", lambda: gate.wait(30))
                n = MAX_ABANDONED_TOOL_CALLS + 4
                with pytest.raises(
                    WorkerCrashed, match="too many abandoned tool calls in this session"
                ):
                    rt.eval(
                        f"for (let i = 0; i < {n}; i++) {{ try {{ hang() }} catch (e) {{}} }}"
                    )
                assert rt.is_closed()
        finally:
            gate.set()

    def test_calls_that_ended_do_not_count_toward_the_cap(self) -> None:
        # Many timeouts over a session's life, each ending before the next: never near the cap.
        release = threading.Event()
        with IsolatedRuntime(tool_timeout=0.05) as rt:
            calls = {"n": 0}

            def brief() -> None:
                calls["n"] += 1
                release.wait(0.15)

            rt.bind_function("brief", brief)
            for _ in range(MAX_ABANDONED_TOOL_CALLS * 2):
                assert rt.eval(_guest("brief()")) == TIMED_OUT
                assert _settle(lambda: rt._outlasted.count == 0)  # noqa: SLF001
            assert rt.eval("1") == 1
            assert calls["n"] == MAX_ABANDONED_TOOL_CALLS * 2

    def test_no_descriptor_is_left_behind(self) -> None:
        release = threading.Event()
        with IsolatedRuntime(tool_timeout=0.05) as rt:
            rt.bind_function("hang", lambda: release.wait(30))
            assert rt.eval(_guest("hang()")) == TIMED_OUT  # warm-up
            before = _fds()
            for _ in range(4):
                assert rt.eval(_guest("hang()")) == TIMED_OUT
            release.set()
            assert _settle(lambda: rt._outlasted.count == 0)  # noqa: SLF001
            assert _fds() == before

    def test_a_pool_checkout_can_set_it(self) -> None:
        gate = threading.Event()
        try:
            with SandboxPool(size=1) as pool:
                with pool.checkout(tool_timeout=LIMIT) as rt:
                    rt.bind_function("slow", lambda: gate.wait(30))
                    assert rt.eval(_guest("slow()")) == TIMED_OUT
                with SandboxPool(size=1, tool_timeout=LIMIT) as pool2:
                    with pool2.checkout() as rt:
                        rt.bind_function("slow", lambda: gate.wait(30))
                        assert rt.eval(_guest("slow()")) == TIMED_OUT
                    with pool2.checkout(tool_timeout=None) as rt:
                        assert rt._tool_timeout is None  # noqa: SLF001
        finally:
            gate.set()


# ---------------------------------------------------------------------------
# AsyncIsolatedRuntime
# ---------------------------------------------------------------------------


class TestAsyncRuntime:
    async def test_a_sync_tool_is_abandoned_and_its_late_result_discarded(self) -> None:
        release = threading.Event()
        finished = threading.Event()

        def late() -> str:
            release.wait(30)
            finished.set()
            return "late"

        async with AsyncIsolatedRuntime(tool_timeout=LIMIT) as rt:
            await rt.bind_function("late", late)
            await rt.bind_function("other", lambda: "other")
            assert await rt.eval(_async_guest("late()")) == TIMED_OUT
            assert rt._outlasted.count == 1  # noqa: SLF001
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            assert await _asettle(lambda: rt._outlasted.count == 0)  # noqa: SLF001
            assert await rt.eval("other()") == "other"
            assert await rt.eval("1 + 1") == 2

    async def test_an_async_tool_is_cancelled(self) -> None:
        state: dict[str, Any] = {}

        async def slow() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async with AsyncIsolatedRuntime(tool_timeout=LIMIT) as rt:
            await rt.bind_function("slow", slow)
            assert await rt.eval(_async_guest("slow()")) == TIMED_OUT
        assert state == {"cancelled": True}

    @pytest.mark.parametrize("redact", [True, False])
    async def test_the_message_ignores_redact_host_errors(self, redact: bool) -> None:
        async def slow() -> None:
            await asyncio.sleep(30)

        async with AsyncIsolatedRuntime(
            tool_timeout=LIMIT, redact_host_errors=redact
        ) as rt:
            await rt.bind_function("slow", slow)
            assert await rt.eval(_async_guest("slow()")) == TIMED_OUT

    async def test_a_bound_object_method(self) -> None:
        async def slow() -> None:
            await asyncio.sleep(30)

        async with AsyncIsolatedRuntime(tool_timeout=LIMIT) as rt:
            await rt.bind_object("db", {"slow": slow, "get": lambda k: k.upper()})
            assert await rt.eval(_async_guest("db.slow()")) == TIMED_OUT
            assert await rt.eval("db.get('a')") == "A"

    async def test_the_cap_on_abandoned_calls_kills_the_session(self) -> None:
        gate = threading.Event()
        try:
            async with AsyncIsolatedRuntime(tool_timeout=0.05) as rt:
                await rt.bind_function("hang", lambda: gate.wait(30))
                n = MAX_ABANDONED_TOOL_CALLS + 4
                with pytest.raises(
                    WorkerCrashed, match="too many abandoned tool calls in this session"
                ):
                    await rt.eval(
                        f"(async () => {{ for (let i = 0; i < {n}; i++) "
                        "{ try { await hang() } catch (e) {} } })()"
                    )
                assert rt.is_closed()
        finally:
            gate.set()

    async def test_the_cap_also_covers_async_tools_that_ignore_cancellation(
        self,
    ) -> None:
        stop = threading.Event()

        async def stubborn() -> None:
            while not stop.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    pass

        import pydeno._isolated as iso

        original = iso._CANCEL_GRACE  # noqa: SLF001
        iso._CANCEL_GRACE = 0.05  # noqa: SLF001
        try:
            async with AsyncIsolatedRuntime(tool_timeout=0.05) as rt:
                await rt.bind_function("stubborn", stubborn)
                n = MAX_ABANDONED_TOOL_CALLS + 4
                with pytest.raises(
                    WorkerCrashed, match="too many abandoned tool calls"
                ):
                    await rt.eval(
                        f"(async () => {{ for (let i = 0; i < {n}; i++) "
                        "{ try { await stubborn() } catch (e) {} } })()"
                    )
        finally:
            iso._CANCEL_GRACE = original  # noqa: SLF001
            stop.set()  # let the stubborn tasks end, so the loop can close
            await asyncio.sleep(0.2)

    async def test_time_in_the_tool_counts_toward_max_host_wait(self) -> None:
        async def slow() -> None:
            await asyncio.sleep(30)

        async with AsyncIsolatedRuntime(tool_timeout=5, max_host_wait=LIMIT) as rt:
            await rt.bind_function("slow", slow)
            with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                await rt.eval("slow()")

    async def test_cancelled_async_tools_leave_no_task_thread_or_descriptor(
        self,
    ) -> None:
        async def slow() -> None:
            await asyncio.sleep(30)

        async with AsyncIsolatedRuntime(tool_timeout=0.05) as rt:
            await rt.bind_function("slow", slow)
            assert await rt.eval(_async_guest("slow()")) == TIMED_OUT  # warm-up
            fds, threads = _fds(), threading.active_count()
            tasks = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
            for _ in range(10):
                assert await rt.eval(_async_guest("slow()")) == TIMED_OUT
            assert await _asettle(
                lambda: (
                    {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
                    <= tasks
                )
            )
            assert rt._outlasted.count == 0  # noqa: SLF001
            assert rt._async_inflight == 0  # noqa: SLF001
            assert _fds() == fds
            assert threading.active_count() == threads

    async def test_abandoned_sync_threads_return_to_the_pool(self) -> None:
        release = threading.Event()
        async with AsyncIsolatedRuntime(tool_timeout=0.05) as rt:
            await rt.bind_function("hang", lambda: release.wait(30))
            for _ in range(3):
                assert await rt.eval(_async_guest("hang()")) == TIMED_OUT
            assert rt._outlasted.count == 3  # noqa: SLF001
            release.set()
            assert await _asettle(lambda: rt._outlasted.count == 0)  # noqa: SLF001

    async def test_a_call_still_queued_for_a_thread_never_runs(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        release = threading.Event()
        ran: list[str] = []
        executor = ThreadPoolExecutor(max_workers=1)

        def hold() -> None:
            release.wait(30)

        def second() -> None:
            ran.append("second")

        try:
            async with AsyncIsolatedRuntime(
                tool_timeout=0.1, handler_executor=executor
            ) as rt:
                await rt.bind_function("hold", hold)
                await rt.bind_function("second", second)
                assert await rt.eval(_async_guest("hold()")) == TIMED_OUT
                # The only thread is busy: this call times out waiting for it, and never starts.
                assert await rt.eval(_async_guest("second()")) == TIMED_OUT
                release.set()
                await asyncio.sleep(0.3)
                assert ran == []
        finally:
            release.set()
            executor.shutdown(wait=True)


def _async_guest(call: str) -> str:
    return (
        f"(async () => {{ try {{ await {call}; return 'returned' }} "
        f"catch (e) {{ return {CAUGHT} }} }})()"
    )


# ---------------------------------------------------------------------------
# agent sessions: journal and replay
# ---------------------------------------------------------------------------

SCRIPT = f"""
const out = [];
try {{ out.push(await slow()) }} catch (e) {{ out.push({CAUGHT}) }}
out.push(await quick(1));
try {{ out.push(await slow()) }} catch (e) {{ out.push({CAUGHT}) }}
out.push(await quick(10));
globalThis.out = out;
return out;
"""
EXPECTED = [TIMED_OUT, 2, TIMED_OUT, 11]


def _never(*_args: Any) -> Any:
    raise AssertionError("a recorded tool call must not run again on replay")


class TestAgentSessions:
    def test_a_timed_out_call_is_a_failure_the_guest_can_catch(self) -> None:
        gate = threading.Event()
        try:
            with AgentSandbox(
                {"slow": lambda: gate.wait(30), "quick": lambda x: x + 1},
                tool_timeout=LIMIT,
            ) as sb:
                assert sb.run(SCRIPT) == EXPECTED
        finally:
            gate.set()

    def test_a_stuck_plain_tool_does_not_hold_up_the_next_one(self) -> None:
        gate = threading.Event()
        try:
            with AgentSandbox(
                {"slow": lambda: gate.wait(30), "quick": lambda x: x + 1},
                tool_timeout=LIMIT,
            ) as sb:
                start = time.monotonic()
                assert sb.run(SCRIPT) == EXPECTED
                # Two timeouts plus two quick calls: nothing waited behind a stuck thread.
                assert time.monotonic() - start < 5
        finally:
            gate.set()

    def test_replay_gives_the_same_failure_and_never_reruns_the_tool(self) -> None:
        gate = threading.Event()
        calls: list[str] = []

        def slow() -> bool:
            calls.append("slow")
            return gate.wait(30)

        def quick(x: int) -> int:
            calls.append("quick")
            return x + 1

        try:
            with AgentSandbox({"slow": slow, "quick": quick}, tool_timeout=LIMIT) as sb:
                assert sb.run(SCRIPT) == EXPECTED
                blob = sb.dump(KEY)
            assert calls == ["slow", "quick", "slow", "quick"]
        finally:
            gate.set()
        before = list(calls)
        for timeout in (LIMIT, None, 30):
            with AgentSandbox.load(
                blob,
                KEY,
                {"slow": _never, "quick": _never},
                tool_timeout=timeout,
            ) as replayed:
                # The replay did not diverge and the guest holds exactly what it saw live.
                assert replayed.run("return globalThis.out") == EXPECTED
        assert calls == before

    def test_the_journal_records_the_failure_like_any_other(self) -> None:
        gate = threading.Event()
        try:
            with AgentSandbox(
                {"slow": lambda: gate.wait(30), "quick": lambda x: x + 1},
                tool_timeout=LIMIT,
            ) as sb:
                sb.run(SCRIPT)
                records = [r for r in sb._records if r[0] == "ans"]  # noqa: SLF001
        finally:
            gate.set()
        failures = [r for r in records if r[1] == "e"]
        assert failures == [["ans", "e", "TimeoutError", TOOL_TIMEOUT_MESSAGE]] * 2

    def test_the_message_is_not_redacted_away_or_leaked_either_way(self) -> None:
        gate = threading.Event()
        try:
            for redact in (True, False):
                with AgentSandbox(
                    {"slow": lambda: gate.wait(30)},
                    tool_timeout=LIMIT,
                    redact_host_errors=redact,
                ) as sb:
                    assert sb.run(_agent_guest("slow()")) == TIMED_OUT
        finally:
            gate.set()

    def test_the_cap_on_abandoned_tools_ends_the_session(self) -> None:
        gate = threading.Event()
        n = MAX_ABANDONED_TOOL_CALLS + 4
        code = f"for (let i = 0; i < {n}; i++) {{ try {{ await hang() }} catch (e) {{}} }} return 1;"
        try:
            with AgentSandbox({"hang": lambda: gate.wait(30)}, tool_timeout=0.05) as sb:
                with pytest.raises(
                    WorkerCrashed, match="too many abandoned tool calls in this session"
                ):
                    sb.run(code)
                assert sb.is_closed()
        finally:
            gate.set()

    def test_async_tools_are_cancelled(self) -> None:
        state: dict[str, bool] = {}

        async def aslow() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        with AgentSandbox({"aslow": aslow}, tool_timeout=LIMIT) as sb:
            assert sb.run(_agent_guest("aslow()")) == TIMED_OUT
        assert state == {"cancelled": True}

    def test_off_by_default_waits_for_the_tool(self) -> None:
        with AgentSandbox({"slow": lambda: time.sleep(LIMIT * 2) or "done"}) as sb:
            assert sb.run("return await slow()") == "done"

    def test_time_in_the_tool_counts_toward_max_pause(self) -> None:
        gate = threading.Event()
        try:
            with AgentSandbox(
                {"slow": lambda: gate.wait(30)}, tool_timeout=5, max_pause=LIMIT
            ) as sb:
                with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                    sb.run("return await slow()")
        finally:
            gate.set()

    def test_calls_you_answer_yourself_are_not_timed(self) -> None:
        with AgentSandbox({"slow": lambda: 1}, tool_timeout=LIMIT) as sb:
            step = sb.start("return await slow()")
            time.sleep(LIMIT * 3)  # the caller takes its time: not a tool running
            step = sb.resume(step, 5)
            assert step.value == 5

    def test_a_pool_checkout_with_it_is_replaced_by_the_sessions_own(self) -> None:
        with SandboxPool(size=1, capture_console=True, random_seed=7) as pool:
            rt = pool.checkout(tool_timeout=3)
            assert rt._tool_timeout == 3  # noqa: SLF001
            with AgentSandbox({"f": lambda: 1}, runtime=rt, tool_timeout=LIMIT):
                assert rt._tool_timeout == LIMIT  # noqa: SLF001


class TestAsyncAgentSessions:
    async def test_a_timed_out_call_is_a_failure_the_guest_can_catch(self) -> None:
        gate = threading.Event()
        try:
            async with AsyncAgentSandbox(
                {"slow": lambda: gate.wait(30), "quick": lambda x: x + 1},
                tool_timeout=LIMIT,
            ) as sb:
                assert await sb.run(SCRIPT) == EXPECTED
        finally:
            gate.set()

    async def test_async_tools_are_cancelled(self) -> None:
        state: dict[str, bool] = {}

        async def aslow() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async with AsyncAgentSandbox({"aslow": aslow}, tool_timeout=LIMIT) as sb:
            assert await sb.run(_agent_guest("aslow()")) == TIMED_OUT
        assert state == {"cancelled": True}

    async def test_replay_gives_the_same_failure_and_never_reruns_the_tool(
        self,
    ) -> None:
        gate = threading.Event()
        calls: list[str] = []

        def slow() -> bool:
            calls.append("slow")
            return gate.wait(30)

        def quick(x: int) -> int:
            calls.append("quick")
            return x + 1

        try:
            async with AsyncAgentSandbox(
                {"slow": slow, "quick": quick}, tool_timeout=LIMIT
            ) as sb:
                assert await sb.run(SCRIPT) == EXPECTED
                blob = await sb.dump(KEY)
        finally:
            gate.set()
        before = list(calls)
        async with await AsyncAgentSandbox.load(
            blob, KEY, {"slow": _never, "quick": _never}, tool_timeout=LIMIT
        ) as replayed:
            assert await replayed.run("return globalThis.out") == EXPECTED
        # A journal from the async class loads into the sync one, with the same result.
        with AgentSandbox.load(
            blob, KEY, {"slow": _never, "quick": _never}, tool_timeout=LIMIT
        ) as replayed_sync:
            assert replayed_sync.run("return globalThis.out") == EXPECTED
        assert calls == before

    async def test_the_cap_on_abandoned_tools_ends_the_session(self) -> None:
        gate = threading.Event()
        n = MAX_ABANDONED_TOOL_CALLS + 4
        code = f"for (let i = 0; i < {n}; i++) {{ try {{ await hang() }} catch (e) {{}} }} return 1;"
        try:
            async with AsyncAgentSandbox(
                {"hang": lambda: gate.wait(30)}, tool_timeout=0.05
            ) as sb:
                with pytest.raises(
                    WorkerCrashed, match="too many abandoned tool calls in this session"
                ):
                    await sb.run(code)
        finally:
            gate.set()

    async def test_a_sync_agent_journal_loads_into_the_async_class(self) -> None:
        gate = threading.Event()
        try:
            with AgentSandbox(
                {"slow": lambda: gate.wait(30), "quick": lambda x: x + 1},
                tool_timeout=LIMIT,
            ) as sb:
                assert sb.run(SCRIPT) == EXPECTED
                blob = sb.dump(KEY)
        finally:
            gate.set()
        async with await AsyncAgentSandbox.load(
            blob, KEY, {"slow": _never, "quick": _never}
        ) as replayed:
            assert await replayed.run("return globalThis.out") == EXPECTED

    async def test_off_by_default_waits_for_the_tool(self) -> None:
        async with AsyncAgentSandbox(
            {"slow": lambda: time.sleep(LIMIT * 2) or "done"}
        ) as sb:
            assert await sb.run("return await slow()") == "done"


# ---------------------------------------------------------------------------
# the front door
# ---------------------------------------------------------------------------

_CATCH = "try { await f() } catch (e) { return [e.name, e.message] }"


class TestFrontDoor:
    def test_feed_run_times_out_a_plain_external(self) -> None:
        gate = threading.Event()
        try:
            with (
                Pydeno(
                    sandbox=MODE,
                    min_processes=1,
                    limits={"tool_timeout_secs": LIMIT},
                ) as pool,
                pool.checkout() as s,
            ):
                assert s.feed_run(_CATCH, external_lookup={"f": gate.wait}) == [
                    "TimeoutError",
                    TOOL_TIMEOUT_MESSAGE,
                ]
                assert s.feed_run("1 + 1") == 2  # the session survives
        finally:
            gate.set()

    def test_resume_auto_times_out_too(self) -> None:
        gate = threading.Event()
        try:
            with (
                Pydeno(
                    sandbox=MODE,
                    min_processes=1,
                    limits={"tool_timeout_secs": LIMIT},
                ) as pool,
                pool.checkout() as s,
            ):
                snap = s.feed_start(_CATCH, external_lookup={"f": gate.wait})
                done = snap.resume_auto()
                assert done.output == ["TimeoutError", TOOL_TIMEOUT_MESSAGE]  # type: ignore[union-attr]
        finally:
            gate.set()

    def test_it_can_be_set_per_session(self) -> None:
        gate = threading.Event()
        try:
            with Pydeno(sandbox=MODE, min_processes=1) as pool:
                with pool.checkout(limits={"tool_timeout_secs": LIMIT}) as s:
                    assert s.feed_run(_CATCH, external_lookup={"f": gate.wait}) == [
                        "TimeoutError",
                        TOOL_TIMEOUT_MESSAGE,
                    ]
                with pool.checkout() as s:  # the default is off
                    assert (
                        s.feed_run(
                            "await f()",
                            external_lookup={"f": lambda: time.sleep(LIMIT) or 3},
                        )
                        == 3
                    )
        finally:
            gate.set()

    def test_the_cap_ends_the_session(self) -> None:
        gate = threading.Event()
        n = MAX_ABANDONED_TOOL_CALLS + 4
        code = f"for (let i = 0; i < {n}; i++) {{ try {{ await f() }} catch (e) {{}} }} return 1;"
        try:
            with (
                Pydeno(
                    sandbox=MODE, min_processes=1, limits={"tool_timeout_secs": 0.05}
                ) as pool,
                pool.checkout() as s,
            ):
                with pytest.raises(PydenoCrashedError, match="abandoned tool calls"):
                    s.feed_run(code, external_lookup={"f": gate.wait})
        finally:
            gate.set()

    async def test_the_async_front_door_times_out_plain_and_async_externals(
        self,
    ) -> None:
        gate = threading.Event()

        async def aslow() -> None:
            await asyncio.sleep(30)

        try:
            async with AsyncPydeno(
                sandbox=MODE, min_processes=1, limits={"tool_timeout_secs": LIMIT}
            ) as pool:
                async with pool.checkout() as s:
                    assert await s.feed_run(
                        _CATCH, external_lookup={"f": gate.wait}
                    ) == ["TimeoutError", TOOL_TIMEOUT_MESSAGE]
                    assert await s.feed_run(_CATCH, external_lookup={"f": aslow}) == [
                        "TimeoutError",
                        TOOL_TIMEOUT_MESSAGE,
                    ]
                    assert await s.feed_run("1 + 1") == 2
        finally:
            gate.set()

    async def test_the_async_front_door_cap_ends_the_session(self) -> None:
        gate = threading.Event()
        n = MAX_ABANDONED_TOOL_CALLS + 4
        code = f"for (let i = 0; i < {n}; i++) {{ try {{ await f() }} catch (e) {{}} }} return 1;"
        try:
            async with AsyncPydeno(
                sandbox=MODE, min_processes=1, limits={"tool_timeout_secs": 0.05}
            ) as pool:
                async with pool.checkout() as s:
                    with pytest.raises(
                        PydenoCrashedError, match="abandoned tool calls"
                    ):
                        await s.feed_run(code, external_lookup={"f": gate.wait})
        finally:
            gate.set()
