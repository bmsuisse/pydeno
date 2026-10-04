"""`AsyncPydeno` / `AsyncPydenoSession`: the Monty-shaped front door for asyncio.

The sync contract (see `test_front_door.py`) on the caller's event loop, plus what asyncio adds:
coroutine external functions, plain ones kept off the loop, and cancellation that kills the worker.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from pydeno import (
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    AsyncPydeno,
    AsyncPydenoSnapshot,
    PydenoComplete,
    PydenoCrashedError,
    PydenoError,
    PydenoRuntimeError,
    PydenoSyntaxError,
    PydenoTimeoutError,
    RuntimeConfig,
    classify_error,
)

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"


async def _gone(pid: int, timeout: float = 10.0) -> bool:
    """The worker is dead and reaped (the loop reaps it, so wait without blocking the loop)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_monty_shape_runs_and_keeps_state() -> None:
    async with AsyncPydeno(sandbox=MODE) as pool:
        async with pool.checkout() as session:
            assert await session.feed_run("1 + 1") == 2
            await session.feed_run("const x = 40")
            assert await session.feed_run("x + 2") == 42
            assert session.session_id is None
            assert session.worker_pid is not None


async def test_construction_starts_nothing_and_entering_twice_fails() -> None:
    pool = AsyncPydeno(sandbox=MODE, min_processes=1)
    assert pool.stats()["ready"] == 0
    async with pool:
        session = pool.checkout()
        with pytest.raises(RuntimeError, match="async with pool.checkout"):
            await session.feed_run("1")
        async with session:
            assert await session.feed_run("1") == 1
        with pytest.raises(RuntimeError, match="once"):
            await session.__aenter__()


async def test_coroutine_and_plain_external_functions() -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []

    async def fetch(n: int) -> int:
        await asyncio.sleep(0)
        return n * 10

    def blocking(n: int) -> int:
        threads.append(threading.get_ident())
        time.sleep(0.01)
        return n + 1

    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            result = await session.feed_run(
                "await fetch(2) + await blocking(1)",
                external_lookup={"fetch": fetch, "blocking": blocking},
            )
            assert result == 22
            assert threads and threads[0] != loop_thread  # never on the loop


async def test_inputs_print_and_errors() -> None:
    got: list[tuple[str, str]] = []

    async def boom() -> None:
        raise ValueError("/secret")

    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            assert await session.feed_run("v.a * 2", inputs={"v": {"a": 21}}) == 42
            await session.feed_run(
                "console.log('out'); console.error('err')",
                print_callback=lambda s, t: got.append((s, t)),
            )
            assert got == [("stdout", "out\n"), ("stderr", "err\n")]
            out = await session.feed_run(
                "try { await boom() } catch (e) { return [e.name, e.message] }",
                external_lookup={"boom": boom},
            )
            assert out == ["ValueError", "host function failed"]
            with pytest.raises(PydenoRuntimeError) as info:
                await session.feed_run("null.x")
            assert classify_error(info.value).kind == "js_error"
            with pytest.raises(PydenoSyntaxError):
                await session.feed_run("x y")
            assert await session.feed_run("'alive'") == "alive"


async def test_feed_start_resume_dump_and_load_snapshot() -> None:
    async def double(n: int) -> int:
        return n * 2

    async with AsyncPydeno(sandbox=MODE, min_processes=2) as pool:
        async with pool.checkout() as session:
            snap = await session.feed_start(
                "const a = await double(3)\nconst b = await double(a)\na + b",
                external_lookup={"double": double},
            )
            assert isinstance(snap, AsyncPydenoSnapshot)
            assert (snap.function_name, snap.args) == ("double", (3,))
            snap = await snap.resume({"return_value": 6})
            assert isinstance(snap, AsyncPydenoSnapshot)
            state = await snap.dump()
            done = await snap.resume_auto()
            assert isinstance(done, PydenoComplete) and done.output == 18
            idle = await session.dump()
        async with pool.checkout() as other:
            restored = await other.load_snapshot(
                state, external_lookup={"double": double}
            )
            assert restored.args == (6,)
            done = await restored.resume(value=100)
            assert done.output == 106
            await other.load_session(idle)
            assert await other.feed_run("a + b") == 18


