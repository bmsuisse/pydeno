"""The macOS profile may read exactly two sysctl names, and must.

`hw.pagesize_compat`: CPython 3.10-3.12 abort without it. `kern.osrelease`: V8 on x86_64 calls `uname()`
while it starts, which reads this name; without it the worker aborts at start-up on Intel Macs. Anything
else a sandboxed worker could read from the kernel (host name, hardware, CPU, process tables) stays denied.
"""

import re

from pydeno import _sandbox


def test_the_only_sysctl_allowances_are_the_page_size_and_the_kernel_release() -> None:
    allows = re.findall(r"\(allow sysctl[^\n]*", _sandbox._SEATBELT_PROFILE)  # noqa: SLF001
    assert allows == [
        '(allow sysctl-read (sysctl-name "hw.pagesize_compat"))',
        '(allow sysctl-read (sysctl-name "kern.osrelease"))',
    ]
