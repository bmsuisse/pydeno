"""The sandbox checks itself instead of trusting the kernel's "0".

* seccomp: the throwaway child that tests the filter also makes one never-legitimate call and must
  die of SIGSYS. A filter that installs but does not kill is reported by `attest()`, so
  `sandbox="require"` refuses it.
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


def _run(code: str) -> str:
    done = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1]


@pytest.mark.full_sandbox
def test_the_kill_action_is_verified_in_a_throwaway_child() -> None:
    out = _run(
        """
        from pydeno import _sandbox
        print(_sandbox._seccomp_is_safe_here(), _sandbox.SECCOMP_KILL_VERIFIED)
        """
    )
    assert out == "True True"


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
        _sandbox.SECCOMP_KILL_VERIFIED = False
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
        from pydeno import _sandbox
        _sandbox.harden_process()
        print(_sandbox.apply(), "|", ",".join(_sandbox.attest()))
        """
    )
    applied, _, breaches = out.partition(" | ")
    assert set(applied.split("+")) >= {"landlock", "seccomp"}, out
    assert breaches == "", out


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
            int(_limit_of(pid, "Max data size")) == 300 * mib + _sandbox.DATA_HEADROOM
        )
        # and the long-standing ones are still there
        assert _limit_of(pid, "Max core file size") == "0"
        assert _limit_of(pid, "Max open files") == "256"


def test_no_max_memory_means_no_data_cap() -> None:
    from pydeno import IsolatedRuntime

    with IsolatedRuntime(max_memory=None, prewarm=False) as rt:
        assert _limit_of(rt._proc.pid, "Max data size") == "unlimited"  # noqa: SLF001
