"""Assume-breach regression tests for the Linux sandbox.

Threat model: a V8 escape gives the attacker arbitrary native code in the worker *after*
`pydeno._sandbox.apply()`. These tests play that attacker: from a freshly sandboxed process
they issue dangerous syscalls with junk arguments and require the answer to be `EPERM`, or, for
the calls no runtime ever makes (`NEVER_LEGITIMATE`), the death of the process by SIGSYS.

Why `EPERM` with junk arguments proves something: the seccomp filter decides at syscall
*entry*, before the kernel validates any argument. A syscall the filter denies therefore
answers `EPERM` whatever we pass (or kills, before anything happens); one it lets through
answers `EFAULT`/`EINVAL`/`EBADF`... or succeeds. (The full sweep in
`scripts/redteam_syscalls.py` enumerates all ~350 calls and was how most of this list was found.)

The list of syscalls that must be blocked is written here by *intent* ("these give host
access"), not derived from `_sandbox.py`, so deleting an entry from the filter cannot also
delete its test. Marked `redteam`: it fires real privileged syscalls, so it is deselected
unless `PYDENO_REDTEAM_CONTAINER=1` says we are inside a container (the matrix sets it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pydeno import _sandbox as _installed_sandbox

pytestmark = [pytest.mark.linux_only, pytest.mark.redteam]

# The module under test is the one the package ships, wherever it is installed: the test
# subprocesses load that very file, so they exercise exactly what the worker runs.
SANDBOX_PY = Path(_installed_sandbox.__file__)
TABLES = json.loads((Path(__file__).parent / "data" / "syscalls.json").read_text())

# What a compromised worker would reach for, grouped by what it would buy.
MUST_BLOCK = {
    # memory the worker's RSS never shows: 200 memfds of 1 MiB held ~208 MiB past max_memory
    "hidden memory": ["memfd_create"],
    "new processes / other processes": [
        "execve",
        "execveat",
        "fork",
        "vfork",
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
        "kcmp",
        "pidfd_open",
        "pidfd_getfd",
        "pidfd_send_signal",
        "process_madvise",
    ],
    "mounts and namespaces": [
        "mount",
        "umount2",
        "pivot_root",
        "chroot",
        "setns",
        "unshare",
        "open_tree",
        "open_tree_attr",
        "mount_setattr",
        "fsconfig",
        "fsopen",
        "fsmount",
        "fspick",
        "move_mount",
        "listmount",
        "statmount",
        "swapon",
        "swapoff",
    ],
    "the kernel": [
        "kexec_load",
        "kexec_file_load",
        "init_module",
        "finit_module",
        "delete_module",
        "bpf",
        "perf_event_open",
        "userfaultfd",
        "keyctl",
        "add_key",
        "request_key",
        "open_by_handle_at",
        "name_to_handle_at",
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
        "reboot",
        "acct",
        "syslog",
        "iopl",
        "ioperm",
        "quotactl",
        "quotactl_fd",
        "lookup_dcookie",
        "nfsservctl",
        "lsm_set_self_attr",
        "personality",
    ],
    "the network": ["socket", "connect", "bind", "listen", "accept", "accept4"],
    "IPC with the host user's other processes": [
        "msgget",
        "msgsnd",
        "msgrcv",
        "msgctl",
        "semget",
        "semop",
        "semctl",
        "semtimedop",
        "shmget",
        "shmat",
        "shmctl",
        "shmdt",
        "mq_open",
        "mq_unlink",
        "mq_timedsend",
        "mq_timedreceive",
        "mq_notify",
        "mq_getsetattr",
        "inotify_init",
        "inotify_init1",
        "inotify_add_watch",
        "inotify_rm_watch",
        "fanotify_init",
        "fanotify_mark",
    ],
    "file metadata (Landlock does not govern it)": [
        "chmod",
        "fchmod",
        "fchmodat",
        "fchmodat2",
        "chown",
        "fchown",
        "lchown",
        "fchownat",
        "utime",
        "utimes",
        "futimesat",
        "utimensat",
        "setxattr",
        "lsetxattr",
        "fsetxattr",
        "removexattr",
        "lremovexattr",
        "fremovexattr",
        "setxattrat",
        "removexattrat",
        "getxattr",
        "lgetxattr",
        "listxattr",
        "llistxattr",
        "getxattrat",
        "listxattrat",
    ],
    "identity and privilege": [
        "setuid",
        "setgid",
        "setreuid",
        "setregid",
        "setgroups",
        "setresuid",
        "setresgid",
        "setfsuid",
        "setfsgid",
        "capset",
    ],
    "the host's clock, name and disks": [
        "settimeofday",
        "clock_settime",
        "adjtimex",
        "clock_adjtime",
        "sethostname",
        "setdomainname",
        "sync",
        "syncfs",
        "tkill",
    ],
}
ALL_BLOCKED = sorted({name for group in MUST_BLOCK.values() for name in group})
# Of those, the ones that must end the process (SIGSYS) rather than fail: written by intent too.
NEVER_LEGITIMATE = {
    "memfd_create", "execve", "execveat", "ptrace", "process_vm_readv",
    "process_vm_writev", "kcmp", "pidfd_getfd", "mount", "umount2", "pivot_root", "chroot",
    "setns", "unshare", "open_tree", "open_tree_attr", "mount_setattr", "fsconfig", "fsopen",
    "fsmount", "fspick", "move_mount", "swapon", "swapoff", "kexec_load", "kexec_file_load",
    "init_module", "finit_module", "delete_module", "bpf", "perf_event_open", "userfaultfd",
    "keyctl", "add_key", "request_key", "open_by_handle_at", "name_to_handle_at",
    "io_uring_setup", "io_uring_enter", "io_uring_register", "reboot", "acct", "syslog", "iopl",
    "ioperm", "quotactl", "quotactl_fd", "lookup_dcookie", "nfsservctl", "lsm_set_self_attr",
    "settimeofday", "clock_settime", "adjtimex", "clock_adjtime", "sethostname", "setdomainname",
}  # fmt: skip
SIGSYS = 31
ARCH = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64"}.get(
    os.uname().machine, os.uname().machine
)
PATTERNS = ["0,0,0,0,0,0", "PTR,PTR,PTR,PTR,PTR,PTR", "1,1,1,1,1,1", "-100,PTR,0,0,0,0"]

CHILD = r"""
import ctypes, importlib.util, json, os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # a SIGSYS kill must not leave a core file
spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
libc = ctypes.CDLL(None, use_errno=True); libc.syscall.restype = ctypes.c_long
sink = ctypes.create_string_buffer(4096); ptr = ctypes.addressof(sink)
layers = sb.apply()
args = [int(a) if a != "PTR" else ptr for a in sys.argv[3].split(",")]
ctypes.set_errno(0)
ret = libc.syscall(int(sys.argv[2]), *[ctypes.c_long(a) for a in args])
sys.stdout.write(json.dumps({"layers": layers, "ret": ret, "errno": ctypes.get_errno()}) + "\n")
sys.stdout.flush()   # os._exit skips the flush; a lost report looks exactly like "reachable"
os._exit(0)
"""


def _fire(nr: int, pattern: str) -> dict:
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD, str(SANDBOX_PY), str(nr), pattern],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    where = {"nr": nr, "pattern": pattern}
    if done.returncode != 0:
        return {"died": done.returncode, "stderr": done.stderr[-200:], **where}
    lines = done.stdout.strip().splitlines()
    if not lines:
        # No report at all: the syscall did something to the child itself (closed its stdout,
        # exited it...). That is a finding, not a harness error, so say which one.
        return {"silent": True, "stderr": done.stderr[-200:], **where}
    return {**json.loads(lines[-1]), **where}


@pytest.fixture(scope="module")
def sweep() -> dict[str, list[dict]]:
    """Every must-block syscall x every argument pattern, fired once, in parallel."""
    by_name = {v: int(k) for k, v in TABLES[ARCH].items()}
    jobs = [(n, by_name[n], p) for n in ALL_BLOCKED if n in by_name for p in PATTERNS]
    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (name, _nr, _pat), res in zip(
            jobs, pool.map(lambda j: _fire(j[1], j[2]), jobs)
        ):
            results.setdefault(name, []).append(res)
    return results


def _cases() -> list[tuple[str, str]]:
    return [
        (group, name)
        for group, names in MUST_BLOCK.items()
        for name in names
        if name in {v for v in TABLES[ARCH].values()}
    ]


@pytest.mark.parametrize(("group", "name"), _cases())
def test_dangerous_syscall_is_denied_whatever_the_arguments(
    sweep: dict[str, list[dict]], group: str, name: str
) -> None:
    results = sweep[name]
    layers = {r.get("layers") for r in results if "layers" in r}
    if layers and "seccomp" not in next(iter(layers)):
        pytest.fail(
            f"seccomp was not applied here ({layers}); the matrix profile that hides it "
            f"must not run the red-team tests"
        )
    if name in NEVER_LEGITIMATE:
        # Killed at syscall entry, whatever the arguments: no answer to iterate on.
        bad = [r for r in results if r.get("died") != -SIGSYS]
        assert not bad, f"[{group}] {name} did not kill the sandboxed process: {bad}"
        return
    bad = [r for r in results if r.get("errno") != 1 or r.get("ret") != -1]
    assert not bad, f"[{group}] {name} was reachable from the sandbox: {bad}"


def test_the_sweep_actually_ran_every_listed_syscall(
    sweep: dict[str, list[dict]],
) -> None:
    """Guards the guard: a table typo would otherwise silently drop a name from the sweep."""
    present = set(TABLES[ARCH].values())
    expected = {n for n in ALL_BLOCKED if n in present}
    assert set(sweep) == expected
    # Names missing on this architecture are legitimate (fork/vfork/chmod... on arm64).
    missing = sorted(set(ALL_BLOCKED) - present)
    assert all(
        n
        in {
            "fork",
            "vfork",
            "inotify_init",
            "iopl",
            "ioperm",
            "chmod",
            "chown",
            "lchown",
            "utime",
            "utimes",
            "futimesat",
        }
        for n in missing
    ), missing


# --- a worker started as root must not stay root ------------------------------------------

DROP = r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
def caps():
    out = {}
    for line in open("/proc/self/status"):
        if line.split(":")[0] in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            out[line.split(":")[0]] = int(line.split(":")[1], 16)
    return out
before = caps()
report = sb.harden_process()
print(json.dumps({"report": report, "before": before, "after": caps(),
                  "uid": [os.getuid(), os.geteuid()], "gid": [os.getgid(), os.getegid()],
                  "groups": os.getgroups()}))
"""


