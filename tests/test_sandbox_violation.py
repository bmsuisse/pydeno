"""A never-legitimate syscall from inside a sandboxed worker kills it, and the parent says so.

Threat model: a V8 escape gives the attacker native code in the worker. The seccomp filter is an
allow-list; what it refuses it answers with EPERM, except the calls no runtime ever makes
(`ptrace`, `mount`, `bpf`, `execve`, ...), which end the process on the spot so a probing exploit
gets nothing to iterate on. These tests play that attacker with a *real* worker: a stand-in
interpreter runs the genuine worker (`pydeno._worker`) with one extra command that makes a raw
syscall. The list of calls is written here by intent, not read from `_sandbox.py`, so deleting an
entry from the filter cannot also delete its test.

Linux, inside a container only (`redteam`: it fires real privileged syscalls, with junk
arguments), deselected elsewhere by `tests/conftest.py`.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import sys
import warnings
from pathlib import Path

import pytest

import pydeno
from pydeno import IsolatedRuntime, WorkerCrashed, classify_error

pytestmark = [pytest.mark.linux_only, pytest.mark.redteam]

TABLES = json.loads((Path(__file__).parent / "data" / "syscalls.json").read_text())
ARCH = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64"}.get(
    os.uname().machine, os.uname().machine
)
NR = {name: int(nr) for nr, name in TABLES[ARCH].items()}

# The real worker, plus a `__fire__:<nr>` command. Everything it uses (ctypes, the C library's
# `syscall`) is loaded before the worker's sandbox goes up, exactly like the worker's own imports.
FIRING_WORKER = r"""
import ctypes, sys
sys.path.insert(0, {pkg_parent!r})
from pydeno import _worker
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
sink = ctypes.create_string_buffer(4096)
_handle = _worker._Worker._handle

def handle(self, message):
    code = message.get("code") or ""
    if message.get("t") == "eval" and code.startswith("__fire__:"):
        nr = int(code.split(":", 1)[1])
        ctypes.set_errno(0)
        args = [ctypes.c_long(0), ctypes.c_long(ctypes.addressof(sink))] + [ctypes.c_long(0)] * 4
        ret = libc.syscall(ctypes.c_long(nr), *args)
        return [int(ret), ctypes.get_errno()]
    return _handle(self, message)

_worker._Worker._handle = handle
_worker.main()
"""

# Never legitimate in a worker: the process must die, whatever the arguments.
NEVER_LEGITIMATE = [
    "ptrace",
    "process_vm_readv",
    "execve",
    "execveat",
    "mount",
    "unshare",
    "setns",
    "pivot_root",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "io_uring_setup",
    "memfd_create",
    "keyctl",
    "init_module",
    "kexec_load",
    "open_by_handle_at",
    "fsopen",
    "settimeofday",
    "sethostname",
]
# Refused, but with an error the caller can handle: libraries probe some of these and fall back,
# so a kill would turn a probe into an outage.
REFUSED_WITH_EPERM = [
    "socket",
    "connect",
    "sysinfo",
    "getpriority",
    "setuid",
    "inotify_init1",
]


@pytest.fixture
def firing_python(tmp_path: Path) -> str:
    script = tmp_path / "fire_worker.py"
    script.write_text(
        FIRING_WORKER.format(pkg_parent=str(Path(pydeno.__file__).parent.parent))
    )
    wrapper = tmp_path / "python"
    # The parent starts `python -I -m pydeno._worker`; this one runs the script instead.
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable!s} -I {script!s}\n")
    wrapper.chmod(0o755)
    return str(wrapper)


def _runtime(python: str) -> IsolatedRuntime:
    # "auto", not "require": the matrix also runs this where Landlock is hidden, and what is
    # tested here is the seccomp layer, asserted just below.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        rt = IsolatedRuntime(python=python, sandbox="auto", prewarm=False)
    assert "seccomp" in rt.sandbox.split("+"), rt.sandbox
    assert rt.eval("1 + 1") == 2  # an ordinary command still works
    return rt


@pytest.mark.parametrize("name", [n for n in NEVER_LEGITIMATE if n in NR])
def test_a_never_legitimate_syscall_kills_the_worker_and_is_reported(
    firing_python: str, name: str
) -> None:
    rt = _runtime(firing_python)
    try:
        with pytest.raises(WorkerCrashed) as caught:
            rt.eval(f"__fire__:{NR[name]}")
        info = classify_error(caught.value)
        assert info.kind == "sandbox_violation", str(caught.value)
        assert info.retryable is False
        assert "sandbox violation" in str(caught.value)
        assert rt.is_closed()
        assert rt._proc.returncode == -signal.SIGSYS  # noqa: SLF001
    finally:
        rt.close()


@pytest.mark.parametrize("name", [n for n in REFUSED_WITH_EPERM if n in NR])
def test_an_ordinary_refusal_is_an_error_and_the_worker_lives_on(
    firing_python: str, name: str
) -> None:
    rt = _runtime(firing_python)
    try:
        ret, err = rt.eval(f"__fire__:{NR[name]}")
        assert (ret, err) == (-1, errno.EPERM), name
        assert rt.eval("2 + 2") == 4
    finally:
        rt.close()


def test_a_call_nobody_listed_is_refused_by_default(firing_python: str) -> None:
    """The allow-list's point: a syscall no list mentions (here `mincore`, `times` and
    `getdents64`, harmless ones nothing in the worker needs) is refused, not allowed."""
    rt = _runtime(firing_python)
    try:
        for name in ("mincore", "times", "getdents64"):
            assert rt.eval(f"__fire__:{NR[name]}") == [-1, errno.EPERM], name
        assert rt.eval("3 + 3") == 6
    finally:
        rt.close()


def test_the_async_runtime_reports_the_violation_too(firing_python: str) -> None:
    import asyncio

    from pydeno import AsyncIsolatedRuntime

    async def go() -> BaseException:
        warnings.simplefilter("ignore", RuntimeWarning)
        rt = await AsyncIsolatedRuntime.create(
            python=firing_python, sandbox="auto", prewarm=False
        )
        try:
            await rt.eval(f"__fire__:{NR['ptrace']}")
        except BaseException as exc:  # noqa: BLE001
            return exc
        finally:
            await rt.close()
        raise AssertionError("the worker survived ptrace")

    exc = asyncio.run(go())
    assert classify_error(exc).kind == "sandbox_violation", str(exc)