async def test_load_refuses_foreign_state() -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            await session.feed_run("var v = 1")
            state = await session.dump()
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as other:
        async with other.checkout() as session:
            with pytest.raises(PydenoError) as info:
                await session.load_session(state)
            assert classify_error(info.value).kind == "journal_invalid"


async def test_limits_timeout_and_memory() -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout(limits={"max_feed_duration_secs": 0.5}) as session:
            with pytest.raises(PydenoTimeoutError) as info:
                await session.feed_run("for (;;) {}")
            assert isinstance(info.value, TimeoutError)
            with pytest.raises(PydenoCrashedError):
                await session.feed_run("1")
        async with pool.checkout(limits={"max_memory": 200 * 1024 * 1024}) as session:
            with pytest.raises(PydenoCrashedError) as crashed:
                await session.feed_run(
                    "const keep = []; for (;;) keep.push(new Array(1e6).fill(Math.random()))"
                )
            assert classify_error(crashed.value).kind == "memory_limit"
        async with pool.checkout() as session:
            assert await session.feed_run("2") == 2


async def test_cancelling_a_feed_kills_the_worker() -> None:
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(3600)

    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            pid = session.worker_pid
            assert pid is not None
            await session.feed_run("var before = 1")
            task = asyncio.ensure_future(
                session.feed_run("await hang()", external_lookup={"hang": hang})
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _gone(pid)
            with pytest.raises(PydenoCrashedError):
                await session.feed_run("1")
            state = await session.dump()  # as of the last good feed
        async with pool.checkout() as session:
            assert await session.feed_run("3") == 3
            await session.load_session(state)
            assert await session.feed_run("before") == 1


async def test_cancelling_a_running_feed_kills_the_worker() -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            pid = session.worker_pid
            assert pid is not None
            task = asyncio.ensure_future(session.feed_run("for (;;) {}"))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _gone(pid)


async def test_single_use_concurrency_and_exhaustion() -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:

        async def work(i: int) -> tuple[int, int]:
            async with pool.checkout() as session:
                await session.feed_run(f"var mine = {i}")
                await asyncio.sleep(0.02)
                pid = session.worker_pid
                assert pid is not None
                return await session.feed_run("mine"), pid

        results = await asyncio.gather(*(work(i) for i in range(4)))
        assert [r for r, _ in results] == [0, 1, 2, 3]
        pids = [p for _, p in results]
        assert len(set(pids)) == 4
        assert all([await _gone(p) for p in pids])
        assert pool.stats()["cold_starts"] >= 1


async def test_async_agent_sandbox_adopts_a_runtime() -> None:
    rt = AsyncIsolatedRuntime(
        RuntimeConfig(on_console=lambda level, args: None), sandbox=MODE, random_seed=77
    )
    async with AsyncAgentSandbox({"f": lambda: 5}, runtime=rt, timeout=5) as sb:
        assert sb.random_seed == 77
        assert await sb.run("return await f()") == 5
        assert (await sb.execute("console.log('hi'); return 1")).stdout == "hi\n"
    assert rt.is_closed()
    with pytest.raises(TypeError, match="drop"):
        AsyncAgentSandbox({}, runtime=AsyncIsolatedRuntime(sandbox=MODE), max_memory=1)


async def test_preinstalled_workers_freeze_the_clock_at_checkout_and_replay() -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        assert await pool._pool.wait_ready(30)  # noqa: SLF001
        await asyncio.sleep(1.5)  # the worker waits in the pool
        before = int(time.time() * 1000)
        async with pool.checkout() as session:
            assert session._agent._core.rt._pydeno_prepared is not None  # noqa: SLF001
            now = await session.feed_run("Date.now()")
            assert before - 5 <= now <= int(time.time() * 1000) + 5
            assert await session.feed_run("typeof __pydeno_agent_freeze") == "undefined"
            await session.feed_run("var r = Math.random()")
            state = await session.dump()
            expected = await session.feed_run("[Date.now(), r, Math.random()]")
        async with pool.checkout() as other:
            await other.load_session(state)
            assert await other.feed_run("[Date.now(), r, Math.random()]") == expected