@pytest.mark.as_root
def test_a_root_worker_becomes_nobody_with_no_capabilities() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-c", DROP, str(SANDBOX_PY)],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["uid"] == [65534, 65534], out
    assert out["gid"] == [65534, 65534], out
    assert out["groups"] == [], out
    # nothing left, and nothing can ever be regained
    for name in ("CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert out["after"][name] == 0, (name, out)


REGAIN = r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
sb.harden_process()
attempts = {}
for name, fn in (
    ("setuid_root", lambda: os.setuid(0)),
    ("seteuid_root", lambda: os.seteuid(0)),
    ("setresuid_root", lambda: os.setresuid(0, 0, 0)),
    ("setgid_root", lambda: os.setgid(0)),
):
    try:
        fn(); attempts[name] = True
    except OSError:
        attempts[name] = False
print(json.dumps({"regained": any(attempts.values()), "attempts": attempts, "uid": os.geteuid()}))
"""


@pytest.mark.as_root
def test_dropping_privileges_is_not_reversible() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-c", REGAIN, str(SANDBOX_PY)],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1])["regained"] is False


# --- calls that act on another process: allowed on ourselves, denied on anyone else ------

OTHERS = r"""
import ctypes, fcntl, importlib.util, json, os, resource, socket, sys
spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
# `uname` is denied once the sandbox is up, so ask for the architecture before it is
IDX = 0 if os.uname().machine == "x86_64" else 1
sb.apply()
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = "allowed"
    except OSError as e:
        out[name] = "EPERM" if e.errno == 1 else f"errno {e.errno}"
    except Exception as e:
        out[name] = type(e).__name__
me = os.getpid()
attempt("setpriority_other", lambda: os.setpriority(os.PRIO_PROCESS, 1, 10))
attempt("setpriority_self_zero", lambda: os.setpriority(os.PRIO_PROCESS, 0, 0))
attempt("setpriority_self_pid", lambda: os.setpriority(os.PRIO_PROCESS, me, 0))
attempt("affinity_other", lambda: os.sched_setaffinity(1, {0}))
attempt("affinity_self", lambda: os.sched_setaffinity(0, os.sched_getaffinity(0)))
attempt("prlimit_other", lambda: resource.prlimit(1, resource.RLIMIT_NOFILE))
attempt("prlimit_self", lambda: resource.prlimit(0, resource.RLIMIT_NOFILE))
attempt("getrlimit_self", lambda: resource.getrlimit(resource.RLIMIT_NOFILE))
attempt("scheduler_other", lambda: os.sched_setscheduler(1, os.SCHED_OTHER, os.sched_param(0)))
attempt("scheduler_self", lambda: os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0)))
attempt("kill_init", lambda: os.kill(1, 0))
attempt("kill_self", lambda: os.kill(me, 0))
attempt("kill_group_zero", lambda: os.killpg(0, 0))
a, b = socket.socketpair()
attempt("setpriority_user_zero", lambda: os.setpriority(os.PRIO_USER, 0, 0))
attempt("fcntl_setown_parent", lambda: fcntl.fcntl(a, fcntl.F_SETOWN, os.getppid()))
attempt("fcntl_setown_self", lambda: fcntl.fcntl(a, fcntl.F_SETOWN, me))
attempt("fcntl_getfl", lambda: fcntl.fcntl(a, fcntl.F_GETFL))
attempt("fcntl_setfl", lambda: fcntl.fcntl(a, fcntl.F_SETFL, os.O_NONBLOCK))
attempt("ioctl_fionread", lambda: fcntl.ioctl(a, 0x541B, b"\\0\\0\\0\\0"))
attempt("ioctl_fiosetown", lambda: fcntl.ioctl(a, 0x8901, os.getppid().to_bytes(4, "little")))
attempt("truncate_path", lambda: os.truncate("/nonexistent-pydeno-probe", 0))
# TIOCSTI on a socket would answer ENOTTY if it reached the kernel; EPERM means the filter did it.
attempt("ioctl_tiocsti", lambda: fcntl.ioctl(a, 0x5412, b"x"))
attempt("ioctl_tioclinux", lambda: fcntl.ioctl(a, 0x541C, b"\\\\x0b"))
libc = ctypes.CDLL(None, use_errno=True)
def libc_call(fn, *args):
    if fn(*args) == -1:
        e = ctypes.get_errno(); raise OSError(e, os.strerror(e))
