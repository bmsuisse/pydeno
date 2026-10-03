"""The seccomp filter's *decision logic*, tested without a kernel.

`pydeno._sandbox._seccomp_program` assembles classic BPF. The kernel's verdict on a real
syscall is covered by `test_redteam_syscalls.py` (Linux, containers); this file checks what the
program *means* by running it in a few dozen lines of Python, over every syscall number, on both
architectures, on any platform, in milliseconds.

Classic BPF is tiny: a 32-bit accumulator, absolute loads from the `seccomp_data` struct,
conditional jumps, and returns. The interpreter below implements exactly the opcodes the
assembler emits and refuses anything else, so a new opcode in the assembler fails loudly here.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from pydeno import _sandbox as sb

TABLES = json.loads((Path(__file__).parent / "data" / "syscalls.json").read_text())

ALLOW = 0x7FFF0000
KILL_PROCESS = 0x80000000
ERRNO = 0x00050000
EPERM, ENOSYS = 1, 38
AUDIT = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
CLONE_THREAD = 0x10000
X32_BIT = 0x40000000
# setpriority's PRIO_PROCESS is 0; ioprio_set's IOPRIO_WHO_PROCESS is 1
WHICH = {"setpriority": 0, "ioprio_set": 1}
ME = os.getpid()


def _by_name(arch: str) -> dict[str, int]:
    return {v: int(k) for k, v in TABLES[arch].items()}


def _program(arch: str) -> list[tuple[int, int, int, int]]:
    raw = sb._seccomp_program(arch)  # noqa: SLF001
    assert raw is not None
    return [struct.unpack_from("<HBBI", raw, i * 8) for i in range(len(raw) // 8)]


def run(
    program: list[tuple[int, int, int, int]],
    arch: str,
    nr: int,
    args: tuple[int, ...] = (),
    audit_arch: int | None = None,
) -> int:
    """Interpret the filter for one syscall. Returns the verdict (a SECCOMP_RET_* value)."""
    padded = (tuple(args) + (0,) * 6)[:6]
    data = struct.pack(
        "<IIQ6Q", nr, AUDIT[arch] if audit_arch is None else audit_arch, 0, *padded
    )
    acc = 0
    pc = 0
    for _ in range(10_000):  # a classic BPF program cannot loop; this is only a guard
        code, jt, jf, k = program[pc]
        pc += 1
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            acc = struct.unpack_from("<I", data, k)[0]
        elif code == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += jt if acc == k else jf
        elif code == 0x35:  # BPF_JMP | BPF_JGE | BPF_K
            pc += jt if acc >= k else jf
        elif code == 0x45:  # BPF_JMP | BPF_JSET | BPF_K
            pc += jt if acc & k else jf
        elif code == 0x06:  # BPF_RET | BPF_K
            return k
        else:
            raise AssertionError(f"opcode {code:#x} is not one the interpreter knows")
    raise AssertionError("the program did not terminate")


@pytest.fixture(scope="module", params=["x86_64", "aarch64"])
def arch(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(scope="module")
def prog(arch: str) -> list[tuple[int, int, int, int]]:
    return _program(arch)


def _idx(arch: str) -> int:
    return 0 if arch == "x86_64" else 1


class TestTheDenyList:
    def test_every_denied_syscall_is_eperm_whatever_the_arguments(
        self, arch: str, prog: list
    ) -> None:
        nrs = {
            name: pair[_idx(arch)]
            for name, pair in sb._SYSCALLS.items()  # noqa: SLF001
            if pair[_idx(arch)] is not None
        }
        assert len(nrs) > 100
        for name, nr in nrs.items():
            for args in (
                (),
                (0,) * 6,
                (1, 1, 1, 1, 1, 1),
                (2**32 - 1, 2**64 - 1, 5, 6, 7, 8),
            ):
                assert run(prog, arch, nr, args) == ERRNO | EPERM, f"{name} with {args}"

    def test_denial_is_an_errno_not_a_kill(self, arch: str, prog: list) -> None:
        """A denied call must fail, not terminate: some libraries probe, and a kill would turn
        a harmless probe into an outage."""
        for pair in sb._SYSCALLS.values():  # noqa: SLF001
            nr = pair[_idx(arch)]
            if nr is not None:
                assert run(prog, arch, nr) != KILL_PROCESS


class TestWhatTheWorkerNeeds:
    NEEDED = [
        "read",
        "write",
        "close",
        "mmap",
        "munmap",
        "mprotect",
        "madvise",
        "brk",
        "futex",
        "epoll_ctl",
        "epoll_pwait",
        "eventfd2",
        "pipe2",
        "socketpair",
        "sendto",
        "recvfrom",
        "sendmsg",
        "recvmsg",
        "clock_gettime",
        "clock_nanosleep",
        "nanosleep",
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
        "dup",
        "fcntl",
        "ioctl",
        "prctl",
        "getrandom",
        "openat",
        "fstat",
        "newfstatat",
        "lseek",
        "readlinkat",
        "pread64",
        "uname",
        "getrlimit",
        "sched_getaffinity",
        "set_tid_address",
        "mremap",
        "getcwd",
        "statx",
        "faccessat",
        "fsync",
        "fdatasync",
        "ftruncate",
        "writev",
        "readv",
        "madvise",
        "memfd_create",
    ]

    def test_each_is_allowed(self, arch: str, prog: list) -> None:
        by = _by_name(arch)
        missing = []
        for name in self.NEEDED:
            if name not in by:
                continue  # not present on this architecture
            if run(prog, arch, by[name], (0, 0, 0, 0, 0, 0)) != ALLOW:
                missing.append(name)
        assert not missing, f"the filter blocks calls the worker relies on: {missing}"

    def test_a_large_part_of_the_table_stays_reachable(
        self, arch: str, prog: list
    ) -> None:
        allowed = sum(
            run(prog, arch, int(nr)) == ALLOW
            for nr in TABLES[arch]
            if int(nr) < sb._FIRST_UNREVIEWED  # noqa: SLF001
        )
        # a default-allow deny list: the runtime needs most of the kernel's plumbing
        assert allowed > 150


class TestClone:
    def test_threads_are_allowed(self, arch: str, prog: list) -> None:
        nr = sb._CLONE[_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (CLONE_THREAD | 0x100 | 0x200,)) == ALLOW

    @pytest.mark.parametrize(
        "flags",
        [0, 0x11, 0x00010000 >> 1, 0x20000000, 0x10000000, 0x7E000000 & ~CLONE_THREAD],
    )
    def test_anything_that_is_not_a_thread_is_denied(
        self, arch: str, prog: list, flags: int
    ) -> None:
        nr = sb._CLONE[_idx(arch)]  # noqa: SLF001
        assert flags & CLONE_THREAD == 0
        assert run(prog, arch, nr, (flags,)) == ERRNO | EPERM

    def test_clone3_falls_back_to_clone(self, arch: str, prog: list) -> None:
        """glibc retries with clone() when clone3 says ENOSYS, which is how threads still start."""
        nr = sb._CLONE3[_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (CLONE_THREAD,)) == ERRNO | ENOSYS


class TestSignals:
    @pytest.fixture(params=["kill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo"])
    def signal_nr(self, request: pytest.FixtureRequest, arch: str) -> int:
        return sb._SIGNALS[request.param][_idx(arch)]  # noqa: SLF001

    def test_to_itself_is_allowed(self, arch: str, prog: list, signal_nr: int) -> None:
        assert run(prog, arch, signal_nr, (ME, 0, 0)) == ALLOW

    @pytest.mark.parametrize("victim", [1, 2, 0, ME + 1, ME - 1, 2**31 - 1, 2**32 - 1])
    def test_to_anyone_else_is_denied(
        self, arch: str, prog: list, signal_nr: int, victim: int
    ) -> None:
        assert run(prog, arch, signal_nr, (victim, 9, 0)) == ERRNO | EPERM

    def test_the_parent_in_particular(
        self, arch: str, prog: list, signal_nr: int
    ) -> None:
        assert run(prog, arch, signal_nr, (os.getppid(), 9, 0)) == ERRNO | EPERM


class TestActingOnOtherProcesses:
    @pytest.mark.parametrize("name", list(sb._SELF_PID_ARG0))  # noqa: SLF001
    def test_pid_in_arg0_self_only(self, arch: str, prog: list, name: str) -> None:
        nr = sb._SELF_PID_ARG0[name][_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (0,)) == ALLOW  # 0 means the caller
        assert run(prog, arch, nr, (ME,)) == ALLOW
        assert run(prog, arch, nr, (1,)) == ERRNO | EPERM
        assert run(prog, arch, nr, (ME + 1,)) == ERRNO | EPERM
        assert run(prog, arch, nr, (os.getppid(),)) == ERRNO | EPERM

    @pytest.mark.parametrize("name", list(sb._SELF_PID_ARG1))  # noqa: SLF001
    def test_a_single_process_selector_with_ourselves_is_allowed(
        self, arch: str, prog: list, name: str
    ) -> None:
        nr = sb._SELF_PID_ARG1[name][_idx(arch)]  # noqa: SLF001
        which = WHICH[name]
        assert run(prog, arch, nr, (which, 0)) == ALLOW  # `who` 0 = the caller
        assert run(prog, arch, nr, (which, ME)) == ALLOW

    @pytest.mark.parametrize("name", list(sb._SELF_PID_ARG1))  # noqa: SLF001
    def test_someone_elses_pid_is_denied_even_with_the_right_selector(
        self, arch: str, prog: list, name: str
    ) -> None:
        nr = sb._SELF_PID_ARG1[name][_idx(arch)]  # noqa: SLF001
        which = WHICH[name]
        for victim in (1, ME + 1, os.getppid()):
            assert run(prog, arch, nr, (which, victim)) == ERRNO | EPERM

    @pytest.mark.parametrize("name", list(sb._SELF_PID_ARG1))  # noqa: SLF001
    def test_a_group_or_user_selector_is_denied_even_for_who_zero(
        self, arch: str, prog: list, name: str
    ) -> None:
        """`PRIO_USER` with who=0 means every process the user runs, the host included."""
        nr = sb._SELF_PID_ARG1[name][_idx(arch)]  # noqa: SLF001
        for which in (n for n in (0, 1, 2, 3, 4, 99, 2**31) if n != WHICH[name]):
            assert run(prog, arch, nr, (which, 0)) == ERRNO | EPERM, which
            assert run(prog, arch, nr, (which, ME)) == ERRNO | EPERM, which

    def test_arg0_of_an_arg1_call_is_not_the_pid(self, arch: str, prog: list) -> None:
        nr = sb._SELF_PID_ARG1["setpriority"][_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (ME, 1)) == ERRNO | EPERM


class TestSignalOwnership:
    """`fcntl(fd, F_SETOWN, parent)` + `F_SETSIG` + `O_ASYNC` makes the kernel signal the owner
    whenever the descriptor is ready, without ever calling `kill`."""

    @pytest.mark.parametrize("cmd", [8, 10, 15])  # F_SETOWN, F_SETSIG, F_SETOWN_EX
    def test_naming_a_signal_owner_is_denied(
        self, arch: str, prog: list, cmd: int
    ) -> None:
        nr = sb._FCNTL[_idx(arch)]  # noqa: SLF001
        for fd in (0, 3, 100):
            assert run(prog, arch, nr, (fd, cmd, os.getppid())) == ERRNO | EPERM
            # even ourselves: nothing here needs it
            assert run(prog, arch, nr, (fd, cmd, ME)) == ERRNO | EPERM

    @pytest.mark.parametrize(
        "cmd",
        [0, 1, 2, 3, 4, 5, 6, 7, 9, 11, 12, 13, 14, 1024, 1025, 1030, 1031, 1032, 1033],
    )
    def test_every_other_fcntl_command_is_untouched(
        self, arch: str, prog: list, cmd: int
    ) -> None:
        """F_GETFL/F_SETFL/F_DUPFD(_CLOEXEC)/F_GETFD/F_SETFD and friends are what asyncio and
        the C library use every day."""
        nr = sb._FCNTL[_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (3, cmd, 0)) == ALLOW

    @pytest.mark.parametrize(
        "cmd",
        [
            0x8901,  # FIOSETOWN
            0x8902,  # SIOCSPGRP
            0x5412,  # TIOCSTI: push a byte into a terminal's input queue (CVE-2017-5226)
            0x541C,  # TIOCLINUX: console-wide actions
        ],
    )
    def test_the_ioctl_spellings_of_the_same_thing_are_denied(
        self, arch: str, prog: list, cmd: int
    ) -> None:
        nr = sb._IOCTL[_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (3, cmd, os.getppid())) == ERRNO | EPERM

    @pytest.mark.parametrize(
        "cmd", [0x541B, 0x5421, 0x5452, 0x8903, 0x8904, 0x5401, 0x5413]
    )
    def test_ordinary_ioctls_are_untouched(
        self, arch: str, prog: list, cmd: int
    ) -> None:
        # FIONREAD, FIONBIO, FIOASYNC, SIOCGPGRP, FIOGETOWN, TCGETS, TIOCGWINSZ
        nr = sb._IOCTL[_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (3, cmd, 0)) == ALLOW

    def test_the_command_is_judged_on_its_low_32_bits_like_the_kernel_does(
        self, arch: str, prog: list
    ) -> None:
        """`cmd` is an `int`; the kernel ignores the upper half of the 64-bit register, so the
        filter must not be fooled by garbage there."""
        nr = sb._FCNTL[_idx(arch)]  # noqa: SLF001
        assert run(prog, arch, nr, (3, (0xDEADBEEF << 32) | 8, 1)) == ERRNO | EPERM


class TestTheFutureAndTheWrongAbi:
    def test_a_syscall_nobody_has_reviewed_is_enosys(
        self, arch: str, prog: list
    ) -> None:
        for nr in (sb._FIRST_UNREVIEWED, sb._FIRST_UNREVIEWED + 1, 500, 1000, 2**20):  # noqa: SLF001
            assert run(prog, arch, nr) == ERRNO | ENOSYS, nr

    def test_the_x32_abi_is_closed(self, arch: str, prog: list) -> None:
        """x32 syscalls reuse the x86_64 arch value but set bit 30 in the number."""
        for base in (0, 1, 59, 60, 231):
            assert run(prog, arch, X32_BIT | base) == ERRNO | ENOSYS

    def test_a_foreign_architecture_is_killed(self, arch: str, prog: list) -> None:
        """int 0x80 on x86_64 enters the 32-bit table, where every number means something else,
        so a filter keyed on x86_64 numbers cannot judge it."""
        for other in (
            0x40000003,
            0x40000028,
            0xC00000B7 if arch == "x86_64" else 0xC000003E,
            0,
        ):
            assert run(prog, arch, 1, audit_arch=other) == KILL_PROCESS

    def test_every_reviewed_number_gets_an_explicit_answer_never_a_kill(
        self, arch: str, prog: list
    ) -> None:
        verdicts = {run(prog, arch, nr) for nr in range(0, sb._FIRST_UNREVIEWED)}  # noqa: SLF001
        assert verdicts <= {ALLOW, ERRNO | EPERM, ERRNO | ENOSYS}

    def test_the_tables_stop_below_the_unreviewed_range(self, arch: str) -> None:
        real = [int(n) for n, name in TABLES[arch].items() if name != "syscalls"]
        assert max(real) < sb._FIRST_UNREVIEWED, (  # noqa: SLF001
            "the kernel tables contain a syscall newer than the filter's review: read what "
            "it does, decide, then raise _FIRST_UNREVIEWED"
        )


class TestShape:
    def test_the_program_fits_the_kernels_limits(self, arch: str, prog: list) -> None:
        assert 0 < len(prog) <= 4096
        assert all(0 <= jt < 256 and 0 <= jf < 256 for _c, jt, jf, _k in prog)

    def test_every_jump_stays_inside_the_program(self, arch: str, prog: list) -> None:
        for i, (code, jt, jf, _k) in enumerate(prog):
            if code in (0x15, 0x35, 0x45):
                assert i + 1 + jt < len(prog)
                assert i + 1 + jf < len(prog)

    def test_the_program_ends_in_returns_only(self, arch: str, prog: list) -> None:
        assert prog[-1][0] == 0x06

    def test_building_it_twice_gives_the_same_program(self, arch: str) -> None:
        assert sb._seccomp_program(arch) == sb._seccomp_program(arch)  # noqa: SLF001
