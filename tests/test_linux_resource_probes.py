"""Resource diagnostics must probe the same Linux identity as the worker."""

import os
import time
from pathlib import Path

import pytest

from pydeno import AsyncIsolatedRuntime, IsolatedRuntime, WorkerCrashed, sandbox_status
from pydeno import _aio, _sandbox, _status

pytestmark = [pytest.mark.linux_only, pytest.mark.as_root]


def _uid(pid):
    status = Path(f"/proc/{pid}/status").read_text()
    return int(
        next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1]
    )


def test_resource_probe_waits_for_worker_privilege_drop(monkeypatch):
    seen = []
    original = _sandbox.rss_bytes

    def rss(pid):
        seen.append(_uid(pid))
        return original(pid)

    monkeypatch.setattr(_sandbox, "rss_bytes", rss)
    result, note = _status._measure_resource_probes(5)
    assert note == ""
    assert all(value is not None for value in result.values())
    assert seen == [_sandbox._NOBODY]


@pytest.mark.parametrize("metric", ["rss_bytes", "cpu_seconds", "thread_count"])
def test_status_reports_unreadable_uid_dropped_worker(monkeypatch, metric):
    original = getattr(_sandbox, metric)

    def hidden(pid):
        # Model hidepid=2: the caller can read its own uid, but cannot see the dropped worker.
        return None if _uid(pid) == _sandbox._NOBODY else original(pid)

    monkeypatch.setattr(_sandbox, metric, hidden)
    status = sandbox_status()
    assert not status.resource_probes.applied
    assert not status.complete
    assert any("cannot be enforced" in warning for warning in status.warnings)


@pytest.mark.parametrize(
    "metric, label",
    [
        ("rss_bytes", "max_memory"),
        ("cpu_seconds", "CPU cap"),
        ("thread_count", "thread cap"),
    ],
)
def test_sync_missing_resource_counter_refuses_require_and_warns_auto(
    monkeypatch, metric, label
):
    monkeypatch.setattr(_sandbox, metric, lambda pid: None)
    with pytest.raises(WorkerCrashed, match=label):
        IsolatedRuntime(sandbox="require", prewarm=False)
    with pytest.warns(RuntimeWarning, match=label):
        with IsolatedRuntime(sandbox="auto", prewarm=False) as runtime:
            assert runtime.eval("1 + 1") == 2


@pytest.mark.parametrize(
    "index, label", [(0, "max_memory"), (1, "CPU cap"), (2, "thread cap")]
)
async def test_async_missing_resource_counter_refuses_require_and_warns_auto(
    monkeypatch, index, label
):
    sample = [1, 0.0, 1]
    sample[index] = None
    monkeypatch.setattr(
        _aio, "_sample_many", lambda pids: [tuple(sample) for _ in pids]
    )
    with pytest.raises(WorkerCrashed, match=label):
        await AsyncIsolatedRuntime.create(sandbox="require", prewarm=False)
    with pytest.warns(RuntimeWarning, match=label):
        async with AsyncIsolatedRuntime(sandbox="auto", prewarm=False) as runtime:
            assert await runtime.eval("1 + 1") == 2


@pytest.mark.parametrize("mode", ["exit", "hang"])
def test_failed_hardening_is_bounded_and_reaped(monkeypatch, mode):
    children = []
    real_fork = os.fork

    def fork():
        pid = real_fork()
        if pid:
            children.append(pid)
        return pid

    def fail():
        if mode == "exit":
            os._exit(9)
        time.sleep(60)

    monkeypatch.setattr(os, "fork", fork)
    monkeypatch.setattr(_sandbox, "harden_process", fail)
    start = time.monotonic()
    result, note = _status._measure_resource_probes(0.1)
    assert result == {} and "hardening" in note
    assert time.monotonic() - start < 3
    assert len(children) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0], os.WNOHANG)
