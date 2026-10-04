"""`SandboxPool` / `AsyncSandboxPool`: pre-started, single-use isolated runtimes.

What a pool must never do: hand one worker to two sessions, give a worker that served a session to
anyone else, fail a checkout because it is empty, or leave a process behind. And what it must keep:
everything a fresh `IsolatedRuntime` guarantees (the sandbox mode, the self-test, the limits).
"""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from pydeno import (
    AsyncIsolatedRuntime,
    AsyncSandboxPool,
    IsolatedRuntime,
    RuntimeConfig,
    RuntimeTimeout,
    SandboxPool,
    WorkerCrashed,
)


def _cfg() -> RuntimeConfig:
    return RuntimeConfig(timeout=10.0)


def _gone(proc: subprocess.Popen[bytes], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return False


def _ready_procs(pool: SandboxPool) -> list[subprocess.Popen[bytes]]:
    with pool._core.cond:  # noqa: SLF001
        return [rt._proc for rt in pool._core.ready]  # noqa: SLF001


class TestCheckout:
    def test_a_checkout_is_a_started_isolated_runtime(self) -> None:
        with SandboxPool(_cfg(), size=2) as pool:
            assert pool.wait_ready(30)
            with pool.checkout() as rt:
                assert isinstance(rt, IsolatedRuntime)
                assert rt.eval("6 * 7") == 42
                assert rt.v8_flags and "--jitless" in rt.v8_flags
                assert rt.sandbox != "none" or sys.platform not in ("darwin", "linux")

    def test_a_warm_checkout_takes_well_under_a_millisecond(self) -> None:
        with SandboxPool(_cfg(), size=2) as pool:
            assert pool.wait_ready(30)
            took = []
            for _ in range(2):
                start = time.perf_counter()
                rt = pool.checkout()
                took.append(time.perf_counter() - start)
                rt.close()
                pool.wait_ready(30)
            # The target is < 1 ms; a loaded CI machine gets slack, a cold start (~50 ms) does not.
            assert min(took) < 0.01, took
            assert pool.stats()["cold_starts"] == 0

    def test_session_options_apply_per_checkout(self) -> None:
        with SandboxPool(_cfg(), size=1, max_host_calls=7) as pool:
            with pool.checkout() as rt:
                assert rt._max_host_calls == 7  # noqa: SLF001
            with pool.checkout(max_host_calls=1, request_timeout=5) as rt:
                assert rt._max_host_calls == 1  # noqa: SLF001
                assert rt._request_timeout == 5.0  # noqa: SLF001
                rt.bind_function("tool", lambda: 1)
                with pytest.raises(WorkerCrashed, match="max_host_calls"):
                    rt.eval("tool(); tool(); tool()")

    def test_spawn_options_cannot_change_per_checkout(self) -> None:
        with SandboxPool(_cfg(), size=1) as pool:
            with pytest.raises(TypeError, match="fixed for the whole pool"):
                pool.checkout(jitless=False)
            with pytest.raises(TypeError, match="fixed for the whole pool"):
                pool.checkout(max_memory=10**9)
            with pytest.raises(ValueError, match="max_inflight_host_calls"):
                pool.checkout(max_inflight_host_calls=0)

    def test_invalid_pool_arguments_fail_at_construction(self) -> None:
        with pytest.raises(ValueError, match="size"):
            SandboxPool(size=0)
        with pytest.raises(ValueError, match="max_concurrent_starts"):
            SandboxPool(max_concurrent_starts=0)
        with pytest.raises(TypeError, match="prewarm"):
            SandboxPool(prewarm=False)
        with pytest.raises(ValueError, match="sandbox"):
            SandboxPool(sandbox="maybe")
        with pytest.raises(ValueError, match="max_host_calls"):
            SandboxPool(max_host_calls=-1)

    def test_require_means_what_it_means_for_a_fresh_runtime(self) -> None:
        """A platform that cannot give every sandbox layer refuses the pool exactly as it refuses
        `IsolatedRuntime(sandbox="require")`; one that can gives every pooled runtime all of it."""
        try:
            with IsolatedRuntime(_cfg(), sandbox="require") as fresh:
                expected = fresh.sandbox
        except WorkerCrashed:
            with pytest.raises(WorkerCrashed):
                SandboxPool(_cfg(), size=2, sandbox="require")
            return
        with SandboxPool(_cfg(), size=2, sandbox="require") as pool:
            assert pool.wait_ready(30)
            for _ in range(3):
                with pool.checkout() as rt:
                    assert rt.sandbox == expected
                    assert rt.eval("1") == 1

    def test_limits_are_those_of_a_fresh_runtime(self) -> None:
        with SandboxPool(RuntimeConfig(timeout=0.3), size=1, timeout_grace=0.5) as pool:
            with pool.checkout() as rt:
                assert rt.eval("typeof SharedArrayBuffer") == "undefined"
                with pytest.raises(RuntimeTimeout):
                    rt.eval("for (;;) {}")


class TestSingleUse:
    def test_no_worker_is_handed_to_two_sessions(self) -> None:
        """Many threads checking out at once, faster than the pool refills: every runtime and
        every worker process is distinct."""
        with SandboxPool(_cfg(), size=3, max_concurrent_starts=2) as pool:
            assert pool.wait_ready(30)
            got: list[IsolatedRuntime] = []
            lock = threading.Lock()
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    for _ in range(2):
                        rt = pool.checkout()
                        with lock:
                            got.append(rt)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            assert not errors, errors
            try:
                assert len(got) == 12
                assert len({id(rt) for rt in got}) == 12
                assert len({rt._proc.pid for rt in got}) == 12  # noqa: SLF001
                # None of them is still (or again) in the pool.
                pooled = {p.pid for p in _ready_procs(pool)}
                assert not pooled & {rt._proc.pid for rt in got}  # noqa: SLF001
                for rt in got:
                    assert rt.eval("1 + 1") == 2
            finally:
                for rt in got:
                    rt.close()

    def test_a_worker_that_served_a_session_is_never_reused(self) -> None:
        with SandboxPool(_cfg(), size=2) as pool:
            seen: set[int] = set()
            for _ in range(5):
                pool.wait_ready(30)
                rt = pool.checkout()
                pid = rt._proc.pid  # noqa: SLF001
                assert pid not in seen
                seen.add(pid)
                rt.eval("globalThis.leftover = 'secret'")
                proc = rt._proc  # noqa: SLF001
                rt.close()
                assert _gone(proc), "closing a checked-out runtime must end its worker"
                # Its replacement is a new process with a fresh global scope.
                pool.wait_ready(30)
                with pool.checkout() as nxt:
                    assert nxt._proc.pid != pid  # noqa: SLF001
                    assert nxt.eval("typeof leftover") == "undefined"
                    seen.add(nxt._proc.pid)  # noqa: SLF001

    def test_a_pooled_worker_that_died_while_waiting_is_discarded(self) -> None:
        with SandboxPool(_cfg(), size=2) as pool:
            assert pool.wait_ready(30)
            victims = _ready_procs(pool)
            for proc in victims:
                os.kill(proc.pid, 9)
            for proc in victims:
                assert _gone(proc)
            with pool.checkout() as rt:
                assert rt._proc.pid not in {p.pid for p in victims}  # noqa: SLF001
                assert rt.eval("2 + 2") == 4


class TestExhaustion:
    def test_an_empty_pool_falls_back_to_a_cold_start(self) -> None:
        with SandboxPool(_cfg(), size=1, max_concurrent_starts=1) as pool:
            assert pool.wait_ready(30)
            held = [pool.checkout() for _ in range(4)]
            try:
                assert pool.stats()["cold_starts"] >= 1
                assert len({rt._proc.pid for rt in held}) == 4  # noqa: SLF001
                for rt in held:
                    assert rt.eval("3") == 3
            finally:
                for rt in held:
                    rt.close()

    def test_background_failures_never_fail_a_checkout(self) -> None:
        """A replacement that cannot start is retried in the background and reported in
        `stats()`; checkouts meanwhile get cold starts."""
        with SandboxPool(_cfg(), size=1) as pool:
            assert pool.wait_ready(30)
            core = pool._core  # noqa: SLF001
            real_new = core._create

            def failing_new(session: object = None) -> IsolatedRuntime:
                if session is None:  # a background start; checkouts pass their session
                    raise OSError("no processes left")
                return real_new(session)  # type: ignore[arg-type]

            core._create = failing_new  # type: ignore[method-assign]
            first = pool.checkout()
            second = pool.checkout()  # pool empty, the filler failing: a cold start
            try:
                assert second.eval("1") == 1
                deadline = time.monotonic() + 5
                while (
                    pool.stats()["last_error"] is None and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                assert "no processes left" in str(pool.stats()["last_error"])
            finally:
                first.close()
                second.close()
                core._create = real_new  # type: ignore[method-assign]


class TestClose:
    def test_close_drains_the_pool(self) -> None:
        pool = SandboxPool(_cfg(), size=3)
        assert pool.wait_ready(30)
        mine = pool.checkout()
        waiting = _ready_procs(pool)
        pool.close()
        for proc in waiting:
            assert _gone(proc), "a pooled worker survived close()"
        assert pool.stats()["ready"] == 0
        assert pool.stats()["starting"] == 0
        with pytest.raises(RuntimeError, match="closed"):
            pool.checkout()
        # A runtime already checked out is the caller's, not the pool's.
        assert mine.eval("5") == 5
        mine.close()
        pool.close()  # idempotent

    def test_a_pool_dropped_without_close_kills_its_workers(self) -> None:
        pool = SandboxPool(_cfg(), size=2)
        assert pool.wait_ready(30)
        waiting = _ready_procs(pool)
        del pool
        gc.collect()
        for proc in waiting:
            assert _gone(proc)


class TestNoLeakedProcesses:
    """In a fresh interpreter, where every child is the pool's: fill and drain concurrently, close,
    and nothing may be left (no live worker, no zombie, no descriptor)."""

    SCRIPT = textwrap.dedent(
        """
        import os, threading
        from pydeno import IsolatedRuntime, RuntimeConfig, SandboxPool
        import pydeno._isolated as _impl
        _impl._refill_spare = lambda: None  # the global spare is not the pool's to account for

        def open_fds():
            for d in ("/proc/self/fd", "/dev/fd"):
                if os.path.isdir(d):
                    return len(os.listdir(d))

        with IsolatedRuntime(RuntimeConfig(timeout=5)) as warm:
            warm.eval("1")
        before = open_fds()

        for _ in range(2):
            pool = SandboxPool(RuntimeConfig(timeout=5), size=3, max_concurrent_starts=3)
            def drain():
                for _ in range(4):
                    with pool.checkout() as rt:
                        assert rt.eval("1 + 1") == 2
            threads = [threading.Thread(target=drain) for _ in range(4)]
            for t in threads: t.start()
            for t in threads: t.join()
            pool.close()

        after = open_fds()
        try:
            os.waitpid(-1, os.WNOHANG)
            children = "a-child-is-still-there"
        except ChildProcessError:
            children = "none"
        print(after - before, children)
        """
    )

    def test_concurrent_fill_and_drain_leaks_nothing(self) -> None:
        done = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert done.returncode == 0, done.stderr
        grew, children = done.stdout.split()
        assert children == "none", "a worker process outlived its pool"
        assert int(grew) == 0, f"{grew} descriptors leaked"

    def test_a_forked_child_never_gets_the_parents_workers(self) -> None:
        script = textwrap.dedent(
            """
            import os
            from pydeno import RuntimeConfig, SandboxPool
            pool = SandboxPool(RuntimeConfig(timeout=5), size=2)
            assert pool.wait_ready(30)
            parents = {rt._proc.pid for rt in pool._core.ready}
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    with pool.checkout() as rt:
                        ok = rt._proc.pid not in parents and rt.eval("1 + 1") == 2
                    pool.close()
                    os.write(w, b"ok" if ok else b"reused")
                finally:
                    os._exit(0)
            os.waitpid(pid, 0)
            print(os.read(r, 16).decode())
            # The child must not have killed the parent's workers either.
            print(all(rt._proc.poll() is None for rt in pool._core.ready))
            pool.close()
            """
        )
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.split() == ["ok", "True"]


class TestAsyncPool:
    def test_checkout_close_and_exhaustion(self) -> None:
        async def main() -> None:
            async with AsyncSandboxPool(_cfg(), size=2) as pool:
                assert await pool.wait_ready(30)
                rt = await pool.checkout()
                assert isinstance(rt, AsyncIsolatedRuntime)
                assert await rt.eval("Promise.resolve(42)") == 42
                held = [rt] + [await pool.checkout() for _ in range(3)]
                try:
                    assert len({r._proc.pid for r in held}) == 4  # noqa: SLF001
                    assert pool.stats()["cold_starts"] >= 1
                finally:
                    for r in held:
                        await r.close()
                async with pool.checkout(max_host_calls=3) as scoped:
                    assert scoped._max_host_calls == 3  # noqa: SLF001
                    assert await scoped.eval("1 + 1") == 2
                assert scoped.is_closed()
                waiting = [r._proc for r in pool._ready]  # noqa: SLF001
            for proc in waiting:
                assert _gone(proc)
            with pytest.raises(RuntimeError, match="closed"):
                await pool.checkout()

        asyncio.run(main())

    def test_no_worker_is_handed_to_two_concurrent_checkouts(self) -> None:
        async def main() -> None:
            async with AsyncSandboxPool(_cfg(), size=3) as pool:
                assert await pool.wait_ready(30)
                got = await asyncio.gather(*(pool.checkout() for _ in range(8)))
                try:
                    assert len({r._proc.pid for r in got}) == 8  # noqa: SLF001
                finally:
                    await asyncio.gather(*(r.close() for r in got))

        asyncio.run(main())

    def test_options_are_validated_without_starting_anything(self) -> None:
        with pytest.raises(ValueError, match="sandbox"):
            AsyncSandboxPool(sandbox="maybe")
        with pytest.raises(TypeError, match="prewarm"):
            AsyncSandboxPool(prewarm=True)

        async def main() -> None:
            pool = AsyncSandboxPool(_cfg(), size=1)
            with pytest.raises(RuntimeError, match="start the pool"):
                await pool.checkout()
            async with pool:
                with pytest.raises(TypeError, match="fixed for the whole pool"):
                    await pool.checkout(clock=0)

        asyncio.run(main())
