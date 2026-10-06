"""Opt-in fork-from-template worker start (issue #72, `pydeno/_template.py`).

Linux only. The template is a single-threaded Python process that has done the worker's imports;
a worker is a fork of it that then applies the same OS sandbox and runs the same self-test as a
freshly launched one. These tests pin the contract: off by default, honest in `sandbox_status()`,
no weaker sandbox, rotation, orphan handling, and exit codes that still reach the supervisor.
"""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pydeno import (
    AsyncIsolatedRuntime,
    IsolatedRuntime,
    WorkerCrashed,
    disable_fork_template,
    enable_fork_template,
    sandbox_status,
)
from pydeno import _template

pytestmark = pytest.mark.linux_only


@pytest.fixture(autouse=True)
def _clean_template():
    yield
    disable_fork_template()
    _template.MANAGER.shutdown()


def _ppid(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[1])


def _threads(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Threads:"):
            return int(line.split()[1])
    raise AssertionError("no Threads line")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _template.MANAGER.reap_retired()  # a retired template is a zombie until it is waited for
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_off_by_default():
    assert not _template.fork_template_enabled()
    assert sandbox_status().worker_start == "exec"
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        assert rt.worker_start == "exec"


def test_enabled_worker_is_a_fork_of_a_template():
    enable_fork_template()
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        assert rt.worker_start == "fork-template"
        assert rt.eval("6 * 7") == 42
        template_pid = _ppid(rt._proc.pid)  # noqa: SLF001
        assert template_pid not in (os.getpid(), rt._proc.pid)  # noqa: SLF001
        assert _ppid(template_pid) == os.getpid()


def test_status_says_so():
    enable_fork_template(max_forks=7, max_age_seconds=30)
    status = sandbox_status()
    assert status.worker_start == "fork-template"
    assert status.to_dict()["worker_start"] == "fork-template"
    text = " ".join(status.warnings)
    assert "template" in text and "address-space" in text and "7 workers" in text


def test_template_is_single_threaded_and_holds_no_isolate():
    # Single-threaded is what lets the child's `unshare(CLONE_NEWUSER)` work; no V8 in the
    # template means each worker's isolate, its seed and its heap are made after the fork.
    enable_fork_template()
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        template_pid = _ppid(rt._proc.pid)  # noqa: SLF001
        assert _threads(template_pid) == 1
        rollup = Path(f"/proc/{template_pid}/smaps_rollup").read_text().splitlines()
        rss_kb = sum(int(line.split()[1]) for line in rollup if line.startswith("Rss:"))
        # a V8 isolate and its heap would add tens of MiB
        assert rss_kb < 150_000


@pytest.mark.full_sandbox
def test_sandbox_is_not_weaker_under_require():
    enable_fork_template()
    with IsolatedRuntime(sandbox="require", prewarm=False) as rt:
        assert rt.worker_start == "fork-template"
        forked = rt.sandbox
        assert rt.eval("typeof SharedArrayBuffer") == "undefined"
    disable_fork_template()
    with IsolatedRuntime(sandbox="require", prewarm=False) as plain:
        assert plain.worker_start == "exec"
        assert plain.sandbox == forked


def test_workers_are_single_use_and_isolated():
    enable_fork_template()
    with (
        IsolatedRuntime(sandbox="auto", prewarm=False) as a,
        IsolatedRuntime(sandbox="auto", prewarm=False) as b,
    ):
        a.eval("globalThis.secret = 1")
        assert b.eval("typeof globalThis.secret") == "undefined"
        assert a._proc.pid != b._proc.pid  # noqa: SLF001
        pid = a._proc.pid  # noqa: SLF001
    assert _wait_gone(pid)
    with IsolatedRuntime(sandbox="auto", prewarm=False) as c:
        assert c._proc.pid != pid  # noqa: SLF001
        assert c.eval("typeof globalThis.secret") == "undefined"


def test_a_killed_worker_reports_its_signal():
    enable_fork_template()
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        proc = rt._proc  # noqa: SLF001
        assert proc.poll() is None
        os.kill(proc.pid, signal.SIGKILL)
        assert proc.wait(timeout=5) == -signal.SIGKILL
        assert proc.poll() == -signal.SIGKILL
        with pytest.raises(WorkerCrashed):
            rt.eval("1 + 1")


def test_wait_times_out_while_the_worker_lives():
    enable_fork_template()
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        with pytest.raises(subprocess.TimeoutExpired):
            rt._proc.wait(timeout=0.05)  # noqa: SLF001


def test_template_rotates_and_retired_template_outlives_its_workers_only():
    enable_fork_template(max_forks=2)
    first = IsolatedRuntime(sandbox="auto", prewarm=False)
    second = IsolatedRuntime(sandbox="auto", prewarm=False)
    third = IsolatedRuntime(sandbox="auto", prewarm=False)
    try:
        t1, t2, t3 = (_ppid(r._proc.pid) for r in (first, second, third))  # noqa: SLF001
        assert t1 == t2
        assert t3 != t1
        # the retired template keeps serving the workers it made
        assert first.eval("1") == 1
        assert second.eval("2") == 2
        assert third.eval("3") == 3
        first.close()
        second.close()
        assert _wait_gone(t1), "a retired template must leave once its last worker has"
        assert third.eval("4") == 4
    finally:
        for rt in (first, second, third):
            rt.close()


def test_template_rotates_by_age():
    enable_fork_template(max_age_seconds=0.2)
    with IsolatedRuntime(sandbox="auto", prewarm=False) as a:
        t1 = _ppid(a._proc.pid)  # noqa: SLF001
    time.sleep(0.3)
    with IsolatedRuntime(sandbox="auto", prewarm=False) as b:
        assert _ppid(b._proc.pid) != t1  # noqa: SLF001


def test_invalid_limits_are_refused():
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            enable_fork_template(max_forks=bad)  # type: ignore[arg-type]
    for bad in (0, -1.0, True):
        with pytest.raises(ValueError):
            enable_fork_template(max_age_seconds=bad)  # type: ignore[arg-type]
    assert not _template.fork_template_enabled()


def test_custom_python_is_never_forked():
    from pydeno import _isolated

    enable_fork_template()
    # A fork of the template would "start" this; the exec path is what refuses it.
    with pytest.raises(OSError):
        _isolated._start_worker("/nonexistent/python")  # noqa: SLF001


def test_async_runtime_uses_the_template():
    enable_fork_template()

    async def main():
        async with AsyncIsolatedRuntime(sandbox="auto", prewarm=False) as rt:
            assert await rt.eval("20 + 22") == 42
            return _ppid(rt._proc.pid)  # noqa: SLF001

    template_pid = asyncio.run(main())
    assert template_pid != os.getpid()


def test_parent_death_takes_template_and_workers_with_it(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(
        textwrap.dedent(
            """
            import time
            from pydeno import IsolatedRuntime, enable_fork_template
            enable_fork_template()
            rt = IsolatedRuntime(sandbox="auto", prewarm=False)
            rt.eval("1")
            stat = open(f"/proc/{rt._proc.pid}/stat").read()
            template = int(stat.rsplit(")", 1)[1].split()[1])
            print(rt._proc.pid, template, flush=True)
            time.sleep(60)
            """
        )
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    try:
        worker, template = (int(x) for x in proc.stdout.readline().split())
        assert _alive(worker) and _alive(template)
        proc.kill()
        proc.wait()
        assert _wait_gone(worker), "orphaned worker survived its parent"
        assert _wait_gone(template), "orphaned template survived its parent"
    finally:
        proc.kill()


def test_forked_child_of_the_host_does_not_inherit_the_template():
    enable_fork_template()
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        rt.eval("1")
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.write(w, b"0" if _template.MANAGER.enabled else b"1")
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        assert os.read(r, 1) == b"1"
        os.close(r)
        os.close(w)
        assert rt.eval("2") == 2


def test_environment_switch():
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pydeno import _template; print(_template.fork_template_enabled())",
        ],
        env={**os.environ, "PYDENO_FORK_TEMPLATE": "1"},
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip().endswith("True"), out.stderr


def test_no_descriptor_or_process_leaks_across_lifecycles():
    enable_fork_template(max_forks=10)

    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    with IsolatedRuntime(sandbox="auto", prewarm=False) as warm:
        warm.eval("1")
    before = fds()
    pids = []
    for _ in range(25):  # crosses two rotations
        with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
            assert rt.eval("1 + 1") == 2
            pids.append(rt._proc.pid)  # noqa: SLF001
    assert all(_wait_gone(pid) for pid in pids)
    _template.MANAGER.reap_retired()
    # the current template's socket, plus at most the one retired template still draining
    assert fds() - before <= 2