ppid = os.getppid()
attempt("getpgid_parent", lambda: os.getpgid(ppid))
attempt("getsid_parent", lambda: os.getsid(ppid))
attempt("getpgid_self", lambda: os.getpgid(0))
nr = sb._SELF_PID_ARG0["get_robust_list"][IDX]
head, size = ctypes.c_void_p(), ctypes.c_size_t()
libc.syscall.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
attempt("get_robust_list_parent", lambda: libc_call(libc.syscall, nr, ppid, ctypes.byref(head), ctypes.byref(size)))
attempt("prctl_sched_core", lambda: libc_call(libc.prctl, 62, 2, ppid, 0, 0))
attempt("prctl_set_name", lambda: libc_call(libc.prctl, 15, b"pydeno", 0, 0, 0))
attempt("ioctl_siocgifconf", lambda: fcntl.ioctl(a, 0x8912, b"\x00" * 16))
attempt("ioctl_siocgifhwaddr", lambda: fcntl.ioctl(a, 0x8927, b"\x00" * 40))
attempt("socketpair_datagram", lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM))
attempt("socketpair_stream", lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM))
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def others() -> dict[str, str]:
    done = subprocess.run(
        [sys.executable, "-I", "-c", OTHERS, str(SANDBOX_PY)],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "operation",
    [
        "setpriority_other",
        "affinity_other",
        "prlimit_other",
        "scheduler_other",
        "kill_init",
        "setpriority_user_zero",
        "fcntl_setown_parent",
        "fcntl_setown_self",
        "ioctl_fiosetown",
        "ioctl_tiocsti",
        "ioctl_tioclinux",
        "getpgid_parent",
        "getsid_parent",
        "get_robust_list_parent",
        "prctl_sched_core",
        "ioctl_siocgifconf",
        "ioctl_siocgifhwaddr",
        "socketpair_datagram",
    ],
)
def test_acting_on_another_process_is_denied(
    others: dict[str, str], operation: str
) -> None:
    assert others[operation] == "EPERM", others


