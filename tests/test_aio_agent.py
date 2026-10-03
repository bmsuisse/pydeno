"""`AsyncAgentSandbox`: `AgentSandbox` on an asyncio loop.

Proved here: the same semantics as the sync class (runs, pauses, tools, budget, errors, journals,
which are interchangeable between the two); cancellation while running and while paused kills
the worker deterministically; the loop is never blocked; and many sessions share one loop
without a thread per session.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import threading
import time

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    Done,
    Failed,
    JavaScriptError,
    JournalError,
    ReplayDivergence,
    RuntimeConfig,
    RuntimeTimeout,
    ToolCall,
    undefined,
)

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def mul(a: int, b: int) -> int:
    await asyncio.sleep(0.01)
    return a * b


TOOLS = {"add": add, "mul": mul}


async def _gone(sb: AsyncAgentSandbox, within: float = 5.0) -> bool:
    """Has the session's worker exited (and been reaped)? Polls, never signals."""
    proc = sb._core.rt._proc  # noqa: SLF001
    deadline = time.monotonic() + within
    while proc.poll() is None:
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


class TestBasics:
    async def test_run_state_and_tools(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            assert (
                await sb.run("const x = await add(1, 2); return x * await mul(3, 4)")
                == 36
            )
            assert await sb.run("return x") == 3
            assert await sb.run("1") is undefined
            assert sb.calls_made == 2
            assert sb.tool_names == ("add", "mul")

    async def test_create_and_close(self) -> None:
        sb = await AsyncAgentSandbox.create(TOOLS, max_tool_calls=3)
        try:
            assert await sb.run("return await add(2, 2)") == 4
            assert sb.calls_remaining == 2
        finally:
            await sb.close()
        assert sb.is_closed()
        assert await _gone(sb)
        await sb.close()  # idempotent

    async def test_not_started(self) -> None:
        sb = AsyncAgentSandbox(TOOLS)
        with pytest.raises(RuntimeError, match="not started"):
            await sb.run("1")
        await sb.close()

    async def test_validation_matches_the_sync_class(self) -> None:
        with pytest.raises(TypeError, match="sets"):
            AsyncAgentSandbox(TOOLS, request_timeout=1)
        with pytest.raises(ValueError, match="RuntimeConfig.timeout"):
            AsyncAgentSandbox(TOOLS, config=RuntimeConfig(timeout=1.0))
        with pytest.raises(ValueError, match="max_tool_calls"):
            AsyncAgentSandbox(TOOLS, max_tool_calls=-1)
        with pytest.raises(ValueError):
            AsyncAgentSandbox({"bad name": add})

    async def test_namespace_and_prompt_helpers(self) -> None:
        async with AsyncAgentSandbox(TOOLS, namespace="tools") as sb:
            assert await sb.run("return await tools.add(1, 1)") == 2
            assert "tools.add" in sb.describe_tools()
            assert "declare namespace tools" in sb.typescript_stubs()

    async def test_errors_like_agent_sandbox(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            with pytest.raises(JavaScriptError):
                await sb.run("throw new Error('boom')")
            step = await sb.start("return )(")
            assert isinstance(step, Failed)
            assert await sb.run("return 5") == 5  # the session survives both

    async def test_tool_errors_are_redacted(self) -> None:
        def broken() -> None:
            raise ValueError("secret detail")

        async with AsyncAgentSandbox({"broken": broken}) as sb:
            got = await sb.run(
                "try { await broken() } catch (e) { return [e.name, e.message] }"
            )
            assert got == ["ValueError", "host function failed"]

    async def test_sync_tools_see_contextvars_and_do_not_block_the_loop(
        self,
    ) -> None:
        var: contextvars.ContextVar[str] = contextvars.ContextVar("v", default="-")
        seen: list[str] = []

        def slow() -> str:
            seen.append(var.get())
            time.sleep(0.4)
            return threading.current_thread().name

        async with AsyncAgentSandbox({"slow": slow}) as sb:
            var.set("request-1")
            lags: list[float] = []

            async def heartbeat() -> None:
                while True:
                    t = time.monotonic()
                    await asyncio.sleep(0.01)
                    lags.append(time.monotonic() - t - 0.01)

            hb = asyncio.create_task(heartbeat())
            name = await sb.run("return await slow()")
            hb.cancel()
            assert seen == ["request-1"]
            assert name != threading.current_thread().name
            assert max(lags) < 0.15, max(lags)

    async def test_budget(self) -> None:
        async with AsyncAgentSandbox(TOOLS, max_tool_calls=1) as sb:
            assert await sb.run("return await add(1, 1)") == 2
            got = await sb.run("try { await add(1, 1) } catch (e) { return e.name }")
            assert got == "ToolBudgetError"


class TestPause:
    async def test_start_resume_value_and_error(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            step = await sb.start("return (await add(1, 2)) + 1")
            assert isinstance(step, ToolCall) and step.args == (1, 2)
            assert sb.pending == step
            assert await sb.resume(step, 41) == Done(42)
            step = await sb.start("try { await add(1) } catch (e) { return e.name }")
            assert await sb.resume(step, error=KeyError("x")) == Done("KeyError")

    async def test_misuse(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            step = await sb.start("return await add(1, 2)")
            with pytest.raises(RuntimeError, match="paused"):
                await sb.start("1")
            with pytest.raises(TypeError, match="exactly one"):
                await sb.resume(step)
            await sb.resume(step, 3)
            with pytest.raises(RuntimeError, match="not the one"):
                await sb.resume(step, 3)

    async def test_concurrent_use_is_refused(self) -> None:
        gate = asyncio.Event()

        async def wait_tool() -> int:
            await gate.wait()
            return 1

        async with AsyncAgentSandbox({"wait_tool": wait_tool}) as sb:
            first = asyncio.create_task(sb.run("return await wait_tool()"))
            await asyncio.sleep(0.2)
            with pytest.raises(RuntimeError, match="busy"):
                await sb.run("1")
            gate.set()
            assert await first == 1

    async def test_a_tool_cannot_drive_its_own_session(self) -> None:
        holder: list[AsyncAgentSandbox] = []

        async def reenter() -> int:
            return await holder[0].run("1")

        async with AsyncAgentSandbox({"reenter": reenter}) as sb:
            holder.append(sb)
            got = await sb.run("try { await reenter() } catch (e) { return e.name }")
            assert got == "RuntimeError"


class TestCancellation:
    async def test_cancel_while_running_kills_the_worker(self) -> None:
        sb = await AsyncAgentSandbox.create(TOOLS)
        task = asyncio.create_task(sb.run("while (true) {}"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sb.is_closed()
        assert await _gone(sb, 2.0)
        with pytest.raises(RuntimeError, match="gone"):
            await sb.run("1")
        with pytest.raises(JournalError, match="gone"):
            await sb.dump(KEY)
        await sb.close()

    async def test_cancel_start_while_running(self) -> None:
        sb = await AsyncAgentSandbox.create(TOOLS)
        task = asyncio.create_task(sb.start("while (true) {}"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sb.is_closed() and await _gone(sb, 2.0)

    async def test_cancel_run_while_paused_at_an_async_tool(self) -> None:
        entered = asyncio.Event()

        async def forever() -> None:
            entered.set()
            await asyncio.Event().wait()

        sb = await AsyncAgentSandbox.create({"forever": forever})
        task = asyncio.create_task(sb.run("return await forever()"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sb.is_closed() and await _gone(sb, 2.0)
        assert not sb._core.rt._tasks  # noqa: SLF001 - no shim task left waiting

    async def test_cancel_the_task_holding_a_paused_session(self) -> None:
        holder: list[AsyncAgentSandbox] = []
        paused = asyncio.Event()

        async def agent() -> None:
            async with AsyncAgentSandbox(TOOLS) as sb:
                holder.append(sb)
                step = await sb.start("return await add(1, 2)")
                assert isinstance(step, ToolCall)
                paused.set()
                await asyncio.sleep(
                    3600
                )  # e.g. waiting for a human to approve the call

        task = asyncio.create_task(agent())
        await paused.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert holder[0].is_closed() and await _gone(holder[0], 2.0)

    async def test_close_while_paused_kills_at_once(self) -> None:
        sb = await AsyncAgentSandbox.create(TOOLS)
        step = await sb.start("return await add(1, 2)")
        assert isinstance(step, ToolCall)
        t = time.monotonic()
        await sb.close()
        assert (
            time.monotonic() - t < 0.9
        )  # no close-frame grace period for a busy worker
        assert await _gone(sb, 2.0)
        assert sb.pending is None

    async def test_hard_timeout_closes_the_session(self) -> None:
        async with AsyncAgentSandbox(TOOLS, timeout=0.5) as sb:
            step = await sb.start("while (true) {}")
            assert isinstance(step, Failed)
            assert isinstance(step.error, RuntimeTimeout)
            assert sb.is_closed()

    async def test_max_pause_closes_a_session_nobody_resumes(self) -> None:
        async with AsyncAgentSandbox(TOOLS, max_pause=0.3) as sb:
            step = await sb.start("return await add(1, 2)")
            await asyncio.sleep(0.8)
            after = await sb.resume(step, 3)
            assert isinstance(after, Failed)
            assert isinstance(after.error, RuntimeTimeout)
            assert sb.is_closed()

    async def test_worker_killed_behind_the_sessions_back(self) -> None:
        import os
        import signal

        async with AsyncAgentSandbox(TOOLS) as sb:
            os.kill(sb.worker_pid, signal.SIGKILL)
            assert await _gone(sb, 2.0)
            assert sb.is_closed()
            with pytest.raises(JournalError):
                await sb.dump(KEY)


class TestJournal:
    async def test_round_trip_while_paused(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            await sb.run("globalThis.n = await add(20, 1)")
            step = await sb.start("return n + await add(0, 0)")
            blob = await sb.dump(KEY, associated_data=b"tenant:1")
        restored = await AsyncAgentSandbox.load(
            blob, KEY, TOOLS, associated_data=b"tenant:1"
        )
        try:
            assert restored.pending is not None
            assert restored.pending.args == step.args
            assert await restored.resume(restored.pending, 21) == Done(42)
            assert restored.calls_made == 2
        finally:
            await restored.close()

    async def test_wrong_associated_data_or_key_is_refused(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            blob = await sb.dump(KEY, associated_data=b"a")
        with pytest.raises(JournalError, match="authentication"):
            await AsyncAgentSandbox.load(blob, KEY, TOOLS, associated_data=b"b")
        with pytest.raises(JournalError, match="authentication"):
            await AsyncAgentSandbox.load(blob, b"x" * 32, TOOLS, associated_data=b"a")

    async def test_journals_are_interchangeable_with_agent_sandbox(self) -> None:
        async with AsyncAgentSandbox(TOOLS) as sb:
            await sb.run("globalThis.v = await add(1, 1)")
            async_blob = await sb.dump(KEY)

        def sync_side() -> bytes:
            with AgentSandbox.load(async_blob, KEY, TOOLS) as s:
                assert s.run("return v") == 2
                return s.dump(KEY)

        sync_blob = await asyncio.to_thread(sync_side)
        restored = await AsyncAgentSandbox.load(sync_blob, KEY, TOOLS)
        try:
            assert await restored.run("return v + 1") == 3
        finally:
            await restored.close()

    async def test_replay_never_calls_tools_and_diverges_on_change(self) -> None:
        calls: list[int] = []

        def counted(x: int) -> int:
            calls.append(x)
            return x

        async with AsyncAgentSandbox({"counted": counted}) as sb:
            await sb.run("globalThis.r = await counted(7)")
            blob = await sb.dump(KEY)
        assert calls == [7]
        restored = await AsyncAgentSandbox.load(blob, KEY, {"counted": counted})
        await restored.close()
        assert calls == [7]
        with pytest.raises(JournalError, match="tools"):
            await AsyncAgentSandbox.load(blob, KEY, {"other": counted})

    async def test_divergence_when_the_environment_differs(self) -> None:
        cfg = RuntimeConfig(bootstrap="globalThis.salt = 1")
        async with AsyncAgentSandbox(TOOLS, config=cfg) as sb:
            await sb.run("return salt")
            blob = await sb.dump(KEY)
        threads = threading.active_count()
        with pytest.raises(ReplayDivergence, match="diverged"):
            await AsyncAgentSandbox.load(
                blob, KEY, TOOLS, config=RuntimeConfig(bootstrap="globalThis.salt = 2")
            )
        assert threading.active_count() <= threads + 1

    async def test_over_cap_journal(self) -> None:
        async with AsyncAgentSandbox(TOOLS, max_journal_bytes=200) as sb:
            await sb.run("return '" + "x" * 300 + "'")
            with pytest.raises(JournalError, match="max_journal_bytes"):
                await sb.dump(KEY)
            assert await sb.run("return 1") == 1  # the session keeps working


class TestScale:
    async def test_no_thread_per_session(self) -> None:
        sessions = [await AsyncAgentSandbox.create(TOOLS) for _ in range(3)]
        await asyncio.gather(*(s.run("return await add(1, 1)") for s in sessions))
        before = threading.active_count()
        more = [await AsyncAgentSandbox.create(TOOLS) for _ in range(20)]
        await asyncio.gather(*(s.run("return await mul(2, 2)") for s in more))
        assert threading.active_count() - before <= 2
        await asyncio.gather(*(s.close() for s in sessions + more))

    async def test_200_concurrent_sessions_in_one_loop(self) -> None:
        n = 200
        threads_before = threading.active_count()
        sessions = await asyncio.gather(
            *(AsyncAgentSandbox.create(TOOLS, max_memory=256 * 2**20) for _ in range(n))
        )
        try:

            async def work(i: int, sb: AsyncAgentSandbox) -> int:
                await sb.run(f"globalThis.i = {i}")
                step = await sb.start("return i + await mul(i, 2)")
                assert isinstance(step, ToolCall)
                await asyncio.sleep(0.01)  # every session paused at once
                done = await sb.resume(step, step.args[0] * step.args[1])
                assert isinstance(done, Done)
                return done.value

            results = await asyncio.gather(
                *(work(i, sb) for i, sb in enumerate(sessions))
            )
            assert results == [3 * i for i in range(n)]
            # The shared pools, not the sessions, own every thread.
            assert threading.active_count() - threads_before <= 8
        finally:
            await asyncio.gather(*(s.close() for s in sessions))
        assert all(s.is_closed() for s in sessions)
        gone = await asyncio.gather(*(_gone(s, 10.0) for s in sessions))
        assert all(gone)

    async def test_dropped_session_is_collected_and_its_worker_killed(self) -> None:
        sb = await AsyncAgentSandbox.create(TOOLS)
        proc = sb._core.rt._proc  # noqa: SLF001
        del sb
        for _ in range(3):
            gc.collect()
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert proc.poll() is not None
