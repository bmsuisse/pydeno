"""The worker's start-up trims: what it does not import, and the precompiled Seatbelt profile.

Each check runs in a fresh interpreter, because applying a sandbox cannot be undone and because
what a module imports is only visible in a process that has not imported it yet.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

import pydeno

_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(pydeno.__file__)))


def _run(code: str) -> str:
    # Started the way the real worker is: isolated, no `site`, the package dir appended.
    boot = f"import sys; sys.path.append({_PACKAGE_PARENT!r})\n" + textwrap.dedent(code)
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", boot],
        env={},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_worker_never_loads_ssl() -> None:
    out = _run(
        """
        import pydeno._worker, asyncio.base_events, sys
        print(sys.modules["ssl"] is None, asyncio.base_events.ssl is None, "_ssl" in sys.modules)
        """
    )
    assert out.split() == ["True", "True", "False"]


def test_worker_does_not_import_typing_itself() -> None:
    # On 3.14+ nothing the worker needs imports `typing`; before that asyncio does anyway, so
    # there the check is only that pydeno's own modules import cleanly without it.
    out = _run(
        """
        import pydeno._worker, sys
        print("typing" in sys.modules)
        """
    )
    if sys.version_info >= (3, 14):
        assert out.split() == ["False"]


def test_worker_reaches_ready_with_ssl_blocked() -> None:
    # The full start-up (imports, sandbox, self-test, V8) still works without `ssl`.
    from pydeno import IsolatedRuntime

    with IsolatedRuntime(prewarm=False) as rt:
        assert rt.eval("1 + 1") == 2


@pytest.mark.darwin_only
def test_precompiled_seatbelt_is_what_gets_applied() -> None:
    out = _run(
        """
        from pydeno import _sandbox
        _sandbox.precompile_seatbelt()
        applied = _sandbox.apply()
        # Consumed by apply(): the precompiled profile was used, not sandbox_init.
        print(applied, _sandbox._precompile_thread is not None, len(_sandbox._precompiled))
        print("breaches", _sandbox.attest())
        """
    )
    assert out.splitlines() == ["seatbelt True 0", "breaches []"]


@pytest.mark.darwin_only
def test_seatbelt_falls_back_to_sandbox_init_when_precompile_fails() -> None:
    out = _run(
        """
        from pydeno import _sandbox
        _sandbox._SANDBOX_LIB = "/nonexistent/libsandbox.dylib"
        _sandbox.precompile_seatbelt()
        _sandbox._precompile_thread.join()
        print(len(_sandbox._precompiled), _sandbox.apply())
        print("breaches", _sandbox.attest())
        """
    )
    assert out.splitlines() == ["0 seatbelt", "breaches []"]


@pytest.mark.darwin_only
def test_precompile_is_a_no_op_once_started() -> None:
    out = _run(
        """
        from pydeno import _sandbox
        _sandbox.precompile_seatbelt()
        first = _sandbox._precompile_thread
        _sandbox.precompile_seatbelt()
        first.join()
        print(_sandbox._precompile_thread is first, len(_sandbox._precompiled))
        """
    )
    assert out.split() == ["True", "1"]


@pytest.mark.linux_only
def test_precompile_does_nothing_off_macos() -> None:
    # Linux must stay single-threaded until the sandbox is up (unshare, Landlock).
    out = _run(
        """
        import threading
        from pydeno import _sandbox
        _sandbox.precompile_seatbelt()
        print(_sandbox._precompile_thread is None, threading.active_count())
        """
    )
    assert out.split() == ["True", "1"]
