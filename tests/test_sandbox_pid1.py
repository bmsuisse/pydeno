"""A host that is PID 1 (a container's main process) must be able to run sandboxed workers (#128)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from pydeno import _sandbox


def test_attest_refuses_when_the_parent_changed() -> None:
    with pytest.raises(RuntimeError, match="orphaned"):
        _sandbox.attest(parent=os.getppid() + 1_000_000)


def test_attest_refuses_a_failed_getppid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getppid", lambda: -1)
    with pytest.raises(RuntimeError, match="orphaned"):
        _sandbox.attest()


@pytest.mark.parametrize(
    ("ppid", "parent", "changed"),
    [
        (1, 1, False),
        (7, 7, False),
        (1, 7, True),
        (7, 1, True),
        (-1, 1, True),
        (0, 0, True),
    ],
)
def test_a_pid_1_parent_is_not_an_orphan(ppid: int, parent: int, changed: bool) -> None:
    assert _sandbox._parent_changed(ppid, parent) is changed  # noqa: SLF001


def _can_unshare_pid_namespace() -> bool:
    if not shutil.which("unshare"):
        return False
    done = subprocess.run(  # noqa: S603 - fixed argv
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--mount-proc",
            "true",
        ],
        capture_output=True,
        timeout=30,
    )
    return done.returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces")
def test_a_sandboxed_worker_starts_under_a_pid_1_host() -> None:
    if not _can_unshare_pid_namespace():
        # Not a skip: CI budgets skips at zero. The pure check above still runs everywhere.
        pytest.xfail("PID namespaces are not available on this host")
    code = textwrap.dedent(
        """
        import os, pydeno
        assert os.getpid() == 1, os.getpid()
        with pydeno.IsolatedRuntime(sandbox="require", empty_root=False) as rt:
            print(rt.eval("1 + 1"))
        """
    )
    done = subprocess.run(  # noqa: S603 - fixed argv
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--mount-proc",
            sys.executable,
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "2"
