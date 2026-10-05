"""The usual ways a sandbox gets worn down over time, rather than broken in one shot.

A hostile guest, or a careless caller, does not need an escape if it can leak the host dry or
catch the runtime in a bad moment. Each test here repeats something many times, or does it
concurrently, and then asserts that what belongs to the host (processes, file descriptors,
threads, registered handlers, memory) is back where it started.

Counts are kept small enough to run in seconds; a leak of even one resource per iteration shows.
"""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import threading
import time

import pytest

from pydeno import IsolatedRuntime, RuntimeConfig, WorkerCrashed
from pydeno import _sandbox


def _child_pids() -> set[int]:
    """Direct children of this process. /proc where it exists, `ps` otherwise (macOS)."""
    me = os.getpid()
    if sys.platform.startswith("linux"):
        out = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    # "pid (comm) state ppid ..." and comm may contain spaces and parens
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


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


def _settle() -> None:
    gc.collect()
    time.sleep(0.3)  # let a spare worker finish starting, and a killed one be reaped


@pytest.fixture
def baseline() -> dict[str, int]:
    # One runtime opened and closed first, so a prewarmed spare (a deliberate extra child) and any
    # lazily created thread or descriptor already exist in the "before" picture.
    with IsolatedRuntime() as rt:
        rt.eval("1")
    _settle()
    return {
        "children": len(_child_pids()),
        "fds": _fds(),
        "threads": threading.active_count(),
    }


def _assert_back_to_baseline(
    before: dict[str, int], what: str, thread_slack: int = 1
) -> None:
    _settle()
    after = {
        "children": len(_child_pids()),
        "fds": _fds(),
        "threads": threading.active_count(),
    }
    assert after["children"] <= before["children"], (what, before, after)
    # A few descriptors of slack: the interpreter and pytest may open one or two lazily.
    assert after["fds"] <= before["fds"] + 3, (what, before, after)
    assert after["threads"] <= before["threads"] + thread_slack, (what, before, after)


class TestNothingLeaksAcrossLifecycles:
    def test_creating_and_closing_many_runtimes(self, baseline: dict[str, int]) -> None:
        for i in range(25):
            with IsolatedRuntime() as rt:
                assert rt.eval(f"{i} + 1") == i + 1
        _assert_back_to_baseline(baseline, "25 create/close cycles")

    def test_runtimes_that_crash_are_reaped(self, baseline: dict[str, int]) -> None:
        for _ in range(8):
            rt = IsolatedRuntime(request_timeout=1.5)
            with pytest.raises(Exception):
                rt.eval("while (true) {}")
            assert rt.is_closed()
        _assert_back_to_baseline(baseline, "8 timeouts")

    def test_runtimes_that_are_killed_from_outside_are_reaped(
        self, baseline: dict[str, int]
    ) -> None:
        for _ in range(8):
            rt = IsolatedRuntime()
            os.kill(rt._proc.pid, 9)  # noqa: SLF001 - the point of the test
            with pytest.raises(WorkerCrashed):
                rt.eval("1")
        _assert_back_to_baseline(baseline, "8 external SIGKILLs")

    def test_a_runtime_that_is_dropped_without_close_is_reaped(
        self, baseline: dict[str, int]
    ) -> None:
        for _ in range(6):
            rt = IsolatedRuntime()
            rt.eval("1")
            del rt
        _assert_back_to_baseline(baseline, "6 dropped runtimes")

    def test_cancelling_eval_async_does_not_orphan_workers(
        self, baseline: dict[str, int]
    ) -> None:
        async def one() -> None:
            rt = IsolatedRuntime()
            task = asyncio.ensure_future(rt.eval_async("new Promise(() => {})"))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            rt.close()

        async def go() -> None:
            for _ in range(6):
                await one()

        asyncio.run(go())
        _assert_back_to_baseline(baseline, "6 cancelled evals")


