"""The sandbox checks itself instead of trusting the kernel's "0".

* seccomp: for `sandbox_status()` the throwaway child that tests the filter also makes one
  never-legitimate call and must die of SIGSYS; a worker start asks the kernel whether the kill
  action is supported instead (every kill is audited). A kill that is missing or not enforced is
  reported by `attest()`, so `sandbox="require"` refuses it.
* Landlock: after `landlock_restrict_self`, a directory that could be opened a moment earlier must
  now be refused with EACCES. A kernel (or a stack in front of it) that accepts the ruleset and does
  not enforce it is therefore not counted as "landlock", and `sandbox="require"` stays honest.

Linux only; the ones that need both layers in force are `full_sandbox` (deselected in the matrix
profiles that hide a layer).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = [pytest.mark.linux_only]


def _run(code: str, lines: int = 1) -> str:
    done = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    return "\n".join(done.stdout.strip().splitlines()[-lines:])


@pytest.mark.full_sandbox
def test_the_kill_action_is_verified_in_a_throwaway_child() -> None:
    out = _run(
        """
        from pydeno import _sandbox
        print(_sandbox._seccomp_is_safe_here(verify_kill=True), _sandbox.SECCOMP_KILL)
        print(_sandbox._seccomp_is_safe_here(), _sandbox.SECCOMP_KILL)
        """,
        lines=2,
    )
    # exercised for sandbox_status(); only asked of the kernel at a worker start (no audit record)
    assert out == "True verified\nTrue available"


@pytest.mark.full_sandbox
def test_a_filter_that_does_not_kill_is_a_breach() -> None:
    """Simulates a kernel that installed the filter but turned the kill into something weaker:
    the self-test must say so, which is what makes the worker refuse to start."""
    out = _run(
        """
        from pydeno import _sandbox
        _sandbox.harden_process()
        applied = _sandbox.apply()
        assert "seccomp" in applied, applied
        _sandbox.SECCOMP_KILL = "not-killed"
        print(",".join(_sandbox.attest()))
        """
    )
    assert "exec-not-killed" in out.split(","), out


@pytest.mark.full_sandbox
def test_a_sandboxed_process_passes_its_self_test_without_making_the_killing_call() -> (
    None
):
    # `attest()` must not fire execve under the filter (that would kill the worker it checks).
    out = _run(
        """
        import json
        from pydeno import _sandbox
        _sandbox.harden_process()
        print(json.dumps([_sandbox.apply(), _sandbox.attest()]))
        """
    )
    applied, breaches = json.loads(out)
    assert set(applied.split("+")) >= {"landlock", "seccomp"}, out
    assert breaches == [], out


@pytest.mark.full_sandbox
def test_landlock_records_its_abi_and_its_canary_passes() -> None:
    out = _run(
        """
        import json
        from pydeno import _sandbox
        ok = _sandbox._apply_landlock()
        print(json.dumps([ok, _sandbox.LANDLOCK_ABI, _sandbox.LANDLOCK_NOTE]))
        """
    )
    ok, abi, note = json.loads(out)
    assert ok is True and abi >= 1 and note == "", out


@pytest.mark.full_sandbox
def test_a_landlock_that_accepts_and_does_not_enforce_is_not_counted() -> None:
    """`landlock_restrict_self` is swapped for `sched_yield` (also returns 0, does nothing): the
    kernel "accepted" the ruleset, and only the canary can tell that nothing is enforced."""
    out = _run(
        """
        import json, os
        from pydeno import _sandbox
        _sandbox._LANDLOCK_RESTRICT = 24 if os.uname().machine == "x86_64" else 124
        layers = _sandbox.apply(empty_root=False)
        print(json.dumps([layers, _sandbox.LANDLOCK_NOTE, sorted(_sandbox.missing_layers(layers))]))
        """
    )
    layers, note, missing = json.loads(out)
    assert "landlock" not in layers.split("+"), out
    assert "not enforced" in note, out
    assert missing == ["landlock"], out


@pytest.mark.full_sandbox
def test_sandbox_status_reports_both_canaries() -> None:
    from pydeno import sandbox_status

    status = sandbox_status()
    assert status.complete, status.explain()
    assert "canary" in status.landlock.detail, status.landlock.detail
    assert "ABI" in status.landlock.detail, status.landlock.detail
    assert "allow-list" in status.seccomp.detail, status.seccomp.detail
    assert "child was killed" in status.seccomp.detail, status.seccomp.detail


# --- kernel-enforced caps -------------------------------------------------------------------


def _limit_of(pid: int, label: str) -> str:
    with open(f"/proc/{pid}/limits") as fh:
        for line in fh:
            if line.startswith(label):
                return line[len(label) :].split()[0]
    raise AssertionError(f"{label} not in /proc/{pid}/limits")


def test_a_worker_with_max_memory_gets_a_kernel_data_cap() -> None:
    from pydeno import IsolatedRuntime, _sandbox

    mib = 1 << 20
    with IsolatedRuntime(max_memory=300 * mib, prewarm=False) as rt:
        pid = rt._proc.pid  # noqa: SLF001
        assert rt.eval("1 + 1") == 2
        assert (
            int(_limit_of(pid, "Max data size")) >= 300 * mib + _sandbox.DATA_HEADROOM
        )
        # and the long-standing ones are still there
        assert _limit_of(pid, "Max core file size") == "0"
        assert _limit_of(pid, "Max open files") == "256"


def test_the_data_cap_counts_from_what_the_interpreter_already_reserved() -> None:
    """RLIMIT_DATA counts private writable *reservations*, not resident pages. A free-threaded
    CPython reserves 1 GiB before any script runs, so a fixed `max_memory + headroom` ceiling would
    sit below the worker's own baseline and V8 could not start (found on 3.14t: a JIT worker died
    with SIGTRAP, a 900 MiB buffer failed). The ceiling is therefore the baseline at the moment it
    is set, plus `max_memory`, plus the headroom."""
    out = _run(
        """
        import mmap, resource
        from pydeno import _sandbox

        def vm_data():
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmData:"):
                        return int(line.split()[1]) * 1024

        mib = 1 << 20
        keep = mmap.mmap(-1, 2048 * mib, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)  # a big private writable reservation, untouched
        base = vm_data()
        limit = _sandbox.limit_data(256 * mib)
        assert limit is not None
        assert limit >= base + 256 * mib + _sandbox.DATA_HEADROOM - 16 * mib, (limit, base)
        extra = mmap.mmap(-1, 512 * mib, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)  # still fits: the cap is not under the baseline
        print("ok")
        """
    )
    assert out == "ok"


def test_no_max_memory_means_no_data_cap() -> None:
    from pydeno import IsolatedRuntime

    with IsolatedRuntime(max_memory=None, prewarm=False) as rt:
        assert _limit_of(rt._proc.pid, "Max data size") == "unlimited"  # noqa: SLF001


@pytest.mark.full_sandbox
def test_a_thread_bomb_meets_a_kernel_ceiling_where_the_worker_has_its_own_namespace() -> (
    None
):
    """RLIMIT_NPROC counts per user, so it caps one worker only inside the worker's own user
    namespace (the empty-root layer) on Linux 5.14+, where the kernel counts per namespace.
    There the 129th task is refused; elsewhere the limit is left alone and the supervisor's
    sampled cap is what stops a bomb."""
    out = _run(
        """
        import json, resource, threading
        from pydeno import _sandbox
        before = resource.getrlimit(resource.RLIMIT_NPROC)
        _sandbox.harden_process()
        _sandbox.apply()
        threading.stack_size(512 * 1024)
        stop = threading.Event()
        started = 0
        try:
            for _ in range(400):
                threading.Thread(target=stop.wait, daemon=True).start()
                started += 1
        except RuntimeError:
            pass
        stop.set()
        print(json.dumps({"caps": _sandbox.KERNEL_CAPS, "extras": _sandbox.EXTRAS,
                          "started": started, "before": before,
                          "after": resource.getrlimit(resource.RLIMIT_NPROC)}))
        """
    )
    got = json.loads(out)
    if "tasklimit" in got["caps"]:
        assert "emptyroot" in got["extras"], got
        assert got["after"] == [128, 128], got
        # the main thread counts too, and the kernel refuses at the limit
        assert 100 < got["started"] < 128, got
    else:
        assert got["after"] == got["before"], got
        assert got["started"] == 400, got


def test_a_buffer_past_the_kernel_ceiling_is_a_catchable_error() -> None:
    """With a buffer cap raised out of the way, a single allocation past `max_memory` + 1 GiB is
    refused by the kernel (RLIMIT_DATA) before it exists: the guest gets a `RangeError` and the
    worker lives on, instead of the allocation succeeding and the sampled ceiling killing it."""
    from pydeno import IsolatedRuntime, JavaScriptError, RuntimeConfig

    mib = 1 << 20
    with IsolatedRuntime(
        RuntimeConfig(max_buffer_bytes=8192 * mib), max_memory=300 * mib, prewarm=False
    ) as rt:
        with pytest.raises(JavaScriptError, match="RangeError|Array buffer"):
            rt.eval("new Uint8Array(1500 * 1024 * 1024).length")
        assert rt.eval("1 + 1") == 2
