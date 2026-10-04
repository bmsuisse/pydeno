"""`AsyncAgentSandbox.dump()` after the worker died: the journal as of the last good run.

The async port of `test_agent_journal_recovery.py`. The logic is `_SessionBase`, shared with
`AgentSandbox`; these tests prove the async driver feeds it the same way, including the two ways
only an asyncio session can lose its worker: a cancelled run, and a worker that dies behind an idle
or paused session's back. Journals are interchangeable with the sync class.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timezone
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    JavaScriptError,
    ResultTooLarge,
    RuntimeTimeout,
    WorkerCrashed,
)
from pydeno._agent import Done, Failed, JournalError, ToolCall, _open, _seal

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def tools() -> dict[str, Any]:
    return {"add": lambda a, b: a + b, "echo": lambda x: x}


async def session(**kwargs: Any) -> AsyncAgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 7)
    return await AsyncAgentSandbox.create(tools(), **kwargs)


async def load(blob: bytes, **kwargs: Any) -> AsyncAgentSandbox:
    return await AsyncAgentSandbox.load(blob, KEY, tools(), **kwargs)


def journal(blob: bytes) -> dict[str, Any]:
    return json.loads(_open(blob, KEY))


def reseal(blob: bytes, edit: Any) -> bytes:
    data = journal(blob)
    edit(data)
    return _seal(json.dumps(data).encode(), KEY)


async def crash_while_paused(s: AsyncAgentSandbox, code: str) -> Failed:
    step = await s.start(code)
    assert isinstance(step, ToolCall)
    os.kill(s.worker_pid, signal.SIGKILL)
    after = await s.resume(step, 1)
    assert isinstance(after, Failed)
    assert isinstance(after.error, WorkerCrashed)
    assert s.is_closed()
    return after


class TestDumpAfterACrash:
    async def test_the_journal_covers_the_runs_before_the_crash(self) -> None:
        s = await session()
        await s.run("globalThis.a = await add(1, 2)")
        await s.run("let b = a * 10")
        await crash_while_paused(s, "globalThis.a = 'clobbered'; await add(0, 0)")
        blob = await s.dump(KEY)
        await s.close()
        records = journal(blob)["records"]
        assert [r[1] for r in records if r[0] == "run"] == [
            "globalThis.a = await add(1, 2)",
            "let b = a * 10",
        ]
        assert records[-1] == ["lost", 1, "WorkerCrashed"]
        t = await load(blob)
        try:
            assert await t.run("return [a, b]") == [3, 30]
            assert t.lost_runs == 1 and t.pending is None and not t.is_closed()
        finally:
            await t.close()

    async def test_load_replays_and_keeps_going_across_both_classes(self) -> None:
        s = await session()
        await s.run(
            "globalThis.log = []; for (const x of [1, 2, 3]) log.push(await echo(x))"
        )
        await s.run("globalThis.r = Math.random()")
        expected = await s.run("return [log, r]")
        await crash_while_paused(s, "await echo('lost')")
        blob = await s.dump(KEY)
        await s.close()

        def sync_side() -> bytes:
            with AgentSandbox.load(blob, KEY, tools()) as t:
                assert t.run("return [log, r]") == expected
                assert t.lost_runs == 1
                t.run("log.push(await echo(4))")
                return t.dump(KEY)

        again = await asyncio.to_thread(sync_side)
        u = await load(again)
        try:
            assert await u.run("return log") == [1, 2, 3, 4]
            assert u.lost_runs == 1
        finally:
            await u.close()

    async def test_dumping_a_dead_session_is_repeatable(self) -> None:
        s = await session()
        await s.run("globalThis.n = 1")
        await crash_while_paused(s, "await add(1, 1)")
        assert await s.dump(KEY) == await s.dump(KEY)
        await s.close()

    async def test_a_hard_timeout(self) -> None:
        s = await session(timeout=0.5)
        await s.run("globalThis.kept = await add(20, 22)")
        step = await s.start("globalThis.kept = 0; while (true) {}")
        assert isinstance(step, Failed) and isinstance(step.error, RuntimeTimeout)
        blob = await s.dump(KEY)
        await s.close()
        assert journal(blob)["records"][-1] == ["lost", 0, "RuntimeTimeout"]
        t = await load(blob, timeout=5)
        try:
            assert await t.run("return kept") == 42
        finally:
            await t.close()

    async def test_a_session_nobody_resumed(self) -> None:
        s = await session(max_pause=0.3)
        await s.run("globalThis.v = 'before'")
        step = await s.start("return await add(1, 2)")
        await asyncio.sleep(0.8)
        assert isinstance(await s.resume(step, 3), Failed)
        blob = await s.dump(KEY)
        await s.close()
        t = await load(blob)
        try:
            assert await t.run("return v") == "before"
        finally:
            await t.close()


class TestAsyncOnlyWaysToLoseAWorker:
    async def test_a_cancelled_run_is_lost_with_what_it_spent(self) -> None:
        gate = asyncio.Event()

        async def slow(x: int) -> int:
            gate.set()
            await asyncio.sleep(3600)
            return x

        s = await AsyncAgentSandbox.create(
            {"add": lambda a, b: a + b, "slow": slow}, max_tool_calls=5, clock=CLOCK
        )
        await s.run("globalThis.n = await add(1, 1)")
        task = asyncio.create_task(
            s.run("await add(1, 1); globalThis.n = await slow(9)")
        )
        await gate.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        blob = await s.dump(KEY)
        await s.close()
        assert journal(blob)["records"][-1] == ["lost", 2, "WorkerCrashed"]
        t = await AsyncAgentSandbox.load(
            blob, KEY, {"add": lambda a, b: a + b, "slow": slow}
        )
        try:
            assert await t.run("return n") == 2
            assert t.calls_made == 3 and t.calls_remaining == 2  # nothing refunded
        finally:
            await t.close()

    async def test_a_worker_killed_while_idle_loses_nothing(self) -> None:
        s = await session()
        await s.run("globalThis.n = await add(2, 3)")
        os.kill(s.worker_pid, signal.SIGKILL)
        for _ in range(100):
            if s.is_closed():
                break
            await asyncio.sleep(0.02)
        blob = await s.dump(KEY)
        await s.close()
        assert all(r[0] != "lost" for r in journal(blob)["records"])
        t = await load(blob)
        try:
            assert await t.run("return n") == 5 and t.lost_runs == 0
        finally:
            await t.close()


class TestTheBudgetIsNotRefunded:
    async def test_calls_made_by_the_crashed_run_stay_spent(self) -> None:
        s = await session(max_tool_calls=6)
        await s.run("await add(1, 1)")
        step = await s.start("await add(1, 1); await add(1, 1); await add(1, 1)")
        step = await s.resume(step, 2)
        step = await s.resume(step, 2)
        assert isinstance(step, ToolCall)
        os.kill(s.worker_pid, signal.SIGKILL)
        await s.resume(step, 2)
        assert s.calls_made == 4
        blob = await s.dump(KEY)
        await s.close()
        assert journal(blob)["records"][-1] == ["lost", 3, "WorkerCrashed"]
        t = await load(blob)
        try:
            assert t.calls_made == 4 and t.calls_remaining == 2
            await t.run("await add(1, 1); await add(1, 1)")
            with pytest.raises(JavaScriptError, match="ToolBudgetError"):
                await t.run("await add(1, 1)")
        finally:
            await t.close()

    async def test_repeated_crash_and_load_cannot_mint_calls(self) -> None:
        blob = None
        for _ in range(3):
            s = (
                await load(blob)
                if blob is not None
                else await session(max_tool_calls=5)
            )
            await crash_while_paused(s, "await add(1, 1)")
            blob = await s.dump(KEY)
            await s.close()
        assert blob is not None
        t = await load(blob)
        try:
            assert t.calls_made == 3 and t.lost_runs == 3
        finally:
            await t.close()


class TestCompletedFailuresStay:
    async def test_a_javascript_error_and_a_too_large_result_are_completed_runs(
        self,
    ) -> None:
        s = await session(max_result_bytes=20)
        with pytest.raises(JavaScriptError):
            await s.run("globalThis.partial = 1; throw new Error('x')")
        with pytest.raises(ResultTooLarge):
            await s.run("globalThis.big = 'y'.repeat(100); return big")
        await crash_while_paused(s, "await add(1, 1)")
        blob = await s.dump(KEY)
        await s.close()
        t = await load(blob)
        try:
            assert await t.run("return [partial, big.length]") == [1, 100]
        finally:
            await t.close()


class TestUnchangedRules:
    async def _dead_blob(self, **dump: Any) -> bytes:
        s = await session()
        await s.run("globalThis.q = 1")
        await crash_while_paused(s, "await add(1, 1)")
        try:
            return await s.dump(KEY, **dump)
        finally:
            await s.close()

    async def test_tampering_key_and_associated_data(self) -> None:
        blob = await self._dead_blob(associated_data=b"tenant-1")
        forged = blob.replace(b'["lost",1,', b'["lost",0,')
        assert forged != blob
        with pytest.raises(JournalError, match="authentication"):
            await load(forged, associated_data=b"tenant-1")
        with pytest.raises(JournalError, match="authentication"):
            await load(blob, associated_data=b"tenant-2")
        t = await load(blob, associated_data=b"tenant-1")
        try:
            assert await t.run("return q") == 1
        finally:
            await t.close()

    async def test_a_lost_record_inside_a_run_is_refused(self) -> None:
        s = await session()
        step = await s.start("return await add(1, 2)")
        assert isinstance(step, ToolCall)
        blob = await s.dump(KEY)
        await s.close()
        bad = reseal(blob, lambda d: d["records"].append(["lost", 0, "WorkerCrashed"]))
        with pytest.raises(JournalError, match="malformed"):
            await load(bad)

    async def test_the_size_cap_still_applies(self) -> None:
        s = await session(max_journal_bytes=200)
        await s.run("globalThis.x = " + json.dumps("z" * 300))
        await crash_while_paused(s, "await add(1, 1)")
        with pytest.raises(JournalError, match="max_journal_bytes"):
            await s.dump(KEY)
        await s.close()

    async def test_a_live_session_dumps_everything_as_before(self) -> None:
        async with await session() as s:
            await s.run("globalThis.w = 1")
            step = await s.start("return await add(w, 1)")
            assert isinstance(step, ToolCall)
            blob = await s.dump(KEY)
        assert all(r[0] != "lost" for r in journal(blob)["records"])
        t = await load(blob)
        try:
            assert t.pending is not None
            assert await t.resume(t.pending, 2) == Done(2)
        finally:
            await t.close()