class TestNothingAccumulatesInsideOneRuntime:
    def test_binding_and_revoking_leaves_no_registry_behind(self) -> None:
        with IsolatedRuntime() as rt:
            for i in range(300):
                token = rt.bind_function(f"f{i % 5}", lambda: 1)
                rt.revoke_op(token)
            assert rt._handlers == {}  # noqa: SLF001
            assert rt._token_to_hid == {}  # noqa: SLF001
            # the memory of revoked ids is bounded, not one entry per revocation forever
            assert len(rt._revoked_hids) <= 4096  # noqa: SLF001

    def test_many_evals_do_not_grow_the_worker_without_bound(self) -> None:
        with IsolatedRuntime() as rt:
            for _ in range(50):  # warm up: first evals allocate lazily
                rt.eval("({a: [1, 2, 3], b: 'x'.repeat(100)})")
            pid = rt._proc.pid  # noqa: SLF001
            before = _sandbox.rss_bytes(pid)
            assert before is not None
            for _ in range(1500):
                rt.eval("({a: [1, 2, 3], b: 'x'.repeat(100)})")
            after = _sandbox.rss_bytes(pid)
            assert after is not None
            # Generous: V8 grows and collects. A per-eval leak of even a kilobyte would add 1.5 MB.
            assert after - before < 24 * 1024 * 1024, (before, after)

    def test_error_paths_do_not_accumulate(self) -> None:
        with IsolatedRuntime() as rt:
            for _ in range(50):
                try:
                    rt.eval("throw new Error('x'.repeat(1000))")
                except Exception:
                    pass
            pid = rt._proc.pid  # noqa: SLF001
            before = _sandbox.rss_bytes(pid)
            for _ in range(800):
                try:
                    rt.eval("throw new Error('x'.repeat(1000))")
                except Exception:
                    pass
            after = _sandbox.rss_bytes(pid)
            assert before is not None and after is not None
            assert after - before < 24 * 1024 * 1024, (before, after)
            assert rt.eval("1") == 1

    def test_host_call_traffic_does_not_grow_the_parent(self) -> None:
        with IsolatedRuntime() as rt:
            rt.bind_function("echo", lambda v: v)
            for _ in range(50):
                rt.eval("echo(1)")
            before = _sandbox.rss_bytes(os.getpid())
            for _ in range(1500):
                rt.eval("echo({k: 'v'.repeat(50)})")
            after = _sandbox.rss_bytes(os.getpid())
            assert before is not None and after is not None
            assert after - before < 32 * 1024 * 1024, (before, after)


class TestRaces:
    def test_concurrent_evals_from_many_threads_never_mix_up_answers(self) -> None:
        wrong: list[tuple[int, object]] = []
        with IsolatedRuntime(RuntimeConfig(timeout=30.0)) as rt:

            def work(n: int) -> None:
                for i in range(40):
                    value = n * 1000 + i
                    got = rt.eval(f"{value}")
                    if got != value:
                        wrong.append((value, got))

            threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            assert not any(t.is_alive() for t in threads)
        assert wrong == []

    def test_close_racing_an_eval_never_hangs_and_ends_in_a_clean_error(self) -> None:
        for _ in range(5):
            rt = IsolatedRuntime(request_timeout=20)
            outcome: list[object] = []

            def run(rt: IsolatedRuntime = rt, outcome: list[object] = outcome) -> None:
                try:
                    outcome.append(rt.eval("while (true) {}"))
                except BaseException as exc:  # noqa: BLE001
                    outcome.append(exc)

            t = threading.Thread(target=run)
            t.start()
            time.sleep(0.2)
            rt.close()
            t.join(30)
            assert not t.is_alive(), "close() left an eval hanging"
            assert isinstance(outcome[0], Exception), outcome

    def test_a_dead_runtime_answers_with_an_error_every_time_not_a_hang(self) -> None:
        rt = IsolatedRuntime(request_timeout=20)
        rt.close()
        start = time.monotonic()
        for _ in range(20):
            with pytest.raises(Exception):
                rt.eval("1")
        assert time.monotonic() - start < 10

    def test_many_runtimes_in_parallel(self, baseline: dict[str, int]) -> None:
        errors: list[BaseException] = []

        def one(n: int) -> None:
            try:
                with IsolatedRuntime() as rt:
                    for i in range(5):
                        assert rt.eval(f"{n} * 100 + {i}") == n * 100 + i
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        assert errors == []
        # The shared worker pools grow to a high-water mark when ten start at once and keep those
        # threads; a leak adds a thread per runtime (ten), so a few threads of slack still catch it.
        _assert_back_to_baseline(baseline, "10 parallel runtimes", thread_slack=4)

    def test_a_host_function_that_closes_the_runtime_ends_cleanly(self) -> None:
        rt = IsolatedRuntime(request_timeout=20)
        threading_error: list[BaseException] = []

        def sabotage() -> int:
            try:
                rt.close()
            except BaseException as exc:  # noqa: BLE001
                threading_error.append(exc)
            return 1

        rt.bind_function("sabotage", sabotage)
        with pytest.raises(Exception):
            rt.eval("sabotage(); 1")
        assert rt.is_closed()
