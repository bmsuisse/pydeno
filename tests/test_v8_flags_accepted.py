"""V8 stops parsing at the first flag it does not know and ignores the rest, silently.

A new V8 that drops a flag the engine layer passes (as 15.2 dropped `--no-validate-asm`) would
turn off every flag after it, among them the one that installs `queueMicrotask`. Start-up must
say nothing about unrecognized flags.
"""

from __future__ import annotations

import subprocess
import sys


def test_v8_accepts_every_flag_the_engine_passes_at_startup() -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "import pydeno; pydeno.Runtime().eval('1')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "unrecognized flag" not in done.stderr, done.stderr
    assert "remaining arguments were ignored" not in done.stderr, done.stderr
