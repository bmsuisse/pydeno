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
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from pydeno import (
    AsyncIsolatedRuntime,
    IsolatedRuntime,
    RuntimeConfig,
    WorkerCrashed,
    disable_fork_template,
    enable_fork_template,
    sandbox_status,
)
from pydeno import _isolated, _template, classify_error

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
    # The child drops the parent's template (its socket is the parent's), keeps the mode on and
    # starts a template of its own when it needs one: a preforking server does not silently get
    # exec mode.
    enable_fork_template()
    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
        rt.eval("1")
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                inherited = _template.MANAGER._current is not None  # noqa: SLF001
                with IsolatedRuntime(sandbox="auto", prewarm=False) as own:
                    ok = own.worker_start == "fork-template" and own.eval("2") == 2
                os.write(w, b"1" if (inherited or not ok) else b"0")
            except BaseException:  # noqa: BLE001
                os.write(w, b"1")
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        assert os.read(r, 1) == b"0"
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


# ---------------------------------------------------------------------------
# review findings (independent review of PR #124)
# ---------------------------------------------------------------------------

# A stand-in worker for the template's own protocol: it runs in the forked child in place of
# `_worker.main`, so a test can make "the worker" die of any signal or exit with any code, without
# a sandbox, and read what the host is told.
_STUB = """import os, resource, signal, sys, time
import pydeno._worker as _w
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
def _stub_main():
    sys.stdout.write("ready\\n"); sys.stdout.flush()
    parts = sys.stdin.readline().split()
    if parts[0] == "exit":
        os._exit(int(parts[1]))
    if parts[0] == "signal":
        os.kill(os.getpid(), int(parts[1])); time.sleep(30)
    time.sleep(1000)
_w.main = _stub_main
"""


@pytest.fixture
def stub_template(monkeypatch):
    monkeypatch.setattr(
        _isolated, "_WORKER_BOOT_PREFIX", _isolated._WORKER_BOOT_PREFIX + _STUB
    )  # noqa: SLF001


def _stub_worker():
    """A forked stub worker, waiting for a command line (`exit N`, `signal N`, `sleep`)."""
    err = tempfile.TemporaryFile()
    proc = _template.MANAGER.spawn(err.fileno())
    assert proc.stdout.readline() == b"ready\n"
    return proc, err


def _tell(proc, command: str) -> None:
    proc.stdin.write(command.encode() + b"\n")


def _state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[0]


def _gone(pid: int, seconds: float = 5.0) -> bool:
    """Dead or a zombie nobody has reaped yet (an orphan whose init is slow)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _state(pid) in (None, "Z", "X"):
            return True
        time.sleep(0.02)
    return False


def _children(pid: int) -> set[int]:
    out = set()
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if _ppid(int(entry)) == pid and _state(int(entry)) != "Z":
                    out.add(int(entry))
            except (OSError, IndexError, ValueError):
                pass
    return out


def test_descriptors_above_1024_do_not_break_the_template():
    # `select()` cannot watch a descriptor >= 1024, and the control socket keeps the number it has
    # in the host. A host with many files open must still get a fork-started worker.
    script = textwrap.dedent(
        """
        import os, resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        held = [os.open("/dev/null", os.O_RDONLY) for _ in range(1100)]
        from pydeno import IsolatedRuntime, enable_fork_template
        enable_fork_template()
        with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
            assert rt.eval("6 * 7") == 42
            print(rt.worker_start, max(held) >= 1024)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert out.stdout.split() == ["fork-template", "True"], out.stderr


