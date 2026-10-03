#!/usr/bin/env python3
"""Regenerate tests/data/syscalls.json from the Linux kernel's own syscall tables.

The seccomp filter (`pydeno/_sandbox.py`) names syscalls by number. Those numbers are checked
against this file by `tests/test_sandbox_syscall_tables.py`, which is only worth anything if the
file comes from somewhere independent of the filter: the kernel sources.

    python scripts/gen_syscall_tables.py            # the pinned tag below
    python scripts/gen_syscall_tables.py v6.17      # a newer kernel

After bumping the tag, run the tests: a syscall added since is reported as unreviewed (everything
at or beyond `_FIRST_UNREVIEWED` in `_sandbox.py` answers ENOSYS until someone decides about it).
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_TAG = "v7.0"
RAW = "https://raw.githubusercontent.com/torvalds/linux/{tag}/{path}"
X86_TABLE = "arch/x86/entry/syscalls/syscall_64.tbl"
GENERIC_UNISTD = (
    "include/uapi/asm-generic/unistd.h"  # aarch64 (and riscv) use the generic table
)
DEST = Path(__file__).resolve().parent.parent / "tests" / "data" / "syscalls.json"


def fetch(tag: str, path: str) -> str:
    with urllib.request.urlopen(RAW.format(tag=tag, path=path), timeout=60) as response:  # noqa: S310
        return response.read().decode()


def x86_64(table: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in table.splitlines():
        parts = line.split()
        if (
            len(parts) >= 3
            and not line.lstrip().startswith("#")
            and parts[1] in ("common", "64")
        ):
            out[int(parts[0])] = parts[2].removeprefix("sys_")
    return out


def generic(header: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in re.finditer(r"#define\s+__NR_(\w+)\s+(\d+)\s*$", header, re.M):
        if (
            m.group(1) != "syscalls"
        ):  # `__NR_syscalls` is the table's size, not a syscall
            out[int(m.group(2))] = m.group(1)
    for m in re.finditer(r"#define\s+__NR3264_(\w+)\s+(\d+)\s*$", header, re.M):
        out.setdefault(int(m.group(2)), m.group(1))
    return out


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TAG
    tables = {
        "x86_64": x86_64(fetch(tag, X86_TABLE)),
        "aarch64": generic(fetch(tag, GENERIC_UNISTD)),
    }
    DEST.write_text(
        json.dumps(
            {a: {str(k): v for k, v in sorted(t.items())} for a, t in tables.items()},
            indent=0,
        )
    )
    print(
        f"{DEST} from linux {tag}: "
        + ", ".join(f"{a} {len(t)} syscalls" for a, t in tables.items())
    )


if __name__ == "__main__":
    main()
