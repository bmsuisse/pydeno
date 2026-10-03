"""The seccomp filter hard-codes syscall numbers per architecture. A wrong number blocks
the wrong call (or none), and the x86_64 half cannot run on every developer's machine, so
check every number against tables taken from the kernel source itself:

- `arch/x86/entry/syscalls/syscall_64.tbl`
- `include/uapi/asm-generic/unistd.h` (the table arm64 uses)

(`tests/data/syscalls.json`, generated from those two files; nothing here is derived from
`pydeno/_sandbox.py`, so a typo in one cannot hide behind the other.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pydeno import _sandbox

TABLES = json.loads((Path(__file__).parent / "data" / "syscalls.json").read_text())
ARCHES = ("x86_64", "aarch64")


def _entries() -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    for name, (x86, arm) in _sandbox._SYSCALLS.items():  # noqa: SLF001
        for arch, nr in (("x86_64", x86), ("aarch64", arm)):
            if nr is not None:
                rows.append((arch, name, nr))
    for table in (_sandbox._SIGNALS,):  # noqa: SLF001
        for name, (x86, arm) in table.items():
            rows.append(("x86_64", name, x86))
            rows.append(("aarch64", name, arm))
    for name, (x86, arm) in (
        ("clone", _sandbox._CLONE),  # noqa: SLF001
        ("clone3", _sandbox._CLONE3),  # noqa: SLF001
    ):
        rows.append(("x86_64", name, x86))
        rows.append(("aarch64", name, arm))
    return rows


# Denied before any released kernel has them (they are in kernel `master`, not yet in a tag
# that `scripts/gen_syscall_tables.py` can pin). Denying a number that does not exist yet is
# harmless and means a new chroot-style call is closed the day it ships. Remove an entry from
# here once a pinned tag contains it.
NOT_YET_IN_A_RELEASE = {"fchroot"}


@pytest.mark.parametrize(("arch", "name", "nr"), _entries())
def test_every_number_in_the_filter_is_the_syscall_it_claims_to_be(
    arch: str, name: str, nr: int
) -> None:
    if name in NOT_YET_IN_A_RELEASE:
        assert str(nr) not in TABLES[arch], (
            f"{name} is now released: drop the exception"
        )
        return
    assert TABLES[arch].get(str(nr)) == name, (
        f"{name} is {nr} in the filter for {arch}, but the kernel table says "
        f"{nr} is {TABLES[arch].get(str(nr))!r}"
    )


@pytest.mark.parametrize("arch", ARCHES)
def test_the_tables_cover_what_the_filter_and_loader_depend_on(arch: str) -> None:
    names = set(TABLES[arch].values())
    # the three Landlock calls, seccomp itself, and what asyncio's self-pipe needs
    assert {
        "landlock_create_ruleset",
        "landlock_add_rule",
        "landlock_restrict_self",
        "seccomp",
        "socketpair",
        "futex",
        "mmap",
    } <= names


@pytest.mark.parametrize("arch", ARCHES)
def test_the_syscalls_the_worker_needs_are_not_in_the_deny_list(arch: str) -> None:
    """Denying one of these would break the worker in ways that only show at runtime."""
    idx = 0 if arch == "x86_64" else 1
    needed = {
        "read",
        "write",
        "pread64",
        "futex",
        "mmap",
        "munmap",
        "mprotect",
        "madvise",
        "brk",
        "getrandom",
        "epoll_wait",
        "epoll_pwait",
        "epoll_ctl",
        "eventfd2",
        "pipe2",
        "socketpair",
        "sendto",
        "recvfrom",
        "sendmsg",
        "recvmsg",
        "clock_gettime",
        "nanosleep",
        "clock_nanosleep",
        "sched_yield",
        "sigaltstack",
        "rt_sigaction",
        "rt_sigprocmask",
        "getpid",
        "gettid",
        "set_robust_list",
        "rseq",
        "exit",
        "exit_group",
        "close",
        "dup",
        "fcntl",
        "ioctl",
        "prctl",
        "prlimit64",
        "getrlimit",
        "sched_getaffinity",
        "openat",
        "fstat",
        "newfstatat",
        "lseek",
        "readlinkat",
    }
    denied_numbers = {
        pair[idx]
        for pair in _sandbox._SYSCALLS.values()
        if pair[idx] is not None  # noqa: SLF001
    }
    by_name = {v: int(k) for k, v in TABLES[arch].items()}
    clash = sorted(n for n in needed if by_name.get(n) in denied_numbers)
    assert not clash, f"the deny list blocks syscalls the worker needs: {clash}"


@pytest.mark.parametrize("arch", ARCHES)
def test_the_deny_list_has_no_duplicate_numbers(arch: str) -> None:
    idx = 0 if arch == "x86_64" else 1
    numbers = [
        pair[idx]
        for pair in _sandbox._SYSCALLS.values()
        if pair[idx] is not None  # noqa: SLF001
    ]
    assert len(numbers) == len(set(numbers))


@pytest.mark.parametrize("arch", ARCHES)
def test_the_program_assembles_and_stays_within_bpf_jump_limits(arch: str) -> None:
    program = _sandbox._seccomp_program(arch)  # noqa: SLF001
    assert program is not None, "a jump offset overflowed 255 instructions"
    assert len(program) % 8 == 0
    assert len(program) // 8 <= 4096  # BPF_MAXINSNS