def test_a_failing_template_falls_back_to_exec_for_any_exception(monkeypatch):
    enable_fork_template()
    before = _template.MANAGER.fallbacks
    for exc in (
        ValueError("filedescriptor out of range in select()"),
        RuntimeError("x"),
    ):

        def boom(_fd, exc=exc):
            raise exc

        monkeypatch.setattr(_template.MANAGER, "spawn", boom)
        with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
            assert rt.worker_start == "exec"
            assert rt.eval("1") == 1
    assert _template.MANAGER.fallbacks == before + 2
    assert any(
        "RuntimeError" in w or "fell back" in w for w in sandbox_status().warnings
    )

    def interrupted(_fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(_template.MANAGER, "spawn", interrupted)
    with pytest.raises(KeyboardInterrupt):  # never swallowed into a fallback
        _isolated._start_worker(sys.executable)  # noqa: SLF001


@pytest.mark.parametrize(
    "command, expected",
    [
        (f"signal {signal.SIGSEGV}", -signal.SIGSEGV),
        (f"signal {signal.SIGSYS}", -signal.SIGSYS),  # what a seccomp kill looks like
        (
            f"exit {_isolated._sandbox.MEMORY_EXIT_CODE}",
            _isolated._sandbox.MEMORY_EXIT_CODE,
        ),
        ("exit 3", 3),
    ],
)
def test_last_worker_of_a_retired_template_keeps_its_real_status(
    stub_template, command, expected
):
    enable_fork_template(max_forks=1)
    first, _e1 = _stub_worker()
    second, _e2 = _stub_worker()  # rotation: the first template is retired
    t1 = first._template  # noqa: SLF001
    assert t1 is not second._template  # noqa: SLF001
    _tell(first, command)
    # Other workers' polls reap retired templates: the one that has just lost its last worker
    # must not be closed before its exit frame has been read.
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        second.poll()
        time.sleep(0.005)
    assert first.wait(timeout=10) == expected
    assert first.poll() == expected
    _tell(second, "exit 0")
    assert second.wait(timeout=10) == 0


@pytest.mark.full_sandbox
def test_a_seccomp_style_kill_of_a_forked_worker_is_a_sandbox_violation():
    # End to end: a real worker, forked, its template retired, killed by SIGSYS as the seccomp
    # filter does; the host must say sandbox violation, not "killed by SIGKILL".
    enable_fork_template(max_forks=1)
    victim = IsolatedRuntime(sandbox="require", prewarm=False)
    other = IsolatedRuntime(sandbox="require", prewarm=False)
    try:
        assert victim.worker_start == "fork-template"
        assert victim.eval("1") == 1
        os.kill(victim._proc.pid, signal.SIGSYS)  # noqa: SLF001
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            other._proc.poll()  # noqa: SLF001
            time.sleep(0.005)
        with pytest.raises(WorkerCrashed) as caught:
            victim.eval("1")
        info = classify_error(caught.value)
        assert info.kind == "sandbox_violation", str(caught.value)
        assert victim._proc.returncode == -signal.SIGSYS  # noqa: SLF001
    finally:
        victim.close()
        other.close()


def test_memory_limit_exit_is_relayed_in_fork_mode():
    enable_fork_template(max_forks=1)
    rt = IsolatedRuntime(
        RuntimeConfig(max_buffer_bytes=8192 * 1024 * 1024),
        sandbox="auto",
        prewarm=False,
        max_memory=300 * 1024 * 1024,
    )
    other = IsolatedRuntime(sandbox="auto", prewarm=False)  # retires rt's template
    try:
        assert rt.worker_start == "fork-template"
        with pytest.raises(WorkerCrashed, match="max_memory"):
            rt.eval("new Uint8Array(800 * 1024 * 1024).fill(1).length")
    finally:
        rt.close()
        other.close()


def test_exit_status_is_kept_until_acked_and_pid_is_held(stub_template):
    # The template reports the status but keeps the zombie until the host acks it, so the pid
    # cannot be recycled while the host may still signal it.
    enable_fork_template()
    proc, _err = _stub_worker()
    _tell(proc, "exit 5")
    template = proc._template  # noqa: SLF001
    assert template.exit_code(proc.pid, 5.0) == 5  # a peek: nothing is acked yet
    assert _state(proc.pid) == "Z"  # still the template's zombie, the pid is still ours
    assert proc.poll() == 5  # the host has it now: acked
    assert _gone(proc.pid), "the template must reap a worker once the host acked it"
    assert proc.wait() == 5
    proc.kill()  # after the ack: must not signal a pid that may be someone else's


def test_template_death_still_lets_close_kill_a_stopped_worker():
    enable_fork_template()
    rt = IsolatedRuntime(sandbox="auto", prewarm=False)
    pid = rt._proc.pid  # noqa: SLF001
    template_pid = _ppid(pid)
    try:
        os.kill(pid, signal.SIGSTOP)
        os.kill(template_pid, signal.SIGKILL)
        end = time.monotonic() + 5
        while rt._proc.poll() is None and time.monotonic() < end:  # noqa: SLF001
            time.sleep(0.01)
        assert rt._proc.poll() == -signal.SIGKILL  # noqa: SLF001
    finally:
        rt.close()
    assert _gone(pid), "a worker whose template died must not be left running"


def test_exit_at_interpreter_end_kills_a_stopped_worker(tmp_path):
    # atexit order: the template's shutdown must not run before the hooks that kill workers, and
    # must itself kill what is left. A stopped worker cannot leave on its own.
    script = tmp_path / "host.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, signal
            from pydeno import IsolatedRuntime, enable_fork_template
            enable_fork_template()
            rt = IsolatedRuntime(sandbox="auto", prewarm=False)
            rt.eval("1")
            os.kill(rt._proc.pid, signal.SIGSTOP)
            print(rt._proc.pid, flush=True)
            """
        )
    )
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    pid = int(out.stdout.split()[0])
    assert _gone(pid, 10), f"the worker survived the host's exit: {out.stderr}"


def test_unanswered_fork_request_does_not_desynchronise_the_next(monkeypatch):
    monkeypatch.setattr(_template, "_SPAWN_REPLY_SECONDS", 0.5)
    enable_fork_template()
    keep = IsolatedRuntime(sandbox="auto", prewarm=False)
    template_pid = _ppid(keep._proc.pid)  # noqa: SLF001
    known = {keep._proc.pid}  # noqa: SLF001
    try:
        os.kill(template_pid, signal.SIGSTOP)
        try:
            # the template cannot answer: this start falls back to exec, it does not raise
            with IsolatedRuntime(sandbox="auto", prewarm=False) as slow:
                assert slow.worker_start == "exec"
                assert slow.eval("1") == 1
                known.add(slow._proc.pid)  # noqa: SLF001
        finally:
            os.kill(template_pid, signal.SIGCONT)
        # The template now answers the stale request: that worker belongs to nobody and dies,
        # and the next request is not handed its pid.
        end = time.monotonic() + 10
        while _children(template_pid) - known and time.monotonic() < end:
            time.sleep(0.02)
        assert not _children(template_pid) - known, "the late worker was left running"
        with IsolatedRuntime(sandbox="auto", prewarm=False) as nxt:
            assert nxt.worker_start == "fork-template"
            assert nxt._proc.pid not in known  # noqa: SLF001
            assert nxt.eval("2") == 2
            assert nxt._proc.poll() is None  # noqa: SLF001
    finally:
        keep.close()


def test_a_wait_does_not_block_other_polls_or_spawns():
    enable_fork_template()
    a = IsolatedRuntime(sandbox="auto", prewarm=False)
    b = IsolatedRuntime(sandbox="auto", prewarm=False)
    waiter = threading.Thread(
        target=lambda: pytest.raises(
            subprocess.TimeoutExpired,
            a._proc.wait,
            2.0,  # noqa: SLF001
        ),
        daemon=True,
    )
    try:
        waiter.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        for _ in range(20):
            assert b._proc.poll() is None  # noqa: SLF001
        assert time.monotonic() - t0 < 0.5
        t0 = time.monotonic()
        with IsolatedRuntime(sandbox="auto", prewarm=False) as c:
            assert c.eval("1") == 1
        assert time.monotonic() - t0 < 1.5, "a spawn waited for an unrelated wait()"
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            b._proc.wait(timeout=0.3)  # noqa: SLF001
        assert time.monotonic() - t0 < 1.0
        waiter.join(5)
    finally:
        a.close()
        b.close()


def test_concurrent_spawns_get_their_own_workers():
    enable_fork_template(max_forks=5)  # crosses rotations
    results: list[tuple[int, int]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def one(n: int) -> None:
        try:
            with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
                value = rt.eval(f"{n} * 2")
                with lock:
                    results.append((n, value))
                    assert rt.worker_start == "fork-template"
                pid = rt._proc.pid  # noqa: SLF001
                assert rt._proc.poll() is None  # noqa: SLF001
                with lock:
                    results.append((-n - 1, pid))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    assert sorted(v for n, v in results if n >= 0) == [n * 2 for n in range(12)]
    pids = [v for n, v in results if n < 0]
    assert len(pids) == len(set(pids)) == 12


def test_host_fork_while_a_spawn_is_in_flight(tmp_path):
    script = tmp_path / "host.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys, threading, warnings
            warnings.simplefilter("ignore")
            from pydeno import IsolatedRuntime, enable_fork_template
            enable_fork_template(max_forks=3)
            stop = threading.Event()
            def spawner():
                while not stop.is_set():
                    with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
                        assert rt.eval("1") == 1
            t = threading.Thread(target=spawner)
            t.start()
            bad = 0
            for _ in range(6):
                pid = os.fork()
                if pid == 0:
                    code = 1
                    try:
                        import signal
                        signal.alarm(90)
                        with IsolatedRuntime(sandbox="auto", prewarm=False) as rt:
                            if rt.eval("2") == 2 and rt.worker_start == "fork-template":
                                code = 0
                    finally:
                        os._exit(code)
                _, status = os.waitpid(pid, 0)
                bad += status != 0
            stop.set()
            t.join(60)
            print("bad", bad, "alive", t.is_alive())
            """
        )
    )
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=240
    )
    assert out.stdout.split() == ["bad", "0", "alive", "False"], out.stderr


