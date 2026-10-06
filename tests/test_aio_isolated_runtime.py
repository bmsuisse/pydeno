"""`AsyncIsolatedRuntime`: the asyncio-native isolated runtime.

Three things are proved here:

* **parity**: it behaves like `IsolatedRuntime` (`eval_async` semantics) from Python's side;
* **the same security properties**, each ported from `test_isolated_runtime.py` /
  `test_isolated_attack_classes.py`, mostly with the same fake (hostile) workers;
* **the reason it exists**: the event loop never stalls, the parent's thread count does not grow
  with the number of runtimes, and nothing leaks across create/close/crash/kill/cancel cycles.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pydeno import (
    JavaScriptError,
    RuntimeConfig,
    RuntimeTimeout,
    WorkerCrashed,
    _sandbox,
    undefined,
)
from pydeno import _aio, _compat
from pydeno._aio import AsyncIsolatedRuntime
from pydeno._isolated import _HARDENING_V8_FLAGS, _MAX_WORKER_THREADS

pytestmark = [
    pytest.mark.full_sandbox,
    # The fake workers below report no sandbox, which is exactly what this warning is for.
    pytest.mark.filterwarnings("ignore:.*degraded OS sandbox:RuntimeWarning"),
]

MIB = 1024 * 1024


async def _rt(**kwargs: Any) -> AsyncIsolatedRuntime:
    cfg = kwargs.pop("config", None) or RuntimeConfig(
        timeout=kwargs.pop("timeout", 5.0)
    )
    return await AsyncIsolatedRuntime.create(cfg, **kwargs)


async def _gone(proc: Any, within: float = 5.0) -> bool:
    """Has the worker exited (and been reaped) within `within` seconds? Polls, never signals."""
    deadline = time.monotonic() + within
    while proc.poll() is None:
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


# ---------------------------------------------------------------------------
# parity with IsolatedRuntime
# ---------------------------------------------------------------------------


class TestParity:
    async def test_eval_and_state(self) -> None:
        async with AsyncIsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
            assert await rt.eval("1 + 2") == 3
            await rt.eval("globalThis.x = 41")
            assert await rt.eval("x + 1") == 42
            assert (
                await rt.eval_async("x") == 41
            )  # alias, for code written for IsolatedRuntime

    async def test_promises_are_awaited(self) -> None:
        async with await _rt() as rt:
            assert await rt.eval("Promise.resolve(7).then(x => x * 6)") == 42

    async def test_javascript_errors_keep_their_type_and_the_runtime_survives(
        self,
    ) -> None:
        async with await _rt() as rt:
            with pytest.raises(JavaScriptError, match="boom"):
                await rt.eval("throw new Error('boom')")
            assert not rt.is_closed()
            assert await rt.eval("2 + 2") == 4

    async def test_a_soft_timeout_raises_and_the_worker_survives(self) -> None:
        async with await _rt(timeout=0.5) as rt:
            with pytest.raises(RuntimeTimeout):
                await rt.eval("while (true) {}")
            assert not rt.is_closed()
            assert await rt.eval("1") == 1

    async def test_a_per_call_timeout(self) -> None:
        async with await _rt(timeout=30.0) as rt:
            with pytest.raises(RuntimeTimeout):
                await rt.eval("while (true) {}", timeout=0.3)
            assert await rt.eval("1") == 1

    async def test_sync_host_functions(self) -> None:
        async with await _rt() as rt:
            await rt.bind_function("add", lambda a, b: a + b)
            assert await rt.eval("add(40, 2)") == 42

    async def test_async_host_functions_run_concurrently(self) -> None:
        async def slow(n: int) -> int:
            await asyncio.sleep(0.4)
            return n * 2

        async with await _rt() as rt:
            await rt.bind_function("slow", slow)
            start = time.monotonic()
            out = await rt.eval("Promise.all([slow(1), slow(2), slow(3)])")
            assert out == [2, 4, 6]
            assert time.monotonic() - start < 1.0

    async def test_a_slow_sync_host_function_does_not_block_the_loop(self) -> None:
        async with await _rt() as rt:
            await rt.bind_function("nap", lambda: time.sleep(0.5) or "rested")
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            task = asyncio.ensure_future(ticker())
            assert await rt.eval("nap()") == "rested"
            task.cancel()
            assert ticks >= 20, "the loop was blocked while a sync handler ran"

    async def test_bind_object(self) -> None:
        async with await _rt() as rt:
            tokens = await rt.bind_object(
                "api", {"double": lambda n: n * 2, "version": "1.0"}
            )
            assert set(tokens) == {"double"}
            assert await rt.eval("api.double(21)") == 42
            assert await rt.eval("api.version") == "1.0"

    async def test_values_cross_both_ways(self) -> None:
        seen: list[object] = []
        async with await _rt() as rt:
            await rt.bind_function("keep", lambda v: seen.append(v) or v)
            assert await rt.eval("keep(2n ** 70n) === 2n ** 70n") is True
            assert seen[-1] == 2**70
            assert await rt.eval("keep(new Uint8Array([1, 2, 3])).length") == 3
            assert seen[-1] == b"\x01\x02\x03"
            assert await rt.eval("keep(new Date(0)).getTime()") == 0
            assert isinstance(seen[-1], datetime)
            assert await rt.eval("keep(new Set([1, 2])).size") == 2
            assert await rt.eval("keep(undefined) === undefined") is True
            assert seen[-1] is undefined

    async def test_large_values_in_both_directions(self) -> None:
        """Frames over 64 KiB are encoded and decoded off the loop; the values must not change."""
        async with await _rt(timeout=20.0) as rt:
            await rt.bind_function("blob", lambda: b"\x07" * (2 * MIB))
            got = await rt.eval("'x'.repeat(3 * 1024 * 1024)")
            assert got == "x" * (3 * MIB)
            assert await rt.eval("blob().length") == 2 * MIB
            code = "const big = '" + "y" * MIB + "'; big.length"
            assert await rt.eval(code) == MIB

    async def test_a_host_function_that_raises_does_not_kill_anything(self) -> None:
        def boom() -> None:
            raise ValueError("tool exploded")

        async with await _rt() as rt:
            await rt.bind_function("boom", boom)
            assert (
                await rt.eval("try { boom(); 'no' } catch (e) { 'caught' }") == "caught"
            )
            assert await rt.eval("1 + 1") == 2

    async def test_revoke_op(self) -> None:
        calls: list[int] = []
        async with await _rt() as rt:
            token = await rt.bind_function("ping", lambda: calls.append(1) or "pong")
            assert await rt.eval("ping()") == "pong"
            assert await rt.revoke_op(token) is True
            with pytest.raises(JavaScriptError):
                await rt.eval("ping()")
        assert calls == [1]

    async def test_modules(self) -> None:
        sources = {
            "custom:a": "import { b } from 'custom:b'; export const a = b + 1;",
            "custom:b": "export const b = 41;",
        }

        async def loader(spec: str) -> str:
            await asyncio.sleep(0.01)
            return sources[spec]

        async with await _rt() as rt:
            await rt.add_static_module("m", "export const answer = 42;")
            assert await rt.eval("import('m').then(ns => ns.answer)") == 42
            assert (await rt.eval_module("static:m"))["answer"] == 42
            await rt.set_module_resolver(lambda spec, ref: spec)
            await rt.set_module_loader(loader)
            assert (await rt.eval_module("custom:a"))["a"] == 42

    async def test_a_sync_loader_that_raises_is_catchable(self) -> None:
        def loader(spec: str) -> str:
            raise ValueError("no such module")

        async with await _rt() as rt:
            await rt.set_module_resolver(lambda spec, ref: spec)
            await rt.set_module_loader(loader)
            with pytest.raises(RuntimeError, match="Failed to load module"):
                await rt.eval_module("custom:missing")
            assert await rt.eval("1 + 1") == 2

    async def test_console_reaches_the_hosts_callback(self) -> None:
        seen: list[tuple[str, list[object]]] = []
        cfg = RuntimeConfig(
            timeout=5.0, on_console=lambda lvl, args: seen.append((lvl, args))
        )
        async with AsyncIsolatedRuntime(cfg) as rt:
            await rt.eval("console.log('a', 1); console.warn('w')")
        assert seen == [("log", ["a", 1]), ("warn", ["w"])]

    async def test_reported_sandbox_and_flags(self) -> None:
        async with await _rt() as rt:
            expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
            if expected is not None:
                assert rt.sandbox == expected
            elif sys.platform == "darwin":
                assert rt.sandbox == "seatbelt"
            elif sys.platform.startswith("linux"):
                assert "seccomp" in rt.sandbox
            assert rt.v8_flags == ["--jitless", *_HARDENING_V8_FLAGS]
            assert set(rt.sandbox_extras) <= {"emptyroot"}
            assert await rt.eval("typeof WebAssembly") == "undefined"

    async def test_sandbox_require_starts_where_the_platform_has_one(self) -> None:
        async with await _rt(sandbox="require") as rt:
            assert rt.sandbox != "none"

    async def test_clock_and_seed(self) -> None:
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with await _rt(clock=when, random_seed=7) as a:
            async with await _rt(clock=when, random_seed=7) as b:
                assert await a.eval("Date.now()") == int(when.timestamp() * 1000)
                assert await a.eval("Math.random()") == await b.eval("Math.random()")

    async def test_non_crossable_result_is_a_type_error(self) -> None:
        async with await _rt() as rt:
            with pytest.raises(TypeError):
                await rt.eval("(() => 1)")
            assert await rt.eval("1") == 1

    async def test_options_are_validated_without_starting_anything(self) -> None:
        from pydeno import InspectorConfig

        with pytest.raises(ValueError, match="inspector"):
            AsyncIsolatedRuntime(RuntimeConfig(inspector=InspectorConfig()))
        with pytest.raises(ValueError, match="sandbox"):
            AsyncIsolatedRuntime(sandbox="sometimes")
        with pytest.raises(ValueError):
            AsyncIsolatedRuntime(max_host_calls=-1)
        with pytest.raises(ValueError):
            AsyncIsolatedRuntime(random_seed=-1)

    async def test_unknown_v8_flags_are_refused(self) -> None:
        with pytest.raises(WorkerCrashed, match="recognise"):
            await _rt(v8_flags=["--definitely-not-a-flag"])

    async def test_a_runtime_must_be_started_and_stays_on_its_loop(self) -> None:
        rt = AsyncIsolatedRuntime()
        with pytest.raises(RuntimeError, match="not started"):
            await rt.eval("1")
        await rt.close()  # closing one that never started is fine
        assert rt.is_closed()

    def test_a_runtime_cannot_be_used_from_another_loop(self) -> None:
        rt = asyncio.run(_rt())
        try:
            with pytest.raises((RuntimeError, WorkerCrashed)):
                asyncio.run(rt.eval("1"))
        finally:
            # Its loop is gone: the supervisor killed the worker when that loop shut down.
            assert rt.is_closed()

    async def test_safe_defaults(self) -> None:
        async with await _rt() as rt:
            assert rt._max_memory == 1024 * MIB  # noqa: SLF001
            assert rt._hard_timeout(None) == 60.0  # noqa: SLF001
            assert rt._hard_timeout(2.0) == 4.0  # noqa: SLF001
            assert rt._redact is True  # noqa: SLF001
        rt = AsyncIsolatedRuntime(max_memory=None, request_timeout=None)
        assert rt._max_memory is None  # noqa: SLF001
        assert rt._hard_timeout(None) is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# contextvars, re-entry, many runtimes
# ---------------------------------------------------------------------------

_REQUEST: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "test_request", default=None
)


class TestContextAndConcurrency:
    async def test_handlers_see_the_callers_contextvars(self) -> None:
        async def who_async() -> str | None:
            return _REQUEST.get()

        async with await _rt() as rt:
            await rt.bind_function("whoSync", lambda: _REQUEST.get())
            await rt.bind_function("whoAsync", who_async)

            async def as_request(name: str) -> list[object]:
                _REQUEST.set(name)
                return [await rt.eval("whoSync()"), await rt.eval("whoAsync()")]

            got = await asyncio.gather(*(as_request(f"req-{i}") for i in range(5)))
        assert got == [[f"req-{i}", f"req-{i}"] for i in range(5)]

    async def test_a_handler_cannot_reenter_its_own_runtime(self) -> None:
        async with await _rt() as rt:

            async def reenter_async() -> object:
                return await rt.eval("1")

            await rt.bind_function("reenterSync", lambda: rt.eval("1"))
            await rt.bind_function("reenterAsync", reenter_async)
            for name in ("reenterSync", "reenterAsync"):
                out = await rt.eval(
                    f"Promise.resolve().then(() => {name}()).then(() => 'no', e => 'blocked')"
                )
                assert out == "blocked", name
            assert await rt.eval("1 + 1") == 2

    async def test_a_handler_may_use_a_different_runtime(self) -> None:
        async with await _rt() as helper, await _rt() as rt:

            async def ask_helper() -> object:
                return await helper.eval("6 * 7")

            await rt.bind_function("askHelper", ask_helper)
            assert await rt.eval("askHelper()") == 42

    async def test_concurrent_evals_on_one_runtime_never_mix_up_answers(self) -> None:
        wrong: list[tuple[int, object]] = []
        async with await _rt(timeout=30.0) as rt:

            async def work(n: int) -> None:
                for i in range(40):
                    value = n * 1000 + i
                    got = await rt.eval(f"{value}")
                    if got != value:
                        wrong.append((value, got))

            await asyncio.gather(*(work(n) for n in range(8)))
        assert wrong == []

    async def test_many_runtimes_in_parallel(self) -> None:
        async def one(n: int) -> list[object]:
            async with await _rt() as rt:
                await rt.bind_function("echo", lambda v: v)
                return [await rt.eval(f"echo({n} * 100 + {i})") for i in range(5)]

        results = await asyncio.gather(*(one(n) for n in range(12)))
        assert results == [[n * 100 + i for i in range(5)] for n in range(12)]


# ---------------------------------------------------------------------------
# containment and limits
# ---------------------------------------------------------------------------


class TestLimits:
    async def test_a_hard_deadline_kills_a_wedged_worker(self) -> None:
        rt = await AsyncIsolatedRuntime.create(RuntimeConfig(), request_timeout=1.0)
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="hard deadline"):
            await rt.eval("while (true) {}")
        assert time.monotonic() - start < 6
        assert rt.is_closed()
        assert await _gone(rt._proc)  # noqa: SLF001
        with pytest.raises(WorkerCrashed, match="closed"):
            await rt.eval("1")
        await rt.close()

    async def test_the_hard_deadline_does_not_charge_host_callbacks(self) -> None:
        async def slow_async() -> str:
            await asyncio.sleep(1.5)
            return "done"

        async with AsyncIsolatedRuntime(RuntimeConfig(), request_timeout=1.0) as rt:
            await rt.bind_function("slowSync", lambda: time.sleep(1.5) or "done")
            await rt.bind_function("slowAsync", slow_async)
            assert await rt.eval("slowSync()") == "done"
            assert await rt.eval("slowAsync()") == "done"

    async def test_max_host_wait_bounds_time_spent_in_callbacks(self) -> None:
        async def forever() -> None:
            await asyncio.sleep(60)

        async with AsyncIsolatedRuntime(
            RuntimeConfig(), request_timeout=1.0, max_host_wait=0.5
        ) as rt:
            await rt.bind_function("forever", forever)
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                await rt.eval("forever()")
            assert time.monotonic() - start < 5
            assert rt.is_closed()

    async def test_max_host_wait_also_ends_a_wedged_sync_handler(self) -> None:
        """IsolatedRuntime's pump is stuck inside a sync handler; here it is not, so the wait cap
        fires while the handler is still running."""
        async with AsyncIsolatedRuntime(
            RuntimeConfig(), request_timeout=1.0, max_host_wait=0.5
        ) as rt:
            await rt.bind_function("nap", lambda: time.sleep(3))
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                await rt.eval("nap()")
            assert time.monotonic() - start < 2.5

    async def test_memory_ceiling_kills_the_worker(self) -> None:
        rt = await AsyncIsolatedRuntime.create(
            RuntimeConfig(max_buffer_bytes=8192 * MIB),
            max_memory=300 * MIB,
            request_timeout=30,
        )
        # 800 MiB: over max_memory, under the kernel's ceiling on Linux (max_memory + 1 GiB)
        with pytest.raises(WorkerCrashed, match="max_memory"):
            await rt.eval("new Uint8Array(800 * 1024 * 1024).fill(1).length")
        assert rt.is_closed()
        await rt.close()

    async def test_the_default_buffer_cap_turns_a_huge_buffer_into_a_range_error(
        self,
    ) -> None:
        async with await _rt(max_memory=400 * MIB) as rt:
            with pytest.raises(JavaScriptError, match="RangeError|Array buffer"):
                await rt.eval("new Uint8Array(2 ** 31).length")
            assert await rt.eval("1") == 1

    async def test_the_worker_exits_by_itself_when_over_budget(self) -> None:
        rt = await AsyncIsolatedRuntime.create(
            RuntimeConfig(max_buffer_bytes=8192 * MIB),
            max_memory=200 * MIB,
            request_timeout=30,
        )
        rt._max_memory = None  # noqa: SLF001 - only the worker's own watchdog remains
        with pytest.raises(WorkerCrashed, match="went over max_memory"):
            await rt.eval("new Uint8Array(900 * 1024 * 1024).fill(1); for (;;) {}")
        await rt.close()

    async def test_a_guest_cannot_loop_on_cheap_host_calls_forever(self) -> None:
        calls: list[int] = []
        rt = await AsyncIsolatedRuntime.create(max_host_calls=50, request_timeout=30)
        await rt.bind_function("tick", lambda: calls.append(1))
        with pytest.raises(WorkerCrashed, match="max_host_calls"):
            await rt.eval("for (;;) tick()")
        assert len(calls) == 50
        assert rt.is_closed()
        await rt.close()

    async def test_max_inflight_host_calls_refuses_the_excess(self) -> None:
        async def slow() -> int:
            await asyncio.sleep(0.3)
            return 1

        async with await _rt(max_inflight_host_calls=3) as rt:
            await rt.bind_function("slow", slow)
            rejected = await rt.eval(
                "Promise.allSettled(Array.from({length: 10}, () => slow()))"
                ".then(r => r.filter(x => x.status === 'rejected').length)"
            )
            assert rejected == 7
            assert (
                await rt.eval("slow()") == 1
            )  # the cap is on concurrency, not a budget

    async def test_redact_host_errors_is_on_by_default(self) -> None:
        def leaky() -> None:
            raise ValueError("/srv/secrets/db.conf: password=hunter2")

        probe = "try { leaky() } catch (e) { e.name + ': ' + e.message }"
        async with await _rt() as rt:
            await rt.bind_function("leaky", leaky)
            assert await rt.eval(probe) == "ValueError: host function failed"
        async with await _rt(redact_host_errors=False) as rt:
            await rt.bind_function("leaky", leaky)
            assert "hunter2" in await rt.eval(probe)

    async def test_worker_death_is_reported_not_hung(self) -> None:
        rt = await AsyncIsolatedRuntime.create(RuntimeConfig(), request_timeout=20)
        os.kill(rt._proc.pid, 9)  # noqa: SLF001 - a worker this test started
        with pytest.raises(WorkerCrashed, match="SIGKILL|died|gone"):
            await rt.eval("1")
        await rt.close()

    async def test_limits_that_cannot_be_measured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_check_limits_can_be_enforced`: warn under "auto", refuse under "require"."""
        monkeypatch.setattr(
            _aio, "_sample_many", lambda pids: [(None, None, None) for _ in pids]
        )
        with pytest.raises(WorkerCrashed, match="cannot be enforced"):
            await _rt(sandbox="require")
        with pytest.warns(RuntimeWarning, match="cannot be enforced"):
            rt = await _rt(sandbox="auto")
        await rt.close()


