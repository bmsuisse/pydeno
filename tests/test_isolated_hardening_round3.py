"""Host-side hardening from the second review round: fork safety, bind names, crash messages."""

import subprocess
import sys
import textwrap

import pytest

from pydeno import IsolatedRuntime

_FORK = textwrap.dedent(
    """
    import os, sys
    from pydeno import IsolatedRuntime
    rt = IsolatedRuntime()
    assert rt.eval("1 + 1") == 2
    pid = os.fork()
    if pid == 0:
        try:
            rt.eval("1")
        except RuntimeError as exc:
            assert "fork" in str(exc), exc
        else:
            os._exit(3)
        rt.close()  # closing the inherited runtime must not touch the parent's worker either
        sys.exit(0)  # a normal exit: atexit hooks and finalizers run
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, status
    assert rt.eval("2 + 2") == 4
    rt.close()
    print("ok")
    """
)


def test_a_forked_child_exiting_does_not_kill_the_parents_worker() -> None:
    # Pre-fork servers create runtimes before forking. The child's atexit hook and finalizers used
    # to SIGKILL every worker it had inherited, which are the parent's.
    done = subprocess.run(
        [sys.executable, "-c", _FORK], capture_output=True, text=True, timeout=120
    )
    assert done.stdout.strip() == "ok", done.stderr


@pytest.mark.parametrize(
    "name", ["x = 1; globalThis.leak", "a.b", "__proto__", "1abc", "", "a b"]
)
def test_bind_names_must_be_plain_identifiers(name: str) -> None:
    with IsolatedRuntime() as rt:
        with pytest.raises((ValueError, TypeError)):
            rt.bind_function(name, lambda: 1)
        with pytest.raises((ValueError, TypeError)):
            rt.bind_object(name, {"f": lambda: 1})
        with pytest.raises((ValueError, TypeError)):
            rt.bind_object("ok", {name: lambda: 1})
        assert rt.eval("typeof globalThis.leak") == "undefined"


def test_a_crash_message_does_not_carry_native_stack_frames() -> None:
    from pydeno._isolated import _NATIVE_FRAME

    for line in (
        "20 libsystem_pthread.dylib 0x000000018b660c1c thread_start + 8",
        "/lib/aarch64-linux-gnu/libc.so.6(+0xedeec) [0xffff8575deec]",
    ):
        assert _NATIVE_FRAME.search(line)
    assert not _NATIVE_FRAME.search("RuntimeError: sandbox self-test failed")


def test_the_spare_lock_is_replaced_in_a_forked_child() -> None:
    # A parent thread may hold the lock at the instant of the fork; the child must not inherit it.
    from pydeno import _isolated

    _isolated._SPARE_LOCK.acquire()  # noqa: SLF001
    try:
        _isolated._forget_parents_workers()  # noqa: SLF001
        assert not _isolated._SPARE_LOCK.locked()  # noqa: SLF001
    finally:
        pass  # the replaced lock is the live one; the old one is simply abandoned


def test_dangling_async_host_calls_count_across_commands() -> None:
    """`max_inflight_host_calls` bounds the calls still running, not one command's: without that,
    each command could leave a full quota of tasks behind and the next would get a fresh quota."""
    import asyncio

    from pydeno import RuntimeConfig

    async def go() -> tuple[int, object]:
        started: list[int] = []

        async def hang() -> None:
            started.append(1)
            await asyncio.sleep(30)

        with IsolatedRuntime(
            RuntimeConfig(timeout=20.0), max_inflight_host_calls=4
        ) as rt:
            rt.bind_function("hang", hang)
            await rt.eval_async(
                "for (let i = 0; i < 4; i++) hang(); 1"
            )  # leaves 4 hanging
            await asyncio.sleep(0.3)
            # a fifth call, in a later command, is refused instead of adding a fifth task
            out = await rt.eval_async(
                "(async () => { try { await hang(); return 'ran' } "
                "catch (e) { return e.message } })()"
            )
            return len(started), out

    count, message = asyncio.run(go())
    assert count == 4, count
    assert "in flight" in str(message), message


def test_a_handler_that_waits_on_another_thread_calling_back_in_times_out_instead_of_deadlocking() -> (
    None
):
    """The re-entrancy guard only sees the same thread; this is the cross-thread version."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from pydeno import RuntimeConfig, RuntimeTimeout

    with IsolatedRuntime(
        RuntimeConfig(timeout=20.0), request_timeout=3, redact_host_errors=False
    ) as rt:
        pool = ThreadPoolExecutor(max_workers=1)

        def reenter_from_another_thread() -> object:
            return pool.submit(rt.eval, "1").result()

        rt.bind_function("reenter", reenter_from_another_thread)
        start = time.monotonic()
        message = rt.eval("try { reenter(); 'returned' } catch (e) { e.message }")
        elapsed = time.monotonic() - start
        pool.shutdown(wait=False)
    assert elapsed < 20, f"deadlocked for {elapsed:.0f}s"
    assert "timed out waiting" in str(message) or "deadlock" in str(message), message
    assert RuntimeTimeout  # the type the inner call raised


def test_eval_async_does_not_use_the_loops_default_executor() -> None:
    """A few runtimes parked on slow host calls must not starve the application's own
    `to_thread` / `run_in_executor(None)` work."""
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from pydeno import RuntimeConfig

    async def go() -> float:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        release = asyncio.Event()

        async def slow() -> int:
            await release.wait()
            return 1

        runtimes = [IsolatedRuntime(RuntimeConfig(timeout=30.0)) for _ in range(2)]
        try:
            for rt in runtimes:
                rt.bind_function("slow", slow)
            parked = [asyncio.ensure_future(rt.eval_async("slow()")) for rt in runtimes]
            await asyncio.sleep(0.5)
            start = time.monotonic()
            await asyncio.to_thread(lambda: 42)  # needs a default-executor thread
            took = time.monotonic() - start
            release.set()
            await asyncio.gather(*parked)
            return took
        finally:
            for rt in runtimes:
                rt.close()

    assert asyncio.run(go()) < 1.0


class TestALimitThatCannotBeMeasuredIsNotSilent:
    def test_require_refuses_to_start_when_memory_cannot_be_read(
        self, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        import pytest

        from pydeno import WorkerCrashed, _sandbox

        monkeypatch.setattr(_sandbox, "rss_bytes", lambda pid: None)
        with pytest.raises(WorkerCrashed, match="cannot be enforced"):
            IsolatedRuntime(sandbox="require", max_memory=512 * 1024 * 1024)

    def test_auto_warns(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import pytest

        from pydeno import _sandbox

        monkeypatch.setattr(_sandbox, "cpu_seconds", lambda pid: None)
        with pytest.warns(RuntimeWarning, match="cannot be enforced"):
            rt = IsolatedRuntime()
        rt.close()