# What a worker really got, read from the kernel and from the handshake, not from the string
# `rt.sandbox` alone.
_STATUS_KEYS = (
    "NoNewPrivs",
    "Seccomp",
    "Seccomp_filters",
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
)


def _worker_view(rt) -> dict:
    pid = rt._proc.pid  # noqa: SLF001
    status = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in _STATUS_KEYS:
            status[key] = value.strip()
    limits = {}
    for line in Path(f"/proc/{pid}/limits").read_text().splitlines()[1:]:
        fields = line.split()
        limits[" ".join(fields[:-3])] = tuple(fields[-3:-1])
    # `RLIMIT_DATA` is derived from the process's own footprint when the limit is set, so it
    # differs by about a megabyte between two processes; everything else must be identical.
    data = limits.pop("Max data size")
    try:
        mounts = len(Path(f"/proc/{pid}/mountinfo").read_text().splitlines())
    except OSError:
        mounts = -1
    return {
        "sandbox": rt.sandbox,
        "extras": sorted(rt.sandbox_extras),
        "degraded": rt.sandbox_degraded,
        "status": status,
        "limits": limits,
        "data_limit": data[0],
        "mounts": mounts,
        "v8_flags": list(rt.v8_flags),
        "typeof": rt.eval(
            "[typeof SharedArrayBuffer, typeof WebAssembly, typeof Deno, typeof process].join()"
        ),
    }


@pytest.mark.full_sandbox
def test_sandbox_equality_with_exec_mode_beyond_the_string():
    kwargs = dict(sandbox="require", prewarm=False, max_memory=512 * 1024 * 1024)
    with IsolatedRuntime(**kwargs) as plain:
        assert plain.worker_start == "exec"
        exec_view = _worker_view(plain)
    enable_fork_template()
    with IsolatedRuntime(**kwargs) as forked:
        assert forked.worker_start == "fork-template"
        fork_view = _worker_view(forked)
    fork_data = fork_view.pop("data_limit")
    exec_data = exec_view.pop("data_limit")
    if fork_data.isdigit() and exec_data.isdigit():
        assert abs(int(fork_data) - int(exec_data)) < 16 * 1024 * 1024
    else:
        assert fork_data == exec_data
    assert fork_view == exec_view
