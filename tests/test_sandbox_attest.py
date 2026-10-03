"""The startup self-test: `_sandbox.attest()` asks the kernel whether the confinement holds.

Two directions, both needed. A process with no sandbox must be reported as breached (otherwise the
probes prove nothing), and a worker that applied its layers must report none (otherwise every
worker would refuse to start, which `test_isolated_*` would show anyway).
"""

import subprocess
import sys
import textwrap

import pytest

from pydeno import IsolatedRuntime


def test_an_unsandboxed_process_is_reported_as_breached() -> None:
    code = textwrap.dedent(
        """
        from pydeno import _sandbox
        print(",".join(_sandbox.attest()))
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    breaches = set(out.split(","))
    # The two that are true on every platform for a plain process.
    assert {"read-file", "write-file", "spawn-process", "network-socket"} <= breaches, (
        out
    )
    if sys.platform == "darwin":
        # The macOS-only probes must also notice, or a green self-test would prove nothing.
        assert {
            "read-parent-argv-environ",
            "read-hardware-uuid",
            "inspect-other-process",
        } <= breaches, out


@pytest.mark.full_sandbox  # `require` must fail by design where a layer is deliberately missing
def test_a_sandboxed_worker_starts_so_it_passed_its_own_self_test() -> None:
    # `_init` raises if attest() finds anything, and that surfaces here as a failed start.
    with IsolatedRuntime(sandbox="require") as rt:
        assert rt.eval("1 + 1") == 2
