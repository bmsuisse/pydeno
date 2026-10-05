"""Async agent calls settle within their run; cancellation ends the worker, not just its waiter."""

from __future__ import annotations

import asyncio

import pytest

from pydeno import AsyncAgentSandbox

pytestmark = [pytest.mark.asyncio, pytest.mark.full_sandbox]


@pytest.mark.parametrize(
    "code, expected",
    [
        ("slow(); return 7", 7),
        (
            "try { await Promise.all([slow(), Promise.reject(new Error('boom'))]); } "
            "catch (e) { return e.message; }",
            "boom",
        ),
        ("return await Promise.race([Promise.resolve(7), slow()])", 7),
    ],
    ids=["unawaited", "rejected-all", "race-loser"],
)
async def test_return_and_rejection_wait_for_outstanding_tool_calls(code, expected):
    entered = asyncio.Event()
    release = asyncio.Event()
    settled = []

    async def slow():
        entered.set()
        try:
            await release.wait()
            settled.append("completed")
            return 42
        except asyncio.CancelledError:
            settled.append("cancelled")
            raise

    async with AsyncAgentSandbox({"slow": slow}, timeout=5, max_pause=5) as session:
        task = asyncio.create_task(session.run(code))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not task.done()
        finally:
            release.set()
        assert await asyncio.wait_for(task, 3) == expected
        assert settled == ["completed"]
        # The prior call's answer cannot arrive during the next run.
        assert await session.run("return 6 * 7") == 42


async def test_queued_async_tool_burst_respects_runtime_cap_and_session_budget():
    seen = []

    async def echo(value):
        seen.append(value)
        await asyncio.sleep(0)
        return value

    async with AsyncAgentSandbox(
        {"echo": echo},
        max_tool_calls=12,
        max_inflight_host_calls=3,
        timeout=5,
        max_pause=5,
    ) as session:
        assert await session.run(
            "return await Promise.all(Array.from({length: 12}, (_, i) => echo(i)))"
        ) == list(range(12))
        assert seen == list(range(12))
        assert (
            await session.run("try { await echo(99); } catch (e) { return e.name; }")
            == "ToolBudgetError"
        )
        assert seen == list(range(12))


async def test_cancelling_queued_tool_burst_runs_finalizer_and_reaps_worker():
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def pending(value):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    async with AsyncAgentSandbox(
        {"pending": pending},
        max_inflight_host_calls=3,
        timeout=5,
        max_pause=5,
    ) as session:
        proc = session._core.rt._proc
        task = asyncio.create_task(
            session.run(
                "return await Promise.all(Array.from({length: 12}, (_, i) => pending(i)))"
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finalized.wait(), 3)

        async def cleaned():
            while proc.poll() is None or session._core.rt._tasks:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(cleaned(), 3)
        assert session.is_closed()
