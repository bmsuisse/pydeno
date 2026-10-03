"""macOS: a sandboxed worker must not read another process's argv/environment.

`(deny default)` does not cover `process-info*`, so before the explicit deny in the profile a worker
could call sysctl(KERN_PROCARGS2) on its parent and read every environment variable the host had,
which undoes the worker's own `env={}`. This applies the real profile in a grandchild and asks.
"""

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.darwin_only

_CHILD = textwrap.dedent(
    """
    import ctypes, os, sys
    from pydeno import _sandbox
    assert _sandbox._apply_seatbelt()
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, os.getppid())  # CTL_KERN, KERN_PROCARGS2, parent
    buf = ctypes.create_string_buffer(1 << 20)
    size = ctypes.c_size_t(len(buf))
    rc = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0)
    leaked = b"SECRET_MARKER=hunter2" in buf.raw[: size.value]
    print(f"rc={rc} leaked={leaked}")
    """
)

_MIDDLE = textwrap.dedent(
    """
    import subprocess, sys
    child = {child!r}
    # The middle process is the worker's parent, and carries the marker like a host would.
    sys.stdout.write(subprocess.run([sys.executable, "-I", "-c", child], env={{}},
                                    capture_output=True, text=True).stdout)
    """
)


def test_worker_cannot_read_the_parents_environment() -> None:
    out = subprocess.run(
        [sys.executable, "-c", _MIDDLE.format(child=_CHILD)],
        env={"SECRET_MARKER": "hunter2", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "leaked=False" in out, out
    assert "rc=-1" in out, out
