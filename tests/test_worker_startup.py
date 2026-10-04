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
        print(applied, _sandbox.SEATBELT_PATH, repr(_sandbox.PRECOMPILE_ERROR))
        print("breaches", _sandbox.attest())
        """
    )
    assert out.splitlines() == ["seatbelt precompiled ''", "breaches []"]


@pytest.mark.darwin_only
def test_precompiled_apply_reports_success_only_when_in_force() -> None:
    # `_apply_precompiled_seatbelt` must say True only for a profile that sandbox_apply accepted.
    out = _run(
        """
        from pydeno import _sandbox
        _sandbox.precompile_seatbelt()
        _sandbox._precompile_thread.join()
        profile, _, free = _sandbox._precompiled.pop()
        _sandbox._precompiled.append((profile, lambda _p: -1, free))
        print(_sandbox._apply_precompiled_seatbelt(), repr(_sandbox.PRECOMPILE_ERROR))
        """
    )
    assert out.splitlines() == ["False 'sandbox_apply returned -1'"]


@pytest.mark.darwin_only
def test_seatbelt_falls_back_to_sandbox_init_when_apply_fails() -> None:
    out = _run(
        """
        import os
        from pydeno import _sandbox
        _sandbox.precompile_seatbelt()
        _sandbox._precompile_thread.join()
        profile, _, free = _sandbox._precompiled.pop()
        _sandbox._precompiled.append((profile, lambda _p: -1, free))
        print(_sandbox.apply(), _sandbox.SEATBELT_PATH, repr(_sandbox.PRECOMPILE_ERROR))
        # The fallback really put the profile in force: no file can be opened any more.
        try:
            os.open("/etc/hosts", os.O_RDONLY)
            print("open allowed")
        except PermissionError:
            print("open denied")
        print("breaches", _sandbox.attest())
        """
    )
    assert out.splitlines() == [
        "seatbelt sandbox_init 'sandbox_apply returned -1'",
        "open denied",
        "breaches []",
    ]


@pytest.mark.darwin_only
def test_seatbelt_falls_back_to_sandbox_init_when_the_library_is_missing() -> None:
    out = _run(
        """
        from pydeno import _sandbox
        _sandbox._SANDBOX_LIB = "/nonexistent/libsandbox.dylib"
        _sandbox.precompile_seatbelt()
        _sandbox._precompile_thread.join()
        print(len(_sandbox._precompiled), _sandbox.apply(), _sandbox.SEATBELT_PATH)
        print(_sandbox.PRECOMPILE_ERROR.startswith("libsandbox unavailable:"))
        print("breaches", _sandbox.attest())
        """
    )
    assert out.splitlines() == ["0 seatbelt sandbox_init", "True", "breaches []"]


@pytest.mark.darwin_only
def test_a_profile_that_does_not_compile_keeps_the_compilers_message() -> None:
    # A broken profile is told apart from a missing library: the compiler's own error text is
    # kept (and its buffer freed). The real profile is restored before applying, so the fallback
    # still confines the process.
    out = _run(
        """
        from pydeno import _sandbox
        real = _sandbox._SEATBELT_PROFILE
        _sandbox._SEATBELT_PROFILE = "(version 1) (deny default) (no-such-operation)"
        _sandbox.precompile_seatbelt()
        _sandbox._precompile_thread.join()
        _sandbox._SEATBELT_PROFILE = real
        error = _sandbox.PRECOMPILE_ERROR
        print(error.startswith("profile did not compile: "), len(error) > 25)
        print(_sandbox.apply(), _sandbox.SEATBELT_PATH, "precompile:" in _sandbox.seatbelt_note())
        print("breaches", _sandbox.attest())
        """
    )
    assert out.splitlines() == [
        "True True",
        "seatbelt sandbox_init True",
        "breaches []",
    ]


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
