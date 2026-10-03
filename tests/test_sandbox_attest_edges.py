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
