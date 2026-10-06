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


# Every per-architecture table the filter is assembled from: the allow-list, the record of what is
# denied (and killed) on purpose, and the argument-checked calls.
_TABLES = {
    "allowed": _sandbox._ALLOWED,  # noqa: SLF001
    "denied": _sandbox._SYSCALLS,  # noqa: SLF001
    "signals": _sandbox._SIGNALS,  # noqa: SLF001
    "self_pid_arg0": _sandbox._SELF_PID_ARG0,  # noqa: SLF001
    "self_pid_arg1": _sandbox._SELF_PID_ARG1,  # noqa: SLF001
    "single": {
        "clone": _sandbox._CLONE,  # noqa: SLF001
        "clone3": _sandbox._CLONE3,  # noqa: SLF001
        "fcntl": _sandbox._FCNTL,  # noqa: SLF001
        "ioctl": _sandbox._IOCTL,  # noqa: SLF001
        "prctl": _sandbox._PRCTL,  # noqa: SLF001
        "socketpair": _sandbox._SOCKETPAIR,  # noqa: SLF001
        "mmap": _sandbox._EXEC_CHECKED[0],  # noqa: SLF001
        "mprotect": _sandbox._EXEC_CHECKED[1],  # noqa: SLF001
    },
}


def _entries() -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    for table in _TABLES.values():
        for name, (x86, arm) in table.items():
            for arch, nr in (("x86_64", x86), ("aarch64", arm)):
                if nr is not None:
                    rows.append((arch, name, nr))
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
def test_the_allow_list_never_reopens_a_deliberate_denial(arch: str) -> None:
    idx = 0 if arch == "x86_64" else 1
    denied = {p[idx] for p in _sandbox._SYSCALLS.values() if p[idx] is not None}  # noqa: SLF001
    allowed = {p[idx]: n for n, p in _sandbox._ALLOWED.items() if p[idx] is not None}  # noqa: SLF001
    assert not {allowed[nr] for nr in allowed.keys() & denied}


@pytest.mark.parametrize("arch", ARCHES)
def test_the_allow_list_and_the_argument_checked_calls_do_not_overlap(
    arch: str,
) -> None:
    """An argument-checked call must not also be allowed outright (the allow-list comes first in
    the program, so its checks would never run). mmap/mprotect are the exception: their PROT_EXEC
    check is placed before the allow-list."""
    idx = 0 if arch == "x86_64" else 1
    allowed = {p[idx] for p in _sandbox._ALLOWED.values() if p[idx] is not None}  # noqa: SLF001
    for key in ("signals", "self_pid_arg0", "self_pid_arg1", "single"):
        for name, pair in _TABLES[key].items():
            if name in ("mmap", "mprotect"):
                continue
            assert pair[idx] not in allowed, name


def test_the_kill_list_is_a_subset_of_the_deliberate_denials() -> None:
    """Each killed call has its reason recorded in `_SYSCALLS`."""
    assert _sandbox._KILL <= set(_sandbox._SYSCALLS)  # noqa: SLF001


@pytest.mark.parametrize("arch", ARCHES)
def test_the_allow_list_has_no_duplicate_numbers(arch: str) -> None:
    idx = 0 if arch == "x86_64" else 1
    numbers = [p[idx] for p in _sandbox._ALLOWED.values() if p[idx] is not None]  # noqa: SLF001
    assert len(numbers) == len(set(numbers))


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
        "uname",
        "openat",
        "fstat",
        "newfstatat",
        "lseek",
        "readlinkat",
        "fstatat",  # aarch64's newfstatat
    }
    denied_numbers = {
        pair[idx]
        for pair in _sandbox._SYSCALLS.values()
        if pair[idx] is not None  # noqa: SLF001
    }
    by_name = {v: int(k) for k, v in TABLES[arch].items()}
    clash = sorted(n for n in needed if by_name.get(n) in denied_numbers)
    assert not clash, f"the deny list blocks syscalls the worker needs: {clash}"
    # and each is on the allow-list or argument-checked, on every architecture that has it
    reachable = {
        p[idx]
        for t in _TABLES.values()
        if t is not _sandbox._SYSCALLS
        for p in t.values()
    }  # noqa: SLF001
    missing = sorted(n for n in needed if n in by_name and by_name[n] not in reachable)
    assert not missing, f"needed but on no list: {missing}"


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
