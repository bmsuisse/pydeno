"""Edge cases of the startup self-test that a first version got wrong (found in review).

* A parent with a large environment: a too-small buffer made an *allowed* `KERN_PROCARGS2` read
  fail with ENOMEM, which was mistaken for a refusal, so the one capability the probe exists to
  find went unreported.
* A probe whose sensitive call succeeds but whose cleanup then fails must still be a breach.
"""

import subprocess
import sys
import textwrap

import pytest

_CHILD = textwrap.dedent(
    """
    from pydeno import _sandbox
    print(",".join(_sandbox.attest()))
    """
)

_MIDDLE = textwrap.dedent(
    """
    import subprocess, sys
    done = subprocess.run([sys.executable, "-I", "-c", {child!r}], env={{}},
                          capture_output=True, text=True)
    sys.stdout.write(done.stdout)
    """
)


@pytest.mark.darwin_only
def test_a_parent_with_a_huge_environment_is_still_seen_as_readable() -> None:
    # An unsandboxed grandchild reading a parent whose environment is far past 4 KiB.
    env = {"PATH": "/usr/bin:/bin", "FILLER": "x" * 200_000}
    out = subprocess.run(
        [sys.executable, "-c", _MIDDLE.format(child=_CHILD)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    assert "read-parent-argv-environ" in out.split(","), out


def test_a_probe_that_succeeds_then_fails_its_cleanup_is_still_a_breach() -> None:
    from pydeno import _sandbox

    # Make the file the write probe creates impossible to remove afterwards: it cannot be
    # unlinked from a directory we then lose, so simulate by breaking `os.unlink` for the probe.
    import os

    real_unlink = os.unlink

    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("cleanup refused")

    os.unlink = broken  # type: ignore[assignment]
    try:
        breaches = _sandbox.attest()
    finally:
        os.unlink = real_unlink
        import glob

        for leftover in glob.glob("/tmp/.pydeno-attest-*"):
            real_unlink(leftover)
    # An unsandboxed test process can write, so the write probe succeeded; the failed cleanup
    # must not turn that into "refused".
    assert "write-file" in breaches


@pytest.mark.linux_only
def test_threads_are_counted_the_way_the_kernel_counts_them() -> None:
    import threading

    from pydeno import _sandbox

    before = _sandbox._thread_count_here()  # noqa: SLF001
    stop = threading.Event()
    t = threading.Thread(target=stop.wait)
    t.start()
    try:
        assert _sandbox._thread_count_here() == before + 1  # noqa: SLF001
    finally:
        stop.set()
        t.join()


@pytest.mark.linux_only
@pytest.mark.as_root
def test_a_root_worker_that_cannot_become_nobody_still_loses_its_capabilities() -> None:
    code = textwrap.dedent(
        """
        import os
        from pydeno import _sandbox
        def refuse(*a, **k):
            raise PermissionError("no")
        os.setuid = refuse
        report = _sandbox.drop_privileges()
        caps = [l for l in open("/proc/self/status") if l.startswith("CapEff")][0].split()[1]
        print(report.get("uid_after"), int(caps, 16))
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    ).stdout.split()
    assert out[0] == "0", out  # still root...
    assert out[1] == "0", out  # ...but holding nothing


@pytest.mark.linux_only
def test_the_read_implies_exec_personality_is_cleared() -> None:
    """With READ_IMPLIES_EXEC set the kernel adds PROT_EXEC after seccomp has judged the mapping."""
    code = textwrap.dedent(
        """
        import ctypes
        from pydeno import _sandbox
        libc = ctypes.CDLL(None, use_errno=True)
        libc.personality(0x0400000)
        had = bool(libc.personality(0xFFFFFFFF) & 0x0400000)
        _sandbox._clear_read_implies_exec()
        print(had, bool(libc.personality(0xFFFFFFFF) & 0x0400000))
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    ).stdout.split()
    assert out == ["True", "False"], out


@pytest.mark.linux_only
def test_an_unconfined_process_is_reported_as_allowed_to_exec() -> None:
    code = textwrap.dedent(
        """
        from pydeno import _sandbox
        print(",".join(_sandbox.attest()))
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    assert "exec-allowed" in out.split(","), out


@pytest.mark.darwin_only
def test_a_confined_macos_worker_cannot_create_sysv_objects() -> None:
    from pydeno import IsolatedRuntime

    # `attest()` includes the SysV probe, so a worker that started under `require` passed it.
    with IsolatedRuntime(sandbox="require") as rt:
        assert rt.sandbox == "seatbelt"
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from pydeno import _sandbox; print(_sandbox._creates_sysv_semaphore())",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    assert out == "True"  # unconfined, it can: so the probe in attest() is meaningful