@pytest.mark.parametrize(
    "operation",
    [
        "setpriority_self_zero",
        "setpriority_self_pid",
        "affinity_self",
        "prlimit_self",
        "getrlimit_self",
        "scheduler_self",
        "kill_self",
        "fcntl_getfl",
        "fcntl_setfl",
        "ioctl_fionread",
        "getpgid_self",
        "prctl_set_name",
        "socketpair_stream",
    ],
)
def test_acting_on_itself_stays_allowed(others: dict[str, str], operation: str) -> None:
    """The runtime does these to itself; denying them would break it in quiet ways."""
    assert others[operation] == "allowed", others


def test_signalling_its_own_process_group_is_confined_to_itself(
    others: dict[str, str],
) -> None:
    # The worker is its own session leader, so group 0 is only itself.
    assert others["kill_group_zero"] in ("allowed", "EPERM")


def test_path_truncate_is_not_available(others: dict[str, str]) -> None:
    """`truncate(2)` by path is denied outright (not even ENOENT leaks through)."""
    assert others["truncate_path"] == "EPERM", others


def test_the_only_reachable_syscalls_are_the_kernel_seccomp_passthrough() -> None:
    """Of the full sweep's REACHABLE list, what the filter does not allow (issue #135).

    `uprobe` and `uretprobe` are not in the filter's allow-list, yet the kernel lets them bypass
    seccomp (Linux 6.11+/6.12+), so they answer `ENXIO` / `SIGILL`. Pin that this set is exactly
    those two: a new kernel pass-through, or a filter regression, then fails here and gets
    reviewed instead of scrolling past in the script's output.
    """
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "redteam_syscalls.py"
    spec = importlib.util.spec_from_file_location("redteam_syscalls_script", script)
    assert spec is not None and spec.loader is not None
    sweep_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep_script)

    results = sweep_script.sweep(ARCH, sorted(int(n) for n in TABLES[ARCH]))
    assert results, "the sweep returned nothing"
    reachable = sweep_script.unexpectedly_reachable(
        results, sweep_script.allowed_names(ARCH)
    )
    expected = set(sweep_script.KERNEL_SECCOMP_PASSTHROUGH)
    assert expected == {"uprobe", "uretprobe"}
    assert reachable <= expected, (
        f"new reachable syscalls: {sorted(reachable - expected)}"
    )
    # Only "nothing else is reachable" is pinned. That the pass-through itself is reachable
    # depends on the kernel, the architecture (`uprobe` is x86-64 only) and the container's own
    # seccomp profile, so asserting it fails on aarch64 and behind a container filter.
