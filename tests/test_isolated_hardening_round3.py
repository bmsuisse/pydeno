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
