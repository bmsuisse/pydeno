"""Refuse workers the supervisor cannot signal, before accepting guest commands."""

import os
import signal

import pytest

from pydeno import AsyncIsolatedRuntime, IsolatedRuntime, WorkerCrashed, classify_error
from pydeno import _status


@pytest.mark.parametrize(
    "mode", ["off", "auto", pytest.param("require", marks=pytest.mark.full_sandbox)]
)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_missing_signal_authority_refuses_and_reaps(
    monkeypatch, mode, asynchronous
):
    original = os.kill
    workers = []

    def denied(pid, sig):
        if sig == 0 and pid != os.getpid():
            workers.append(pid)
            raise PermissionError("test: worker cannot be signalled")
        return original(pid, sig)

    monkeypatch.setattr(os, "kill", denied)
    runtime = None
    try:
        with pytest.raises(WorkerCrashed, match="termination authority") as caught:
            runtime = (
                await AsyncIsolatedRuntime.create(sandbox=mode, prewarm=False)
                if asynchronous
                else IsolatedRuntime(sandbox=mode, prewarm=False)
            )
        info = classify_error(caught.value)
        assert info.kind == "sandbox_unavailable"
        assert not info.retryable
        assert workers
        for pid in workers:
            with pytest.raises(ProcessLookupError):
                original(pid, 0)
    finally:
        if runtime is not None:
            if asynchronous:
                await runtime.close()
            else:
                runtime.close()


def test_status_exposes_missing_termination_authority(monkeypatch):
    original = os.kill

    def denied(pid, sig):
        if sig == signal.SIGKILL:
            raise PermissionError("test: hardened probe cannot be terminated")
        return original(pid, sig)

    monkeypatch.setattr(os, "kill", denied)
    result = _status.sandbox_status()
    assert not result.termination.applied
    assert not result.complete
    assert not result.to_dict()["termination"]["applied"]
    assert any("termination authority" in warning for warning in result.warnings)


@pytest.mark.linux_only
@pytest.mark.as_root
@pytest.mark.full_sandbox
def test_linux_without_kill_capability_refuses_before_guest_code():
    # Drop capabilities only in a disposable supervisor, never in the pytest process.
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent("""
        import asyncio
        import ctypes
        import os
        from pydeno import AsyncIsolatedRuntime, IsolatedRuntime, WorkerCrashed, classify_error, sandbox_status

        class Header(ctypes.Structure):
            _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
        class Data(ctypes.Structure):
            _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
        libc = ctypes.CDLL(None, use_errno=True)
        header, data = Header(0x20080522, 0), (Data * 2)()
        assert libc.capget(ctypes.byref(header), ctypes.byref(data)) == 0
        for name in ('effective', 'permitted', 'inheritable'):
            setattr(data[0], name, getattr(data[0], name) & ~(1 << 5))  # CAP_KILL
        assert libc.capset(ctypes.byref(header), ctypes.byref(data)) == 0
        status = sandbox_status()
        assert not status.termination.applied and not status.complete

        async def check():
            for mode in ('off', 'auto', 'require'):
                for asynchronous in (False, True):
                    runtime = None
                    try:
                        if asynchronous:
                            runtime = await AsyncIsolatedRuntime.create(sandbox=mode, prewarm=False)
                        else:
                            runtime = IsolatedRuntime(sandbox=mode, prewarm=False)
                    except WorkerCrashed as exc:
                        assert 'termination authority' in str(exc), str(exc)
                        assert classify_error(exc).kind == 'sandbox_unavailable'
                        assert not classify_error(exc).retryable
                    else:
                        if asynchronous:
                            await runtime.close()
                        else:
                            runtime.close()
                        raise AssertionError('startup accepted an unsupervisable worker')
            try:
                child = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                raise AssertionError(('worker was not reaped', child))
        asyncio.run(check())
    """)
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_disappeared_worker_is_not_a_permission_refusal(
    monkeypatch, asynchronous
):
    from types import SimpleNamespace

    def gone(pid, sig):
        raise ProcessLookupError("worker already exited")

    monkeypatch.setattr(os, "kill", gone)
    worker = SimpleNamespace(_proc=SimpleNamespace(pid=12345))
    with pytest.raises(ProcessLookupError):
        if asynchronous:
            await AsyncIsolatedRuntime._check_termination_authority(worker)
        else:
            IsolatedRuntime._check_termination_authority(worker)


def test_termination_refusal_does_not_recommend_auto():
    from pydeno._front import _start_failure

    error = _start_failure(
        WorkerCrashed("supervisor termination authority is unavailable"), "require"
    )
    assert "sandbox='auto'" not in str(error)


def test_failed_probe_hardening_reports_its_cause(monkeypatch):
    note = "resource probe child exited before finishing hardening"
    monkeypatch.setattr(
        _status, "_measure_resource_probes", lambda deadline: ({}, note)
    )
    result = _status.sandbox_status()
    assert result.termination.detail == note
    assert any(note in warning for warning in result.warnings)
    assert "startup refuses in all sandbox modes" in result.explain()