# ---------------------------------------------------------------------------
# cancellation and close
# ---------------------------------------------------------------------------


class TestCancellationAndClose:
    async def test_cancelling_a_running_eval_kills_the_worker(self) -> None:
        rt = await AsyncIsolatedRuntime.create(RuntimeConfig(), request_timeout=20)
        task = asyncio.ensure_future(rt.eval("new Promise(() => {})"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rt.is_closed()
        assert await _gone(rt._proc)  # noqa: SLF001
        await rt.close()

    async def test_asyncio_timeout_around_eval_is_a_cancellation(self) -> None:
        rt = await _rt(timeout=30.0)
        with pytest.raises(TimeoutError):
            async with _compat.timeout(0.3):
                await rt.eval("while (true) {}")
        assert rt.is_closed()
        await rt.close()

    async def test_cancelling_a_command_still_queued_leaves_the_runtime_alive(
        self,
    ) -> None:
        async with await _rt() as rt:
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold() -> int:
                entered.set()
                await release.wait()
                return 1

            await rt.bind_function("hold", hold)
            first = asyncio.ensure_future(rt.eval("hold()"))
            try:
                # Keep the first command active without racing a machine-dependent CPU loop
                # against the runtime timeout. The second task must be waiting for the slot.
                await asyncio.wait_for(entered.wait(), 5)
                queued = asyncio.ensure_future(rt.eval("2"))
                await asyncio.sleep(0)
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
            finally:
                release.set()
            assert await first == 1
            assert not rt.is_closed()
            assert await rt.eval("3") == 3

    async def test_cancelling_create_does_not_leak_a_worker(self) -> None:
        # Counts, not sets: `_child_pids` itself runs `ps` (a child) on macOS.
        before = len(_child_pids() - _spares())
        for delay in (0.0, 0.005, 0.02, 0.06):
            task = asyncio.ensure_future(_rt(prewarm=False))
            await asyncio.sleep(delay)
            task.cancel()
            try:
                await (await task).close()  # it finished before the cancel landed
            except (asyncio.CancelledError, WorkerCrashed):
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(_child_pids() - _spares()) > before:
            await asyncio.sleep(0.05)
        assert len(_child_pids() - _spares()) <= before

    async def test_close_racing_an_eval_ends_it_cleanly(self) -> None:
        for _ in range(3):
            rt = await AsyncIsolatedRuntime.create(request_timeout=20)
            task = asyncio.ensure_future(rt.eval("while (true) {}"))
            await asyncio.sleep(0.2)
            start = time.monotonic()
            await rt.close()
            assert time.monotonic() - start < 3, "close() must kill after a second"
            with pytest.raises(WorkerCrashed):
                await task
            assert await _gone(rt._proc)  # noqa: SLF001

    async def test_close_is_idempotent_and_reaps(self) -> None:
        rt = await _rt()
        proc = rt._proc  # noqa: SLF001
        await rt.close()
        await rt.close()
        assert proc.poll() is not None

    async def test_a_host_function_that_closes_the_runtime_ends_cleanly(self) -> None:
        rt = await _rt(request_timeout=20)

        async def sabotage() -> int:
            await rt.close()
            return 1

        await rt.bind_function("sabotage", sabotage)
        with pytest.raises(WorkerCrashed):
            await rt.eval("sabotage()")
        assert rt.is_closed()

    async def test_a_dead_runtime_answers_with_an_error_every_time(self) -> None:
        rt = await _rt()
        await rt.close()
        start = time.monotonic()
        for _ in range(20):
            with pytest.raises(WorkerCrashed):
                await rt.eval("1")
        assert time.monotonic() - start < 1


# ---------------------------------------------------------------------------
# lifecycle: finalizer, loop shutdown, atexit, fork
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_a_runtime_dropped_without_close_is_killed_and_reaped(self) -> None:
        rt = await _rt()
        assert await rt.eval("1") == 1
        proc = rt._proc  # noqa: SLF001
        del rt
        gc.collect()
        assert await _gone(proc)

    async def test_the_supervisor_keeps_no_runtime_alive(self) -> None:
        """Regression: the supervisor's frame once held the last runtime it iterated over, so a
        runtime dropped without close() could survive (worker and all) indefinitely."""
        runtimes = [await _rt() for _ in range(3)]
        await asyncio.sleep(0.4)  # several supervisor ticks over all three
        procs = [rt._proc for rt in runtimes]  # noqa: SLF001
        finalizers = [rt._finalizer for rt in runtimes]  # noqa: SLF001
        del runtimes
        await asyncio.sleep(
            0.3
        )  # no gc.collect(): reference counting alone must free them
        assert not any(f.alive for f in finalizers)
        for proc in procs:
            assert await _gone(proc)

    def test_a_loop_that_shuts_down_takes_its_workers_with_it(self) -> None:
        async def leak() -> Any:
            rt = await _rt()
            await rt.eval("1")
            return rt  # never closed

        rt = asyncio.run(leak())
        assert rt.is_closed()
        assert rt._proc.poll() is not None  # noqa: SLF001

    def test_interpreter_exit_kills_workers_of_a_loop_that_never_shut_down(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "leaky.py"
        script.write_text(
            textwrap.dedent(
                """
                import asyncio
                from pydeno._aio import AsyncIsolatedRuntime
                loop = asyncio.new_event_loop()
                rt = loop.run_until_complete(AsyncIsolatedRuntime.create(prewarm=False))
                print(rt._proc.pid, flush=True)
                # exits with the loop still open and the runtime never closed
                """
            )
        )
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stderr
        pid = int(done.stdout.split()[0])
        deadline = time.monotonic() + 5
        while _sandbox.rss_bytes(pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _sandbox.rss_bytes(pid) is None, "the worker outlived its parent"

    async def test_a_forked_child_leaves_the_parents_workers_alone(self) -> None:
        async with await _rt() as rt:
            assert await rt.eval("1") == 1
            proc = rt._proc  # noqa: SLF001
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads
                pid = os.fork()
            if (
                pid == 0
            ):  # the child: must not touch, signal or talk to the parent's worker
                code = 1
                try:
                    if _aio._LIVE or rt._finalizer.alive:  # noqa: SLF001
                        os._exit(2)
                    asyncio.run(rt.close())  # only drops the inherited descriptors
                    try:
                        asyncio.run(rt.eval("1"))
                    except RuntimeError:
                        code = 0  # refused, as it must be
                    _aio._kill_all_at_exit()  # noqa: SLF001 - what atexit would do
                    gc.collect()
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            assert proc.poll() is None, "the child killed the parent's worker"
            assert await rt.eval("2") == 2


# ---------------------------------------------------------------------------
# the point: no loop stalls, no thread per runtime, no leaks
# ---------------------------------------------------------------------------


def _child_pids() -> set[int]:
    me = os.getpid()
    if sys.platform.startswith("linux"):
        out = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    rest = fh.read().rsplit(b")", 1)[1].split()
            except OSError:
                continue
            if int(rest[1]) == me:
                out.add(int(entry))
        return out
    done = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True
    )
    return {
        int(a)
        for a, b in (ln.split() for ln in done.stdout.splitlines())
        if int(b) == me
    }


def _spares() -> set[int]:
    from pydeno import _isolated

    spare = _isolated._SPARE  # noqa: SLF001
    return {spare[0].pid} if spare is not None else set()


def _leak_report() -> dict[str, object]:
    from pydeno import _isolated

    return {
        "children": sorted(_child_pids()),
        "spare": _spares(),
        "live": [rt._proc.pid for rt in list(_aio._LIVE) if rt._proc is not None],  # noqa: SLF001
        "zombies": [p.pid for p in _aio._ZOMBIES],  # noqa: SLF001
        "iso_live": [rt._proc.pid for rt in list(_isolated._LIVE)],  # noqa: SLF001
        "refilling": _aio._REFILLING,  # noqa: SLF001
    }


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


async def _settle() -> None:
    gc.collect()
    await asyncio.sleep(0.4)  # supervisor ticks reap; a spare finishes starting
    gc.collect()


class _Heartbeat:
    """A 1 ms heartbeat on the running loop: how late each beat woke up."""

    def __init__(self) -> None:
        self.lags: list[float] = []
        self._stop = False
        self._task: asyncio.Future[None] | None = None

    async def __aenter__(self) -> _Heartbeat:
        self._task = asyncio.ensure_future(self._run())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop = True
        assert self._task is not None
        await self._task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop:
            due = loop.time() + 0.001
            await asyncio.sleep(0.001)
            self.lags.append(loop.time() - due)

    def p99(self) -> float:
        ordered = sorted(self.lags)
        return ordered[int(len(ordered) * 0.99)] if ordered else 0.0

    def worst(self) -> float:
        return max(self.lags, default=0.0)


_BURN = (
    "import time\nt = time.process_time()\nwhile time.process_time() - t < 0.06: pass"
)


async def _cpu_burst(n: int) -> None:
    """No pydeno: `n` plain child processes, each burning about as much CPU as a worker start-up,
    started from a thread pool (as worker start-ups are) and polled from the loop."""
    loop = asyncio.get_running_loop()

    def spawn() -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, "-S", "-c", _BURN], stdin=subprocess.DEVNULL
        )

    procs = await asyncio.gather(*(loop.run_in_executor(None, spawn) for _ in range(n)))
    while any(p.poll() is None for p in procs):
        await asyncio.sleep(0.005)


class TestScaling:
    async def test_the_loop_never_stalls(self) -> None:
        """A 1 ms heartbeat stays on time while 16 runtimes are created, driven (with a host call
        per evaluation) and closed: 99th percentile under 100 ms, nothing later than a second.

        Those bounds are absolute on a machine that has CPU to spare. On one that does not (a
        small CI runner, a debug build, a loaded host), any burst of process start-ups delays the
        loop, pydeno or not: measured on Linux containers with a 2-CPU quota, 16 plain processes
        burning 60 ms of CPU each cost the loop as much as 16 worker start-ups do (#83). So the
        same heartbeat first measures such a burst, without pydeno, and a bound only grows past
        its absolute value when that control itself was at least half as late."""
        async with _Heartbeat() as control:
            await _cpu_burst(16)

        async with _Heartbeat() as beat:
            runtimes = await asyncio.gather(*(_rt(timeout=30.0) for _ in range(16)))
            await asyncio.gather(
                *(rt.bind_function("echo", lambda v: v) for rt in runtimes)
            )

            async def drive(rt: AsyncIsolatedRuntime) -> None:
                for i in range(30):
                    assert await rt.eval(f"echo({i}) + 1") == i + 1

            await asyncio.gather(*(drive(rt) for rt in runtimes))
            await asyncio.gather(*(rt.close() for rt in runtimes))

        measured = (
            f"pydeno: p99 {beat.p99() * 1000:.1f} ms, worst {beat.worst() * 1000:.1f} ms; "
            f"control burst: p99 {control.p99() * 1000:.1f} ms, "
            f"worst {control.worst() * 1000:.1f} ms"
        )
        assert beat.p99() < max(0.1, 2 * control.p99()), measured
        assert beat.worst() < max(1.0, 2 * control.worst()), measured

    async def test_nothing_blocking_runs_on_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deterministic half of the test above (#83): during bursts of creations, host
        calls, closes, timeouts, crashes and pool checkouts, the blocking operations (process
        spawn, blocking waits, resource reads, stderr reads, sleeps) all run off the loop thread.
        A regression that moved one onto the loop would stall it on every machine, however fast."""
        from pydeno import AsyncSandboxPool

        loop_thread = threading.get_ident()
        on_loop: list[str] = []
        off_loop: list[str] = []

        def guard(owner: Any, name: str) -> None:
            original = getattr(owner, name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                where = on_loop if threading.get_ident() == loop_thread else off_loop
                where.append(name)
                return original(*args, **kwargs)

            monkeypatch.setattr(owner, name, wrapper)

        guard(subprocess.Popen, "__init__")
        guard(subprocess.Popen, "wait")
        guard(subprocess.Popen, "communicate")
        guard(_sandbox, "usage")
        guard(_aio, "_stderr_tail")
        guard(time, "sleep")

        async def lifecycle(i: int) -> None:
            rt = await _rt(timeout=0.3 if i % 4 == 1 else 30.0)
            try:
                await rt.bind_function("echo", lambda v: v)
                assert await rt.eval("echo(1) + 1") == 2
                if i % 4 == 1:  # timed out: the worker is killed
                    with pytest.raises(RuntimeTimeout):
                        await rt.eval("while (true) {}")
                elif i % 4 == 2:  # crashed under it
                    os.kill(rt._proc.pid, 9)  # noqa: SLF001 - our own worker
                    with pytest.raises(WorkerCrashed):
                        await rt.eval("1")
            finally:
                await rt.close()

        await asyncio.gather(*(lifecycle(i) for i in range(12)))
        async with AsyncSandboxPool(RuntimeConfig(timeout=5.0), size=2) as pool:

            async def checkout() -> None:
                async with pool.checkout() as rt:
                    assert await rt.eval("2 * 21") == 42

            await asyncio.gather(*(checkout() for _ in range(6)))  # mostly cold starts
        await _settle()

        assert not on_loop, f"blocking calls on the event loop thread: {on_loop}"
        # Not vacuous: the guarded operations did happen, on other threads.
        assert off_loop.count("__init__") >= 12, off_loop
        assert "usage" in off_loop, off_loop

    async def test_fifty_runtimes_add_almost_no_threads(self) -> None:
        async with await _rt() as warm:  # the shared pools exist from here on
            await warm.eval("1")
        await _settle()
        baseline = threading.active_count()
        runtimes = await asyncio.gather(*(_rt() for _ in range(50)))
        try:
            await asyncio.gather(*(rt.eval("1") for rt in runtimes))
            added = threading.active_count() - baseline
            assert added < 5, f"{added} threads for 50 runtimes"
        finally:
            await asyncio.gather(*(rt.close() for rt in runtimes))

    async def test_nothing_leaks_across_100_lifecycles(self) -> None:
        async with await _rt() as warm:
            await warm.bind_function("f", lambda: 1)
            await warm.eval("f()")
        await _settle()
        before = {
            "children": len(_child_pids() - _spares()),
            "fds": _fds(),
            "threads": threading.active_count(),
        }

        async def closed() -> None:
            async with await _rt() as rt:
                await rt.bind_function("f", lambda: 1)
                assert await rt.eval("f() + 1") == 2

        async def crashed() -> None:
            rt = await _rt(request_timeout=0.3)
            with pytest.raises(RuntimeTimeout):
                await rt.eval("while (true) {}")
            await rt.close()

        async def killed() -> None:
            rt = await _rt()
            os.kill(rt._proc.pid, 9)  # noqa: SLF001 - our own worker
            with pytest.raises(WorkerCrashed):
                await rt.eval("1")
            await rt.close()

        async def cancelled() -> None:
            rt = await _rt()
            task = asyncio.ensure_future(rt.eval("new Promise(() => {})"))
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await rt.close()

        async def dropped() -> None:
            rt = await _rt()
            await rt.eval("1")
            del rt

        for kind in (closed, crashed, killed, cancelled, dropped):
            await asyncio.gather(*(kind() for _ in range(20)))
        await _settle()
        deadline = time.monotonic() + 5
        while (
            len(_child_pids() - _spares()) > before["children"]
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.1)
        after = {
            "children": len(_child_pids() - _spares()),
            "fds": _fds(),
            "threads": threading.active_count(),
        }
        assert after["children"] <= before["children"], (before, after, _leak_report())
        assert after["fds"] <= before["fds"] + 3, (before, after)
        assert after["threads"] <= before["threads"] + 2, (before, after)

    async def test_binding_and_revoking_leaves_no_registry_behind(self) -> None:
        async with await _rt() as rt:
            for i in range(200):
                token = await rt.bind_function(f"f{i % 5}", lambda: 1)
                await rt.revoke_op(token)
            assert rt._handlers == {}  # noqa: SLF001
            assert rt._token_to_hid == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# the worker is untrusted (fake workers, as in test_isolated_runtime.py)
# ---------------------------------------------------------------------------

_FAKE = textwrap.dedent(
    """
    import json, os, struct, sys, threading, time
    def read():
        h = sys.stdin.buffer.read(4)
        if len(h) < 4:
            sys.exit(0)
        return json.loads(sys.stdin.buffer.read(struct.unpack("<I", h)[0]))
    def send(m):
        b = json.dumps(m).encode()
        sys.stdout.buffer.write(struct.pack("<I", len(b)) + b)
        sys.stdout.buffer.flush()
    def raw(b):
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()
    def reply_to(cid):
        while True:
            m = read()
            if m.get("t") == "reply" and m.get("cid") == cid:
                return m
    def spin():
        while True:
            pass
    def nap():
        time.sleep(60)
    MODE = __MODE__
    read()
    send({"t": "ready", "version": 1, "sandbox": "none"})
    if MODE == "noread":
        time.sleep(60)
    elif MODE == "idle_spin":
        spin()
    elif MODE == "idle_threads":
        for _ in range(80):
            threading.Thread(target=nap, daemon=True).start()
        time.sleep(60)
    cmd = read()
    if MODE == "oversize":
        raw(struct.pack("<I", 2**31)); time.sleep(30)
    elif MODE == "badjson":
        raw(struct.pack("<I", 9) + b"{not json"); time.sleep(30)
    elif MODE == "unknown_call":
        send({"t": "call", "cid": 1, "hid": 999, "args": []}); time.sleep(30)
    elif MODE == "bad_tag":
        send({"t": "result", "id": cmd["id"], "v": {"$": "zzz", "v": 1}}); time.sleep(30)
    elif MODE == "wrong_id":
        send({"t": "result", "id": 12345, "v": 1}); time.sleep(30)
    elif MODE == "deep":
        v = 0
        for _ in range(500):
            v = [v]
        send({"t": "result", "id": cmd["id"], "v": v}); time.sleep(30)
    elif MODE == "env":
        send({"t": "result", "id": cmd["id"], "v": sorted(os.environ)}); time.sleep(30)
    elif MODE == "drip":
        raw(struct.pack("<I", 1 << 20))
        while True:
            raw(b" "); time.sleep(0.02)
    elif MODE == "bigerr":
        send({"t": "error", "id": cmd["id"], "kind": "RuntimeError", "msg": "x" * (5 << 20)})
        time.sleep(30)
    elif MODE == "unknown_kind":
        send({"t": "error", "id": cmd["id"], "kind": "SystemExit", "msg": "nope"}); time.sleep(30)
    elif MODE == "call_flood":
        for i in range(1, 100000):
            send({"t": "call", "cid": i, "hid": 1, "args": []})
    elif MODE == "alloc_during_call":
        send({"t": "call", "cid": 1, "hid": 1, "args": []})
        big = bytearray(b"x" * (300 << 20)); copy = bytes(big)
        time.sleep(30)
    elif MODE == "dup_result":
        send({"t": "result", "id": cmd["id"], "v": 1})
        send({"t": "result", "id": cmd["id"], "v": 2}); time.sleep(30)
    elif MODE == "escape_error":
        send({"t": "error", "id": cmd["id"], "kind": "RuntimeError",
              "msg": "boom\\x1b[2J\\x1b]0;pwned\\x07\\nat line 2"})
        time.sleep(30)
    elif MODE == "dup_token":
        while True:
            send({"t": "result", "id": cmd["id"], "v": 7})
            cmd = read()
    elif MODE == "wrong_object_keys":
        while True:
            send({"t": "result", "id": cmd["id"], "v": {"unrelated": 7}})
            cmd = read()
    elif MODE == "revoked_call":
        send({"t": "result", "id": cmd["id"], "v": 7})        # bind_function -> token 7
        cmd = read()
        send({"t": "result", "id": cmd["id"], "v": True})     # revoke
        cmd = read()                                          # eval: a call already in flight
        send({"t": "call", "cid": 1, "hid": 1, "args": []})
        r = reply_to(1)
        send({"t": "result", "id": cmd["id"], "v": [r.get("etype"), r.get("err")]})
        time.sleep(30)
    elif MODE == "inflight":
        for cid in (1, 2, 3):                                 # three calls left running
            send({"t": "call", "cid": cid, "hid": 1, "args": []})
        send({"t": "result", "id": cmd["id"], "v": 1})
        cmd = read()
        send({"t": "call", "cid": 4, "hid": 1, "args": []})   # a fourth, in the next command
        r = reply_to(4)
        send({"t": "result", "id": cmd["id"], "v": r.get("err")})
        time.sleep(30)
    elif MODE == "bad_specifier":
        send({"t": "result", "id": cmd["id"], "v": None})     # set_module_resolver
        cmd = read()
        send({"t": "call", "cid": 1, "hid": 1, "args": [1, "../../etc/passwd\\u0000"]})
        r = reply_to(1)
        send({"t": "result", "id": cmd["id"], "v": r.get("etype")})
        time.sleep(30)
    elif MODE == "bad_console":
        send({"t": "call", "cid": 1, "hid": 1, "args": ["__init__", []]})
        r = reply_to(1)
        send({"t": "result", "id": cmd["id"], "v": r.get("etype")})
        time.sleep(30)
    elif MODE == "native_crash":
        sys.stderr.write("fatal at 0x00007fff5fbff000 libv8.dylib + 1234\\n"); sys.stderr.flush()
        os._exit(1)
    elif MODE == "escape_crash":
        sys.stderr.write("fatal \\x1b[2Jboom\\x07\\n"); sys.stderr.flush()
        os._exit(1)
    elif MODE == "threads":
        for _ in range(80):
            threading.Thread(target=nap, daemon=True).start()
        time.sleep(60)
    elif MODE == "cpu_in_call":
        send({"t": "call", "cid": 1, "hid": 1, "args": []})   # pauses the wall-clock deadline
        for _ in range(4):
            threading.Thread(target=spin, daemon=True).start()
        time.sleep(60)
    """
)


def _fake_worker(tmp_path: Path, mode: str) -> str:
    script = tmp_path / f"fake_{mode}.py"
    script.write_text(_FAKE.replace("__MODE__", repr(mode)))
    wrapper = tmp_path / f"fake_{mode}.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


async def _fake(tmp_path: Path, mode: str, **kwargs: Any) -> AsyncIsolatedRuntime:
    kwargs.setdefault("request_timeout", 20)
    return await AsyncIsolatedRuntime.create(
        kwargs.pop("config", RuntimeConfig()),
        python=_fake_worker(tmp_path, mode),
        **kwargs,
    )


class TestFrameReader:
    def test_the_frame_cap_is_enforced_before_the_payload_is_buffered(self) -> None:
        reader = _aio._FrameReader(max_frame=1024)  # noqa: SLF001
        reader.data_received((2**31).to_bytes(4, "little") + b"x" * 100)
        assert reader.error is not None and "exceeds" in reader.error
        assert not reader.partial() and not reader.frames

    def test_frames_split_across_reads_are_reassembled(self) -> None:
        reader = _aio._FrameReader()  # noqa: SLF001
        blob = b"".join(len(p).to_bytes(4, "little") + p for p in (b"ab", b"", b"cde"))
        for i in range(len(blob)):
            reader.data_received(blob[i : i + 1])
        assert list(reader.frames) == [b"ab", b"", b"cde"]
        assert not reader.partial()


class TestUntrustedWorker:
    @pytest.mark.parametrize(
        "mode", ["oversize", "badjson", "unknown_call", "bad_tag", "wrong_id", "deep"]
    )
    async def test_a_hostile_frame_discards_the_worker_and_spares_the_parent(
        self, tmp_path: Path, mode: str
    ) -> None:
        rt = await _fake(tmp_path, mode)
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="protocol"):
            await rt.eval("1")
        assert time.monotonic() - start < 10
        assert rt.is_closed()
        assert await _gone(rt._proc)  # noqa: SLF001
        await rt.close()

    async def test_a_dripping_worker_cannot_stall_the_parent(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "drip", request_timeout=1.5)
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="hard deadline"):
            await rt.eval("1")
        assert time.monotonic() - start < 10
        assert await _gone(rt._proc)  # noqa: SLF001
        await rt.close()

    async def test_a_huge_error_message_is_truncated(self, tmp_path: Path) -> None:
        rt = await _fake(tmp_path, "bigerr")
        try:
            with pytest.raises(RuntimeError) as caught:
                await rt.eval("1")
        finally:
            await rt.close()
        assert len(str(caught.value)) < 70_000
        assert "more characters" in str(caught.value)

    async def test_an_unknown_error_kind_cannot_pick_the_exception_class(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "unknown_kind")
        try:
            with pytest.raises(RuntimeError) as caught:
                await rt.eval("1")
        finally:
            await rt.close()
        assert type(caught.value) is RuntimeError

    async def test_terminal_escapes_in_a_remote_error_are_neutralised(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "escape_error")
        try:
            with pytest.raises(RuntimeError) as caught:
                await rt.eval("1")
        finally:
            await rt.close()
        text = str(caught.value)
        assert "\x1b" not in text and "\x07" not in text
        assert "at line 2" in text

    @pytest.mark.parametrize("mode", ["native_crash", "escape_crash"])
    async def test_a_dying_workers_stderr_is_sanitised(
        self, tmp_path: Path, mode: str
    ) -> None:
        rt = await _fake(tmp_path, mode)
        with pytest.raises(WorkerCrashed) as caught:
            await rt.eval("1")
        await rt.close()
        text = str(caught.value)
        assert "exit code 1" in text
        assert "0x00007fff" not in text and ".dylib" not in text  # no ASLR map
        assert "\x1b" not in text and "\x07" not in text

    async def test_a_duplicate_result_poisons_the_next_command(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "dup_result")
        try:
            assert await rt.eval("1") == 1
            with pytest.raises(WorkerCrashed):
                await rt.eval("2")
        finally:
            await rt.close()

    async def test_a_flood_of_host_calls_hits_the_budget(self, tmp_path: Path) -> None:
        calls: list[int] = []
        rt = await _fake(tmp_path, "call_flood", max_host_calls=100, request_timeout=30)
        rt._handlers[1] = (lambda: calls.append(1), False)  # noqa: SLF001
        with pytest.raises(WorkerCrashed, match="max_host_calls"):
            await rt.eval("1")
        assert len(calls) == 100
        await rt.close()

    async def test_the_worker_starts_with_an_empty_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYDENO_TEST_SECRET", "hunter2")
        rt = await _fake(tmp_path, "env")
        try:
            keys = await rt.eval("1")
        finally:
            await rt.close()
        assert "PYDENO_TEST_SECRET" not in keys
        assert not {"PATH", "HOME", "USER", "VIRTUAL_ENV"} & set(keys)

    async def test_a_worker_that_never_starts_is_an_error_not_a_hang(
        self, tmp_path: Path
    ) -> None:
        wrapper = tmp_path / "dead.sh"
        wrapper.write_text("#!/bin/sh\nexit 3\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        with pytest.raises(WorkerCrashed, match="startup|start"):
            await AsyncIsolatedRuntime.create(RuntimeConfig(), python=str(wrapper))

    async def test_a_worker_that_stops_reading_is_killed_after_the_stall_timeout(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "noread", write_stall_timeout=0.5)
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="stopped reading"):
            await rt.eval("'" + "x" * (4 * MIB) + "'")
        assert time.monotonic() - start < 5
        assert await _gone(rt._proc)  # noqa: SLF001
        await rt.close()

    async def test_a_thread_bomb_during_a_command_is_killed(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "threads")
        with pytest.raises(WorkerCrashed, match="threads"):
            await rt.eval("1")
        assert f"limit {_MAX_WORKER_THREADS}" in str(
            rt._verdict_error()  # noqa: SLF001
        )
        await rt.close()

    async def test_a_thread_bomb_while_idle_is_killed(self, tmp_path: Path) -> None:
        rt = await _fake(tmp_path, "idle_threads")
        assert await _gone(rt._proc, within=5)  # noqa: SLF001
        assert rt.is_closed()
        with pytest.raises(WorkerCrashed):
            await rt.eval("1")
        await rt.close()

    async def test_a_worker_burning_cpu_while_idle_is_killed(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "idle_spin")
        assert await _gone(rt._proc, within=8)  # noqa: SLF001 - 2 s allowance, then a sample
        assert rt.is_closed()
        await rt.close()

    async def test_the_cpu_cap_fires_while_the_deadline_is_paused(
        self, tmp_path: Path
    ) -> None:
        """A host call is outstanding, so the wall-clock deadline is paused; the worker burns CPU
        on four threads meanwhile. CPU is what it cannot hide."""

        async def slow() -> None:
            await asyncio.sleep(30)

        rt = await _fake(
            tmp_path, "cpu_in_call", request_timeout=1.0, max_host_wait=None
        )
        rt._handlers[1] = (slow, True)  # noqa: SLF001
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="CPU"):
            await rt.eval("1")
        assert time.monotonic() - start < 10
        await rt.close()

    async def test_memory_is_enforced_while_a_sync_handler_runs(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(
            tmp_path, "alloc_during_call", max_memory=150 * MIB, request_timeout=60
        )
        rt._handlers[1] = (lambda: time.sleep(8), False)  # noqa: SLF001
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="max_memory"):
            await rt.eval("1")
        # long before the 8 s handler returned
        assert time.monotonic() - start < 5
        await rt.close()


class TestWorkerCannotForgeCapabilityBookkeeping:
    async def test_a_reused_token_ends_the_session(self, tmp_path: Path) -> None:
        rt = await _fake(tmp_path, "dup_token")
        assert await rt.bind_function("harmless", lambda: 1) == 7
        with pytest.raises(WorkerCrashed, match="reused"):
            await rt.bind_function("privileged", lambda: 2)
        assert rt.is_closed()
        await rt.close()

    async def test_a_token_map_that_does_not_match_ends_the_session(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "wrong_object_keys")
        with pytest.raises(WorkerCrashed, match="malformed"):
            await rt.bind_object("api", {"add": lambda a, b: a + b})
        assert rt.is_closed()
        await rt.close()

    async def test_revoking_drops_the_handler_even_if_the_worker_never_answers(
        self, tmp_path: Path
    ) -> None:
        rt = await _fake(tmp_path, "dup_token")
        token = await rt.bind_function("f", lambda: 1)
        (hid,) = list(rt._handlers)  # noqa: SLF001
        await rt.revoke_op(token)
        assert hid not in rt._handlers  # noqa: SLF001
        await rt.close()

    async def test_a_call_in_flight_when_revoked_gets_an_error_not_a_kill(
        self, tmp_path: Path
    ) -> None:
        called: list[int] = []
        rt = await _fake(tmp_path, "revoked_call")
        token = await rt.bind_function("f", lambda: called.append(1))
        assert await rt.revoke_op(token) is True
        etype, _ = await rt.eval("f()")
        assert etype == "PermissionError"
        assert called == []
        assert not rt.is_closed()
        await rt.close()

    async def test_the_inflight_cap_is_runtime_wide(self, tmp_path: Path) -> None:
        async def slow() -> None:
            await asyncio.sleep(5)

        rt = await _fake(tmp_path, "inflight", max_inflight_host_calls=3)
        rt._handlers[1] = (slow, True)  # noqa: SLF001
        assert await rt.eval("1") == 1  # leaves three calls running
        assert "in flight" in await rt.eval(
            "2"
        )  # the fourth, a command later, is refused
        await rt.close()

    async def test_module_handlers_only_get_bounded_strings(
        self, tmp_path: Path
    ) -> None:
        seen: list[object] = []
        rt = await _fake(tmp_path, "bad_specifier")
        await rt.set_module_resolver(lambda spec, ref: seen.append(spec))
        assert await rt.eval("1") == "ValueError"
        assert seen == []
        await rt.close()

    async def test_console_handler_only_gets_console_levels(
        self, tmp_path: Path
    ) -> None:
        seen: list[object] = []
        rt = await _fake(
            tmp_path,
            "bad_console",
            config=RuntimeConfig(on_console=lambda *a: seen.append(a)),
        )
        assert await rt.eval("1") == "ValueError"
        assert seen == []
        await rt.close()
