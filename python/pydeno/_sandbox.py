"""OS-level confinement for the `IsolatedRuntime` worker process.

A process boundary keeps a crash away from the host; this module is what stops a
guest that *escapes V8* from using the process. Monty states that it has no OS
layer (it relies on being a memory-safe interpreter with no I/O at all); pydeno
cannot make that argument about V8, so the worker applies one itself:

- **macOS**: a deny-by-default Seatbelt profile (no files, no network, no fork/exec,
  no mach services beyond logging).
- **Linux**: Landlock (no filesystem access, no TCP) plus a seccomp-bpf filter that is an
  allow-list (everything else is refused; calls no runtime ever makes, such as exec, ptrace,
  mount, bpf and io_uring, kill the process), with `no_new_privs`.

Both are one-way: once applied they cannot be lifted from inside the process. They
must be applied *before* the isolate exists so that every thread V8 and tokio spawn
inherits them, which means everything the worker will ever import has to be imported
first (see `_worker.py`).

`apply()` reports which layers took effect; it never raises for a missing mechanism,
so the caller decides whether "none" is acceptable (`sandbox="require"`).
"""

from __future__ import annotations

import ctypes
import errno
import os
import resource
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable

__all__ = ["apply", "harden_process", "rss_bytes", "rss_reader", "MEMORY_EXIT_CODE"]

# Distinct from a signal death or an ordinary failure, like Monty's `OOM_EXIT_CODE`,
# so the parent can say "memory limit" instead of "crashed".
MEMORY_EXIT_CODE = 78

# --------------------------------------------------------------------------- macOS

_SEATBELT_PROFILE = """
(version 1)
(deny default)
(allow sysctl-read (sysctl-name "hw.pagesize_compat"))
; V8 on x86_64 macOS calls uname() while it starts and aborts if it fails; uname() reads this one name.
; It is the Darwin kernel version (for example "24.6.0"): no host, user or hardware identity.
(allow sysctl-read (sysctl-name "kern.osrelease"))
(allow signal (target self))
(deny process-info*)
(allow process-info-pidinfo (target self))
(deny iokit-get-properties)
(deny darwin-notification-post)
(deny syscall-unix (syscall-number SYS_gethostuuid SYS_getfsstat SYS_getfsstat64 SYS_csops
  SYS_csops_audittoken SYS_getpriority SYS_getpgid SYS_getsid SYS_fstatfs SYS_fstatfs64 SYS_kill
  SYS_semget SYS_shmget SYS_msgget SYS_semsys SYS_shmsys SYS_msgsys))
(deny syscall-mig (kernel-mig-routine host_statistics_from_user host_statistics64_from_user
  host_processor_info))
(deny system-fcntl (fcntl-command F_GETPATH))
(deny process-codesigning*)
"""
# What each line is for. The profile is a deny-by-default base plus explicit denies for the things
# `(deny default)` does NOT cover; each was found by asking from inside the sandbox, not assumed:
#  * `process-info*`: KERN_PROCARGS2 on the parent returns its argv and *environment* (any
#    same-user process's, in fact), which undoes `env={}`. `(deny default)` alone does not stop it,
#    and neither does narrowing `sysctl-read` to a prefix, so the only `sysctl-read` allowance is the
#    one name below.
#  * `sysctl-read hw.pagesize_compat` (the page size, nothing else): V8's allocator asks for it when
#    the process has not already cached it. Python 3.13+ happens to, 3.10 to 3.12 do not, and
#    without this name the worker aborts at start ("LowLevelAlloc arithmetic overflow") on those.
#  * `iokit-get-properties`, `SYS_gethostuuid`: the machine's permanent hardware identifier.
#  * `SYS_getpriority`/`getpgid`/`getsid`/`getfsstat`/`fstatfs`, `host_statistics*`: they list every
#    host process, the mounted volumes and free disk space, and system-wide CPU and memory
#    counters (a side channel on what else the machine is doing).
#  * `F_GETPATH`: turns an open descriptor back into a path on the host's disk.
#  * SysV `semget`/`shmget`/`msgget`: objects that survive the worker (they cannot be removed from
#    inside) and come from a small system-wide table (32 shared-memory ids), so one worker could
#    use them all up until the next reboot. `(deny ipc-sysv*)` does not stop this; the syscalls must be denied.
# Known gap: `notify_post()` still reaches other processes (the connection to notifyd is opened
# before the profile is applied), and `kill(pid, 0)` still tells a running pid from an absent one.


# Compiling the SBPL text is almost all of what `sandbox_init` costs (about 8 ms; applying the
# compiled profile takes under 0.1 ms). `precompile_seatbelt()` does the compiling on a thread
# while the worker is still importing (ctypes releases the GIL for the call, so it really runs
# alongside), and `_apply_seatbelt` then only applies the result. The profile is the same text,
# compiled by the same library `sandbox_init` uses for it; the self-test (`attest`) checks the
# outcome either way, and any failure on this path falls back to `sandbox_init`.
_SANDBOX_LIB = "/usr/lib/libsandbox.1.dylib"
_precompiled: list[tuple[int, Callable[..., int], Callable[..., object]]] = []
_precompile_thread: threading.Thread | None = None
#: Which call put the Seatbelt profile in force: "precompiled", "sandbox_init", or "" (none yet).
SEATBELT_PATH = ""
#: Why the precompiled path was not used (library missing, the compiler's own error text, apply
#: refused, ...), or "" if it was. Parent-side diagnostics only: it goes into the worker's start-up
#: failure message, never to guest code.
PRECOMPILE_ERROR = ""


def _compile_seatbelt() -> None:
    global PRECOMPILE_ERROR  # noqa: PLW0603
    try:
        lib = ctypes.CDLL(_SANDBOX_LIB)
        compile_string = lib.sandbox_compile_string
        sandbox_apply = lib.sandbox_apply
        free_profile = lib.sandbox_free_profile
        free_error = lib.sandbox_free_error
    except (OSError, AttributeError) as exc:
        PRECOMPILE_ERROR = f"libsandbox unavailable: {exc}"
        return
    compile_string.restype = ctypes.c_void_p
    # The error is a malloc'd C string the caller frees, so it is taken as a raw pointer.
    compile_string.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    sandbox_apply.restype = ctypes.c_int
    sandbox_apply.argtypes = [ctypes.c_void_p]
    free_profile.restype = None
    free_profile.argtypes = [ctypes.c_void_p]
    free_error.restype = None
    free_error.argtypes = [ctypes.c_void_p]
    err = ctypes.c_void_p()
    profile = compile_string(_SEATBELT_PROFILE.encode(), None, ctypes.byref(err))
    if err.value:
        text = ctypes.string_at(err.value).decode("utf-8", "replace")
        free_error(err)
        if not profile:
            PRECOMPILE_ERROR = f"profile did not compile: {text}"
    if profile:
        _precompiled.append((profile, sandbox_apply, free_profile))
    elif not PRECOMPILE_ERROR:
        PRECOMPILE_ERROR = "profile did not compile (no error text)"


def precompile_seatbelt() -> None:
    """Start compiling the Seatbelt profile in the background (macOS; a no-op elsewhere).

    Only for a process that will call `apply()` soon: the worker calls it before its imports.
    """
    global _precompile_thread  # noqa: PLW0603
    if sys.platform != "darwin" or _precompile_thread is not None:
        return
    _precompile_thread = threading.Thread(
        target=_compile_seatbelt, name="pydeno-seatbelt-compile", daemon=True
    )
    _precompile_thread.start()


def _apply_precompiled_seatbelt() -> bool:
    """True only if the precompiled profile is now in force."""
    global PRECOMPILE_ERROR  # noqa: PLW0603
    thread = _precompile_thread
    if thread is None:
        return False
    thread.join(timeout=5.0)  # never seen to take more than ~10 ms
    if thread.is_alive():
        PRECOMPILE_ERROR = "compile still running after 5 s"
        return False
    if not _precompiled:
        return False  # PRECOMPILE_ERROR says why
    profile, sandbox_apply, free_profile = _precompiled.pop()
    try:
        rc = sandbox_apply(profile)
    finally:
        free_profile(profile)
    if rc != 0:
        PRECOMPILE_ERROR = f"sandbox_apply returned {rc}"
        return False
    return True


def seatbelt_note() -> str:
    """ " (precompile: ...)" when the precompiled path was not used, for failure messages."""
    return f" (precompile: {PRECOMPILE_ERROR})" if PRECOMPILE_ERROR else ""


def _apply_seatbelt() -> bool:
    global SEATBELT_PATH  # noqa: PLW0603
    if _apply_precompiled_seatbelt():
        SEATBELT_PATH = "precompiled"
        return True
    # `sandbox_init` lives in libsystem_sandbox, which libSystem re-exports, so it is already
    # loaded in every process. Looking it up there saves `ctypes.util` (and `shutil`, which it
    # imports): about 4 ms of worker start-up, for a library search that always found the same one.
    lib = ctypes.CDLL(None)
    if not hasattr(lib, "sandbox_init"):
        return False
    lib.sandbox_init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    err = ctypes.c_char_p()
    # flags=0: the first argument is a raw SBPL profile, not a named one.
    if lib.sandbox_init(_SEATBELT_PROFILE.encode(), 0, ctypes.byref(err)) != 0:
        return False
    SEATBELT_PATH = "sandbox_init"
    return True


# --------------------------------------------------------------------------- Linux

_PR_SET_NO_NEW_PRIVS = 38

# The filter is an ALLOW-LIST (`_ALLOWED` below, plus the argument-checked calls after it): a
# syscall nobody listed is refused with EPERM. Of the refused ones, those in `_KILL` end the
# process instead (SECCOMP_RET_KILL_PROCESS), so an exploit probing for one gets no answer to
# iterate on, and the parent reports a sandbox violation.
#
# `_SYSCALLS` is the record of what is refused *on purpose*, with the reason. The default already
# refuses every one of them; the table stays because each entry says why the call must never be
# allowed, and `tests/test_sandbox_syscall_tables.py` checks that the allow-list never re-opens one
# (and every number against the kernel's own tables).
#
# (x86_64, aarch64) syscall numbers; None where the arch has no such call.
_SYSCALLS: dict[str, tuple[int | None, int | None]] = {
    "execve": (59, 221),
    "execveat": (322, 281),
    "fork": (57, None),
    "vfork": (58, None),
    "ptrace": (101, 117),
    "process_vm_readv": (310, 270),
    "process_vm_writev": (311, 271),
    "mount": (165, 40),
    "umount2": (166, 39),
    "pivot_root": (155, 41),
    "chroot": (161, 51),
    "setns": (308, 268),
    "unshare": (272, 97),
    "kexec_load": (246, 104),
    "init_module": (175, 105),
    "finit_module": (313, 273),
    "delete_module": (176, 106),
    "bpf": (321, 280),
    "perf_event_open": (298, 241),
    "userfaultfd": (323, 282),
    "keyctl": (250, 219),
    "add_key": (248, 217),
    "request_key": (249, 218),
    "open_by_handle_at": (304, 265),
    "name_to_handle_at": (303, 264),
    "io_uring_setup": (425, 425),
    "io_uring_enter": (426, 426),
    "io_uring_register": (427, 427),
    "connect": (42, 203),
    "bind": (49, 200),
    "listen": (50, 201),
    "accept": (43, 202),
    "accept4": (288, 242),
    "swapon": (167, 224),
    "swapoff": (168, 225),
    "reboot": (169, 142),
    "acct": (163, 89),
    "kcmp": (312, 272),
    # No standalone sockets at all. asyncio's self-pipe uses socketpair(), which stays allowed.
    # A socketpair's peers are connected to each other, but a *datagram* socketpair can still
    # `sendto()` a named address, so what keeps that harmless is not this filter: it is the
    # empty root and empty network namespace (no path, no abstract name exists to send to) and,
    # on Linux 6.12+, Landlock's abstract-socket scope. An AF_UNIX `socket()` would add nothing
    # to what socketpair already allows, so it is simply closed with the rest.
    "socket": (41, 198),
    # What a worker never asks and an attacker wants: uptime / process count / RAM (`sysinfo`), and
    # other processes' priorities (`getpriority` and `ioprio_get` walk every pid). glibc's thread
    # set-up calls `sched_get*` with a *thread* id, which is why those stay open. NOT `uname`: the
    # kernel version would help pick an exploit, but V8's x86_64 build calls it while starting and
    # aborts (`Check failed: 0 == uname(&uname_buffer)`) if it is refused. The aarch64 build does not,
    # which is why this was found on an x86_64 CI runner and not in an aarch64 container.
    # memfd_create + write holds memory in an anonymous file that never shows in the worker's RSS:
    # 200 memfds of 1 MiB (the RLIMIT_FSIZE) kept ~208 MiB hidden from the memory limit.
    "memfd_create": (319, 279),
    "sysinfo": (99, 179),
    "getpriority": (140, 141),
    "ioprio_get": (252, 31),
    # Taking over or signalling other processes of the same user.
    "pidfd_open": (434, 434),
    "pidfd_getfd": (438, 438),
    "pidfd_send_signal": (424, 424),
    "process_madvise": (440, 440),
    "tkill": (200, 130),  # obsolete; glibc uses tgkill
    # Kernel log and raw I/O ports.
    "syslog": (103, 116),
    "iopl": (172, None),
    "ioperm": (173, None),
    # Landlock governs reading, writing, creating and removing files, but not changing their
    # metadata, so permissions, ownership, timestamps and extended attributes are closed here.
    "chmod": (90, None),
    "fchmod": (91, 52),
    "fchmodat": (268, 53),
    "fchmodat2": (452, 452),
    "chown": (92, None),
    "fchown": (93, 55),
    "lchown": (94, None),
    "fchownat": (260, 54),
    "utime": (132, None),
    "utimes": (235, None),
    "futimesat": (261, None),
    "utimensat": (280, 88),
    "setxattr": (188, 5),
    "lsetxattr": (189, 6),
    "fsetxattr": (190, 7),
    "removexattr": (197, 14),
    "lremovexattr": (198, 15),
    "fremovexattr": (199, 16),
    # Identity and capabilities. A worker running as root (a container) must not be able to
    # change who it is; `no_new_privs` does not cover that.
    "setuid": (105, 146),
    "setgid": (106, 144),
    "setreuid": (113, 145),
    "setregid": (114, 143),
    "setgroups": (116, 159),
    "setresuid": (117, 147),
    "setresgid": (119, 149),
    "setfsuid": (122, 151),
    "setfsgid": (123, 152),
    "capset": (126, 91),
    # Found by `scripts/redteam_syscalls.py`: reachable from a compromised worker, and none of
    # them is something a JavaScript runtime or CPython does after start-up.
    #
    # IPC with the host user's other processes. SysV and POSIX IPC are namespaced by the IPC
    # namespace, not by Landlock, so on a plain host they reach every process of the same user.
    "msgget": (68, 186),
    "msgsnd": (69, 189),
    "msgrcv": (70, 188),
    "msgctl": (71, 187),
    "semget": (64, 190),
    "semop": (65, 193),
    "semctl": (66, 191),
    "semtimedop": (220, 192),
    "shmget": (29, 194),
    "shmat": (30, 196),
    "shmctl": (31, 195),
    "shmdt": (67, 197),
    "mq_open": (240, 180),
    "mq_unlink": (241, 181),
    "mq_timedsend": (242, 182),
    "mq_timedreceive": (243, 183),
    "mq_notify": (244, 184),
    "mq_getsetattr": (245, 185),
    # Watching other processes' file activity: Landlock does not govern it.
    "inotify_init": (253, None),
    "inotify_init1": (294, 26),
    "inotify_add_watch": (254, 27),
    "inotify_rm_watch": (255, 28),
    "fanotify_init": (300, 262),
    "fanotify_mark": (301, 263),
    # The new mount API: mounting, and reading the host's mount table.
    "open_tree": (428, 428),
    "open_tree_attr": (467, 467),
    "mount_setattr": (442, 442),
    "fsconfig": (431, 431),
    "fsopen": (430, 430),
    "fsmount": (432, 432),
    "fspick": (433, 433),
    "move_mount": (429, 429),
    "listmount": (458, 458),
    "statmount": (457, 457),
    # Changing the system clock, and the host's name.
    "settimeofday": (164, 170),
    "clock_settime": (227, 112),
    "adjtimex": (159, 171),
    "clock_adjtime": (305, 266),
    "sethostname": (170, 161),
    "setdomainname": (171, 162),
    # Weakening this process's own defences, and kernel interfaces with no legitimate use here.
    "personality": (135, 92),
    "quotactl": (179, 60),
    "quotactl_fd": (443, 443),
    "lookup_dcookie": (212, 18),
    "nfsservctl": (180, 42),
    "kexec_file_load": (320, 294),
    "lsm_set_self_attr": (460, 460),
    # Flushing every filesystem on the host: a way to stall its disks. (fsync/fdatasync on
    # the worker's own descriptors stay allowed.)
    "sync": (162, 81),
    "syncfs": (306, 267),
    # Landlock only governs path `truncate()` from ABI 3 (kernel 6.2), so on a kernel with
    # Landlock but no empty root it would destroy any file the user can write. (`ftruncate`, on
    # the worker's own descriptors, stays.)
    "truncate": (76, 45),
    # Kernel AIO contexts count against a host-wide limit (`fs.aio-max-nr`): a worker could use
    # them all up and break AIO for every other process. Nothing here uses AIO.
    "io_setup": (206, 0),
    "io_destroy": (207, 1),
    "io_getevents": (208, 4),
    "io_submit": (209, 2),
    "io_cancel": (210, 3),
    "io_pgetevents": (333, 292),
    # The newest path-based and namespace calls (kernel 6.17+).
    "file_getattr": (468, 468),
    "file_setattr": (469, 469),
    "listns": (470, 470),
    "fchroot": (472, 472),
    # Extended attributes by path: Landlock does not govern metadata, and reading them leaks
    # information about files the worker cannot open.
    "setxattrat": (463, 463),
    "removexattrat": (466, 466),
    "getxattr": (191, 8),
    "lgetxattr": (192, 9),
    "listxattr": (194, 11),
    "llistxattr": (195, 12),
    "getxattrat": (464, 464),
    "listxattrat": (465, 465),
    # Second pass (surface a guest never needs): NUMA policy, filesystem mutation, file-to-file
    # copies, POSIX timers/queues, protection keys, and re-entering Landlock/seccomp once applied.
    "mbind": (237, 235),
    "set_mempolicy": (238, 237),
    "set_mempolicy_home_node": (450, 450),
    "memfd_secret": (447, 447),
    "mknodat": (259, 33),
    "linkat": (265, 37),
    "symlinkat": (266, 36),
    "renameat": (264, 38),
    "renameat2": (316, 276),
    "unlinkat": (263, 35),
    "mkdirat": (258, 34),
    "remap_file_pages": (216, 234),
    "copy_file_range": (326, 285),
    "sendfile": (40, 71),
    "splice": (275, 76),
    "tee": (276, 77),
    "sync_file_range": (277, 84),
    "readahead": (187, 213),
    "fallocate": (285, 47),
    "signalfd4": (289, 74),
    "timer_create": (222, 107),
    "timer_delete": (226, 111),
    "timer_settime": (223, 110),
    "timer_gettime": (224, 108),
    "timer_getoverrun": (225, 109),
    "pkey_alloc": (330, 289),
    "pkey_free": (331, 290),
    "pkey_mprotect": (329, 288),
    "process_mrelease": (448, 448),
    "landlock_create_ruleset": (444, 444),
    "landlock_add_rule": (445, 445),
    "landlock_restrict_self": (446, 446),
    "lsm_get_self_attr": (459, 459),
    "lsm_list_modules": (461, 461),
    "seccomp": (317, 277),
    # x86_64 also has the older path-based spellings of the filesystem-mutation calls denied above
    # (aarch64 only ever had the `*at` forms), and glibc on x86_64 calls them directly. Without
    # these the denial above would hold on one architecture and not the other.
    "mkdir": (83, None),
    "rmdir": (84, None),
    "creat": (85, None),
    "link": (86, None),
    "unlink": (87, None),
    "symlink": (88, None),
    "rename": (82, None),
    "mknod": (133, None),
    # obsolete x86 interfaces nothing legitimate needs: load a shared library by path, edit the
    # local descriptor table (a classic exploit helper), and the removed sysctl interface
    "uselib": (134, None),
    "modify_ldt": (154, None),
    "_sysctl": (156, None),
    # Moving another process's memory between NUMA nodes (these were allowed on ourselves until the
    # filter became an allow-list; nothing in the runtime calls them).
    "migrate_pages": (256, 238),
    "move_pages": (279, 239),
}

# Never legitimate in a worker, on any code path of CPython, V8, tokio or the C library: each is
# an exploit's tool (debugging another process, executing a program, changing the mount table or
# the namespaces, loading kernel code or BPF, the io_uring and userfaultfd attack surface, the
# kernel keyring, anonymous memory files) or a system administrator's (the clock, the host name,
# swap, reboot, quotas, the kernel log). A process that makes one of these calls is not a runtime
# hitting an edge case, so it is killed on the spot. Everything else that is refused gets EPERM,
# because some libraries probe a call and fall back (sockets for name-service lookups, the
# scheduler and identity calls): turning a harmless probe into a crash would be an outage. That is
# why `fork` and `vfork` are not here although `execve` is: CPython's `subprocess` calls glibc's
# `vfork()`, which on x86_64 is the `vfork` syscall itself (aarch64 has none and uses `clone`), so
# a library that tries to start a program must get EPERM back, not die. `execve` is only ever
# reached after a fork succeeded, which the filter never lets happen.
# Traced, not assumed: `scripts/trace_worker_syscalls.py` ran real workers through the isolation
# suites on x86_64 and aarch64 and none of these was ever called (see docs/security-report.md).
_KILL: frozenset[str] = frozenset(
    {
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
        "kcmp",
        "pidfd_getfd",
        "execve",
        "execveat",
        "mount",
        "umount2",
        "pivot_root",
        "chroot",
        "fchroot",
        "setns",
        "unshare",
        "open_tree",
        "open_tree_attr",
        "mount_setattr",
        "fsopen",
        "fsconfig",
        "fsmount",
        "fspick",
        "move_mount",
        "swapon",
        "swapoff",
        "reboot",
        "kexec_load",
        "kexec_file_load",
        "init_module",
        "finit_module",
        "delete_module",
        "bpf",
        "perf_event_open",
        "userfaultfd",
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
        "memfd_create",
        "memfd_secret",
        "keyctl",
        "add_key",
        "request_key",
        "open_by_handle_at",
        "name_to_handle_at",
        "acct",
        "iopl",
        "ioperm",
        "modify_ldt",
        "uselib",
        "_sysctl",
        "lookup_dcookie",
        "nfsservctl",
        "quotactl",
        "quotactl_fd",
        "syslog",
        "settimeofday",
        "clock_settime",
        "adjtimex",
        "clock_adjtime",
        "sethostname",
        "setdomainname",
        "lsm_set_self_attr",
    }
)

# What a worker may call with any arguments, found by tracing real workers on both architectures
# (`scripts/trace_worker_syscalls.py`; the list of what each trace saw is in
# docs/security-report.md), plus the close relatives the C library picks between by version or
# architecture (`epoll_wait`/`epoll_pwait`, `fstat`/`newfstatat`/`statx`, ...) and the calls the
# kernel itself relies on (`rt_sigreturn`, `restart_syscall`). Nothing here reaches beyond the
# process: descriptors it already holds, its own memory, threads, clocks and signal state. Path
# lookups (`openat`, the stat family) are allowed because Landlock, not seccomp, decides what a
# path may reach (seccomp cannot read the path), and refusing them would turn every "file not
# found" in CPython's lazy imports into an error it does not expect.
_ALLOWED: dict[str, tuple[int | None, int | None]] = {
    # the hot path first: the filter compares in order
    "futex": (202, 98),
    "epoll_pwait": (281, 22),
    "epoll_wait": (232, None),
    "read": (0, 63),
    "write": (1, 64),
    "recvfrom": (45, 207),
    "sendto": (44, 206),
    "madvise": (28, 233),
    "munmap": (11, 215),
    "clock_nanosleep": (230, 115),
    "getppid": (110, 173),
    "gettid": (186, 178),
    "pread64": (17, 67),
    "sched_getaffinity": (204, 123),
    "rt_sigprocmask": (14, 135),
    "clock_gettime": (228, 113),
    "getpid": (39, 172),
    "sched_yield": (24, 124),
    # memory (mmap/mprotect are checked for PROT_EXEC first when the worker is jitless)
    "mmap": (9, 222),
    "mprotect": (10, 226),
    "brk": (12, 214),
    "mremap": (25, 216),
    # threads
    "set_robust_list": (273, 99),
    "rseq": (334, 293),
    "set_tid_address": (218, 96),
    "exit": (60, 93),
    "exit_group": (231, 94),
    "sched_getparam": (143, 121),
    "sched_getscheduler": (145, 120),
    "sched_getattr": (315, 275),
    "sched_get_priority_max": (146, 125),
    "sched_get_priority_min": (147, 126),
    # time
    "nanosleep": (35, 101),
    "clock_getres": (229, 114),
    "gettimeofday": (96, 169),
    "time": (201, None),
    # signals (delivery to this process only; sending is checked below)
    "rt_sigaction": (13, 134),
    "rt_sigreturn": (15, 139),
    "sigaltstack": (131, 132),
    "restart_syscall": (219, 128),
    # the event loops (asyncio's selector, tokio's epoll) and their wake-ups
    "epoll_create1": (291, 20),
    "epoll_create": (213, None),
    "epoll_ctl": (233, 21),
    "epoll_pwait2": (441, 441),
    "eventfd2": (290, 19),
    "eventfd": (284, None),
    "poll": (7, None),
    "ppoll": (271, 73),
    "select": (23, None),
    "pselect6": (270, 72),
    "pipe2": (293, 59),
    "pipe": (22, None),
    # descriptors the process already holds (the parent's pipes, asyncio's socketpair)
    "readv": (19, 65),
    "writev": (20, 66),
    "pwrite64": (18, 68),
    "preadv": (295, 69),
    "pwritev": (296, 70),
    "lseek": (8, 62),
    "close": (3, 57),
    "dup": (32, 23),
    "dup2": (33, None),
    "dup3": (292, 24),
    "recvmsg": (47, 212),
    "sendmsg": (46, 211),
    "shutdown": (48, 210),
    "fsync": (74, 82),
    "fdatasync": (75, 83),
    "ftruncate": (77, 46),
    # paths: Landlock decides (see above)
    "openat": (257, 56),
    "open": (2, None),
    "fstat": (5, 80),
    "newfstatat": (262, None),
    "fstatat": (None, 79),  # aarch64's name for newfstatat
    "statx": (332, 291),
    "stat": (4, None),
    "lstat": (6, None),
    "access": (21, None),
    "faccessat": (269, 48),
    "faccessat2": (439, 439),
    "readlinkat": (267, 78),
    "readlink": (89, None),
    "getcwd": (79, 17),
    # about this process only
    "getrandom": (318, 278),
    "getrlimit": (97, 163),
    "getrusage": (98, 165),
    "getuid": (102, 174),
    "geteuid": (107, 175),
    "getgid": (104, 176),
    "getegid": (108, 177),
    "getresuid": (118, 148),
    "getresgid": (120, 150),
    "getgroups": (115, 158),
    # V8's x86_64 build calls uname() while it starts and aborts (`Check failed: 0 ==
    # uname(&uname_buffer)`) if it is refused; aarch64 does not, but the answer is the same
    # kernel release on both, and refusing it on one only would add a way to break that one.
    "uname": (63, 160),
}
# Calls that act on *another process* chosen by a pid argument. Allowed only on ourselves
# (pid 0 or our own), so a compromised worker cannot renice, re-pin or re-limit the host process
# or anything else the same user runs.
_SELF_PID_ARG0 = {
    "sched_setscheduler": (144, 119),
    "sched_setparam": (142, 118),
    "sched_setattr": (314, 274),
    "sched_setaffinity": (203, 122),
    "prlimit64": (302, 261),
    # The read-only calls that leak something about another process: `get_robust_list(parent)`
    # returns a pointer into the host process (its ASLR), and `getpgid`/`getsid` over every number
    # enumerate the pids the user is running. (Not `sched_get*`, `getpriority` or `ioprio_get`:
    # glibc and V8 call those with a *thread* id, which is not our pid, so a self-only rule breaks
    # thread startup, and what they return is only a scheduling parameter.)
    "get_robust_list": (274, 100),
    "getpgid": (121, 155),
    "getsid": (124, 156),
}
_SELF_PID_ARG1 = {  # (which, who, ...): `who` is the pid, and `which` must say "a process"
    "setpriority": (141, 140),
    "ioprio_set": (251, 30),
}
# `setpriority(PRIO_USER, 0, ...)` and `ioprio_set(IOPRIO_WHO_USER, 0, ...)` mean "every process
# of the caller's user", the host process and everything else the user runs included. Only the
# "a single process" selector is allowed, and then only on ourselves. The two syscalls number
# their selectors differently: PRIO_PROCESS is 0, IOPRIO_WHO_PROCESS is 1.
_WHICH_PROCESS = {"setpriority": 0, "ioprio_set": 1}
# Signals can also be aimed at the parent without `kill`: `fcntl(fd, F_SETOWN, parent)` plus
# `F_SETSIG` and `O_ASYNC` makes the kernel raise a signal in the owner whenever the descriptor
# becomes ready, gated only by "same user". Nothing in this runtime needs to name an owner or a
# signal for a descriptor, so those commands are closed. (Landlock's signal scope also stops this,
# but only on Linux 6.12+.)
_FCNTL = (72, 25)
_IOCTL = (16, 29)
# `fcntl` and `ioctl` are allow-lists of commands too. fcntl: duplicating, the descriptor and
# status flags, record locks, and read-only queries. Not F_SETOWN/F_SETSIG/F_SETOWN_EX (signals to
# a named owner, see above), F_SETPIPE_SZ (pipe buffers are memory the RSS poll misses),
# F_SETLEASE/F_NOTIFY (signals again) or F_ADD_SEALS. The command is an `int`, so the filter
# judges its low 32 bits, as the kernel does.
_FCNTL_ALLOWED_CMDS = (
    0,  # F_DUPFD
    1,  # F_GETFD
    2,  # F_SETFD
    3,  # F_GETFL
    4,  # F_SETFL
    5,  # F_GETLK
    6,  # F_SETLK
    7,  # F_SETLKW
    9,  # F_GETOWN
    11,  # F_GETSIG
    16,  # F_GETOWN_EX
    36,  # F_OFD_GETLK
    37,  # F_OFD_SETLK
    38,  # F_OFD_SETLKW
    1025,  # F_GETLEASE
    1030,  # F_DUPFD_CLOEXEC
    1032,  # F_GETPIPE_SZ
    1034,  # F_GET_SEALS
)
# ioctl: what CPython, the C library and tokio ask of an ordinary descriptor (is it a terminal, how
# much is buffered, blocking or not, close-on-exec). Every other command is refused, which closes
# by construction what used to be denied one by one: FIOSETOWN/SIOCSPGRP (signal ownership),
# TIOCSTI/TIOCLINUX (terminal injection), FIOASYNC, and the socket-ioctl block (`SIOCGIFCONF`,
# `SIOCGIFHWADDR`, ...: the host's interfaces, addresses and MACs, answered even on a unix socket),
# plus the filesystem ioctls of the one regular file the worker holds (its stderr).
_IOCTL_ALLOWED_CMDS = (
    0x5401,  # TCGETS (isatty)
    0x5413,  # TIOCGWINSZ
    0x541B,  # FIONREAD
    0x5421,  # FIONBIO
    0x5450,  # FIONCLEX
    0x5451,  # FIOCLEX
)
_PRCTL = (157, 167)
_SOCKETPAIR = (53, 199)
# `prctl` can change how the process is traced, scheduled and killed (`PR_SET_DUMPABLE`,
# `PR_SET_PDEATHSIG`, `PR_SCHED_CORE` on another process, speculation controls), so only what a real
# worker does is allowed (verified by tracing 182 workers across the isolation suite): naming its
# threads, naming memory areas, and reading its dumpable flag.
_PRCTL_ALLOWED = (15, 16, 0x53564D41, 3)  # SET_NAME, GET_NAME, SET_VMA, GET_DUMPABLE
_PR_SET_DUMPABLE = 4
# Mapping memory executable. A jitless V8 never needs it, and refusing it means an exploit must
# work without injecting code of its own.
_EXEC_CHECKED = (
    (9, 222),
    (10, 226),
)  # mmap, mprotect (pkey_mprotect is denied outright)
_PROT_EXEC = 4
_AF_UNIX, _SOCK_STREAM, _SOCK_TYPE_MASK = 1, 1, 0xF
# Every syscall number below this has been looked at (`tests/data/syscalls.json`, from the
# kernel's own tables, and `tests/test_sandbox_syscall_tables.py` fails if the table ever grows
# past it). Numbers from here up are syscalls that did not exist when this filter was reviewed:
# a future kernel's new interface, or the x32 ABI's flag bit. They get ENOSYS rather than the
# allow-list's EPERM, because ENOSYS is what V8, CPython and glibc treat as "not available on this
# kernel" and fall back from, which is the truth for a call the filter has never heard of.
_FIRST_UNREVIEWED = 473
# Needs argument inspection or a different errno, so handled separately below.
_CLONE = (56, 220)
_CLONE3 = (435, 435)
# Signals may be sent to this process and to nobody else: `kill(parent, SIGKILL)` is the
# first thing a V8 escape would try. arg0 is the target pid (tgid for tgkill).
_SIGNALS = {
    "kill": (62, 129),
    "tgkill": (234, 131),
    "rt_sigqueueinfo": (129, 138),
    "rt_tgsigqueueinfo": (297, 240),
}

_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_SYS_SECCOMP = {"x86_64": 317, "aarch64": 277}

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_JSET_K = 0x45
_BPF_RET_K = 0x06
_BPF_AND_K = 0x54
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM, _ENOSYS = 1, 38
_CLONE_THREAD = 0x10000


def _seccomp_program(arch: str, *, allow_exec: bool = True) -> bytes | None:
    """Assemble the filter: an allow-list. In order, a syscall is
    1. killed if it comes from another architecture's ABI (the numbers would mean other calls);
    2. ENOSYS if its number is past everything reviewed (`_FIRST_UNREVIEWED`);
    3. EPERM if it would map memory executable in a jitless worker;
    4. allowed if it is in `_ALLOWED`;
    5. judged on its arguments if it is one of the calls checked below (threads only, signals and
       scheduling on ourselves only, fcntl/ioctl/prctl commands from a list, ...);
    6. killed if it is in `_KILL` (never legitimate: the parent reports a sandbox violation);
    7. otherwise EPERM."""
    idx = 0 if arch == "x86_64" else 1
    allowed = [pair[idx] for pair in _ALLOWED.values() if pair[idx] is not None]
    kill = [
        _SYSCALLS[name][idx]
        for name in sorted(_KILL)
        if _SYSCALLS[name][idx] is not None
    ]

    # (code, jt_label, jf_label, k); labels are resolved to relative offsets below.
    ins: list[tuple[int, str | None, str | None, int | str]] = []
    # A name can be defined many times. BPF jumps only go forward and reach at most 255
    # instructions, so each stretch of the program gets its own `allow`/`eperm`/... stubs and a
    # jump lands on the nearest one after it.
    labels: dict[str, list[int]] = {}

    def label(name: str) -> None:
        labels.setdefault(name, []).append(len(ins))

    def stubs(*, enosys: bool = False) -> None:
        label("allow")
        ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ALLOW))
        label("eperm")
        ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ERRNO | _EPERM))
        if enosys:  # glibc falls back to clone() when clone3 reports ENOSYS
            label("enosys")
            ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ERRNO | _ENOSYS))
            label("violation")
            ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_KILL_PROCESS))

    ins.append((_BPF_LD_W_ABS, None, None, 4))  # arch
    ins.append((_BPF_JEQ_K, "nr", "kill", _AUDIT_ARCH[arch]))
    label("kill")
    ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_KILL_PROCESS))
    label("nr")
    ins.append((_BPF_LD_W_ABS, None, None, 0))  # nr
    ins.append(
        (_BPF_JGE_K, "enosys", None, _FIRST_UNREVIEWED)
    )  # also catches the x32 bit
    if not allow_exec:  # before the allow-list, which has mmap and mprotect
        for pair in _EXEC_CHECKED:
            ins.append((_BPF_JEQ_K, "noexec", None, pair[idx]))
    for nr in allowed:
        ins.append((_BPF_JEQ_K, "allow", None, nr))
    ins.append((_BPF_JEQ_K, "clone", None, _CLONE[idx]))
    ins.append((_BPF_JEQ_K, "enosys", None, _CLONE3[idx]))
    for pair in _SIGNALS.values():
        ins.append((_BPF_JEQ_K, "signal", None, pair[idx]))
    for pair in _SELF_PID_ARG0.values():
        ins.append((_BPF_JEQ_K, "selfpid0", None, pair[idx]))
    for name, pair in _SELF_PID_ARG1.items():
        ins.append((_BPF_JEQ_K, f"selfpid1_{name}", None, pair[idx]))
    ins.append((_BPF_JEQ_K, "fcntl", None, _FCNTL[idx]))
    ins.append((_BPF_JEQ_K, "ioctl", None, _IOCTL[idx]))
    ins.append((_BPF_JEQ_K, "prctl", None, _PRCTL[idx]))
    ins.append((_BPF_JEQ_K, "socketpair", None, _SOCKETPAIR[idx]))
    for nr in kill:
        ins.append((_BPF_JEQ_K, "violation", None, nr))
    ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ERRNO | _EPERM))  # the default
    stubs(enosys=True)

    label("clone")  # only thread creation: flags must contain CLONE_THREAD
    ins.append((_BPF_LD_W_ABS, None, None, 16))
    ins.append((_BPF_JSET_K, "allow", "eperm", _CLONE_THREAD))
    stubs()
    label("signal")  # only to ourselves (low 32 bits of the pid argument)
    ins.append((_BPF_LD_W_ABS, None, None, 16))
    ins.append((_BPF_JEQ_K, "allow", "eperm", os.getpid()))
    stubs()
    label("selfpid0")  # pid 0 means "the caller"; anything else must be our own pid
    ins.append((_BPF_LD_W_ABS, None, None, 16))
    ins.append((_BPF_JEQ_K, "allow", None, 0))
    ins.append((_BPF_JEQ_K, "allow", "eperm", os.getpid()))
    stubs()
    for name in _SELF_PID_ARG1:
        # `which` (arg0) must be the "a single process" selector, and `who` (arg1) ourselves.
        label(f"selfpid1_{name}")
        ins.append((_BPF_LD_W_ABS, None, None, 16))
        ins.append((_BPF_JEQ_K, None, "eperm", _WHICH_PROCESS[name]))
        ins.append((_BPF_LD_W_ABS, None, None, 24))
        ins.append((_BPF_JEQ_K, "allow", None, 0))
        ins.append((_BPF_JEQ_K, "allow", "eperm", os.getpid()))
        stubs()
    label("fcntl")  # the command is arg1 (its low 32 bits, an int)
    ins.append((_BPF_LD_W_ABS, None, None, 24))
    for cmd in _FCNTL_ALLOWED_CMDS:
        ins.append((_BPF_JEQ_K, "allow", None, cmd))
    ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ERRNO | _EPERM))
    stubs()
    label("ioctl")  # the request is arg1; the kernel takes its low 32 bits too
    ins.append((_BPF_LD_W_ABS, None, None, 24))
    for cmd in _IOCTL_ALLOWED_CMDS:
        ins.append((_BPF_JEQ_K, "allow", None, cmd))
    ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ERRNO | _EPERM))
    stubs()
    label("prctl")  # the option is arg0
    ins.append((_BPF_LD_W_ABS, None, None, 16))
    for option in _PRCTL_ALLOWED:
        ins.append((_BPF_JEQ_K, "allow", None, option))
    ins.append((_BPF_RET_K, None, None, _SECCOMP_RET_ERRNO | _EPERM))
    stubs()
    if not allow_exec:
        label("noexec")  # `prot` is arg2 of both
        ins.append((_BPF_LD_W_ABS, None, None, 32))
        ins.append((_BPF_JSET_K, "eperm", "allow", _PROT_EXEC))
        stubs()
    label(
        "socketpair"
    )  # only a stream socketpair: a datagram one can `sendto` any path
    ins.append((_BPF_LD_W_ABS, None, None, 16))
    ins.append((_BPF_JEQ_K, None, "eperm", _AF_UNIX))
    ins.append((_BPF_LD_W_ABS, None, None, 24))
    ins.append((_BPF_AND_K, None, None, _SOCK_TYPE_MASK))
    ins.append((_BPF_JEQ_K, "allow", "eperm", _SOCK_STREAM))
    stubs()

    def target(name: str, at: int) -> int:
        # The nearest definition after `at`: jumps only go forward.
        return min(pos for pos in labels[name] if pos > at) - at - 1

    out = b""
    for i, (code, jt, jf, k) in enumerate(ins):
        jt_off = target(jt, i) if jt else 0
        jf_off = target(jf, i) if jf else 0
        if not (0 <= jt_off < 256 and 0 <= jf_off < 256):
            return None
        out += struct.pack("<HBBI", code, jt_off, jf_off, k)
    return out


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _apply_seccomp(*, allow_exec: bool = True) -> bool:
    arch = _machine()
    if arch == "arm64":
        arch = "aarch64"
    if arch not in _AUDIT_ARCH:
        return False
    program = _seccomp_program(arch, allow_exec=allow_exec)
    if program is None:
        return False
    libc = _libc()
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return False
    buf = ctypes.create_string_buffer(program, len(program))

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = SockFprog(len(program) // 8, ctypes.cast(buf, ctypes.c_void_p))
    # TSYNC: apply to every existing thread, not just this one.
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(_SYS_SECCOMP[arch], 1, 1, ctypes.byref(prog)) != 0:
        return False
    global _FILTER_IN_FORCE  # noqa: PLW0603
    _FILTER_IN_FORCE = True
    return True


#: True once this process runs under the filter (`attest()` must then not fire a killing call).
_FILTER_IN_FORCE = False
#: What is known about the kill action on this kernel, set by `_seccomp_is_safe_here`:
#: "verified" (a throwaway child under the filter made a never-legitimate call and was killed),
#: "not-killed" (it made the call and came back), "available" (the kernel reports the action as
#: supported; not exercised), "unavailable", or "" (not checked). `attest()` reports a breach for
#: "not-killed" and "unavailable" once the filter is in force.
SECCOMP_KILL = ""
_SIGSYS = 31  # on x86_64 and aarch64 alike
_SECCOMP_GET_ACTION_AVAIL = 2


def _kill_action_available(arch_nr: int) -> bool:
    """Does the kernel support SECCOMP_RET_KILL_PROCESS? A query; changes nothing."""
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    action = ctypes.c_uint32(_SECCOMP_RET_KILL_PROCESS)
    return (
        libc.syscall(arch_nr, _SECCOMP_GET_ACTION_AVAIL, 0, ctypes.byref(action)) == 0
    )


def _seccomp_is_safe_here(
    *, allow_exec: bool = True, verify_kill: bool = False
) -> bool:
    """Fire the filter in a throwaway child first.

    The filter hard-codes syscall numbers per architecture and kills the process if
    the architecture does not match. `platform.machine()` can lie under emulation
    (an x86_64 image on Apple silicon reports x86_64 while the kernel is aarch64), so
    instead of trusting it, find out whether this exact filter survives here, and skip
    the layer rather than kill the worker if it does not.

    With `verify_kill=True` (what `sandbox_status()` asks for) the same child then makes one
    never-legitimate call (`execve` of a path that cannot exist) and must die of SIGSYS before
    the kernel looks at the path: the kill action, exercised. A worker start does not do that,
    because the kernel audits every seccomp kill (`type=SECCOMP` in the audit log, or the kernel
    log), and a record per worker start would bury the real ones a host's monitoring looks for;
    it asks the kernel whether the action is supported instead. Either way the answer lands in
    `SECCOMP_KILL`. (A filter in front of ours, such as a container runtime's, cannot weaken
    the kill: the kernel applies the most severe action of all filters.)
    """
    global SECCOMP_KILL  # noqa: PLW0603
    SECCOMP_KILL = ""
    idx = _arch_index()
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never returns
        code = 4
        try:
            os.close(read_end)
            # It may be killed on purpose: no core file, and not dumpable.
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            _libc().prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
            if _apply_seccomp(allow_exec=allow_exec) and os.getpid() > 0:
                os.write(write_end, b"1")
                code = 0
                if verify_kill and idx is not None:
                    code = (
                        5  # the canary call below came back: the kill is not in force
                    )
                    libc = _libc()
                    libc.syscall.restype = ctypes.c_long
                    libc.syscall(
                        _SYSCALLS["execve"][idx],
                        b"/nonexistent-pydeno-canary",
                        None,
                        None,
                    )
            else:
                code = 3
        finally:
            os._exit(code)
    os.close(write_end)
    try:
        installed = os.read(read_end, 1) == b"1"
    finally:
        os.close(read_end)
    _, status = os.waitpid(pid, 0)
    exited = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    killed = os.WIFSIGNALED(status) and os.WTERMSIG(status) == _SIGSYS
    if not installed:
        return False
    if verify_kill:
        SECCOMP_KILL = "verified" if killed else "not-killed"
        # Safe to install either way once it installed; a filter that does not kill is reported
        # by `attest()`, which is what makes `sandbox="require"` refuse it.
        return killed or exited == 5
    arch = "aarch64" if _machine() in ("aarch64", "arm64") else _machine()
    try:
        available = _kill_action_available(_SYS_SECCOMP[arch])
    except (KeyError, OSError, AttributeError):
        available = False
    SECCOMP_KILL = "available" if available else "unavailable"
    return exited == 0


_LANDLOCK_CREATE, _LANDLOCK_ADD, _LANDLOCK_RESTRICT = 444, 445, 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0


#: The kernel's Landlock ABI version as this process found it (0: none, or not asked yet).
LANDLOCK_ABI = 0
#: Why Landlock was not counted as applied although the kernel accepted it, or "" (diagnostics:
#: the worker's start-up failure message and `sandbox_status()`).
LANDLOCK_NOTE = ""
# Directories a process can normally open for reading; the canary needs one that worked before.
_CANARY_DIRS = ("/", "/tmp", "/usr", "/proc/self")


def _readable_dir() -> str | None:
    for path in _CANARY_DIRS:
        try:
            os.close(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        except OSError:
            continue
        return path
    return None


def _landlock_canary(path: str | None) -> bool:
    """After `landlock_restrict_self`: is reading `path`, which worked a moment ago, now refused
    the way Landlock refuses it (EACCES)? A kernel, an LSM stack or a container runtime that
    accepts the ruleset and then does not enforce it is otherwise invisible: the syscall said 0."""
    global LANDLOCK_NOTE  # noqa: PLW0603
    if path is None:
        # Nothing was readable even before (an unusual root): no canary is possible, and the
        # filesystem is closed to this process either way.
        return True
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except PermissionError:
        return True
    except OSError as exc:
        LANDLOCK_NOTE = (
            f"canary open of {path} failed with errno {exc.errno}, not EACCES"
        )
        return False
    os.close(fd)
    LANDLOCK_NOTE = (
        f"the kernel accepted the Landlock ruleset (ABI {LANDLOCK_ABI}) but {path} could "
        "still be opened afterwards: Landlock is not enforced here"
    )
    return False


def _apply_landlock() -> bool:
    """Deny all filesystem access (and TCP where the kernel can) to this thread and
    everything it spawns afterwards. Already-open file descriptors keep working.

    True only if a canary then proves it: a directory that could be opened before can no longer
    be (`_landlock_canary`)."""
    global LANDLOCK_ABI, LANDLOCK_NOTE  # noqa: PLW0603
    LANDLOCK_NOTE = ""
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(_LANDLOCK_CREATE, None, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 1:
        return False
    LANDLOCK_ABI = int(abi)
    canary = _readable_dir()
    fs = (1 << 13) - 1  # ABI v1: EXECUTE .. MAKE_SYM
    if abi >= 2:
        fs |= 1 << 13  # REFER
    if abi >= 3:
        fs |= 1 << 14  # TRUNCATE
    if abi >= 5:
        fs |= 1 << 15  # IOCTL_DEV
    net = 0b11 if abi >= 4 else 0  # BIND_TCP | CONNECT_TCP
    scoped = (
        0b11 if abi >= 6 else 0
    )  # ABSTRACT_UNIX_SOCKET | SIGNAL: nothing outside us
    if abi >= 6:
        attr = struct.pack("<QQQ", fs, net, scoped)
    elif abi >= 4:
        attr = struct.pack("<QQ", fs, net)
    else:
        attr = struct.pack("<Q", fs)
    buf = ctypes.create_string_buffer(attr, len(attr))
    fd = libc.syscall(_LANDLOCK_CREATE, buf, len(attr), 0)
    if fd < 0:
        return False
    try:
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return False
        # No rules added: every handled access is denied.
        if libc.syscall(_LANDLOCK_RESTRICT, fd, 0) != 0:
            return False
    finally:
        os.close(fd)
    return _landlock_canary(canary)


def layer_notes() -> str:
    """Why a layer did not count although the kernel offered it, for start-up failure messages
    (parent-side diagnostics only, never shown to guest code)."""
    notes = seatbelt_note()
    if LANDLOCK_NOTE:
        notes += f" (landlock: {LANDLOCK_NOTE})"
    return notes


# --------------------------------------------------------------------------- public


#: Bonus layers that took effect in *this* process, kept apart from the names `apply()` returns so
#: that the core answer ("landlock+seccomp", "seatbelt") means the same on every machine. Today:
#: "emptyroot", which only works where unprivileged user namespaces are allowed.
EXTRAS: list[str] = []
#: Kernel-enforced resource caps `apply()` set beyond the rlimits every worker gets: "tasklimit"
#: (RLIMIT_NPROC per worker, see `_limit_tasks`). Diagnostics; never part of `apply()`'s answer.
KERNEL_CAPS: list[str] = []

_CLONE_NEWNS = 0x00020000
_CLONE_NEWUTS = 0x04000000
_CLONE_NEWIPC = 0x08000000
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000
_MS_NOSUID, _MS_NODEV, _MS_NOEXEC = 0x2, 0x4, 0x8
_MS_REC, _MS_PRIVATE = 0x4000, 1 << 18
_MNT_DETACH = 2
_PR_SET_DUMPABLE = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_ROOT_CANDIDATES = ("/tmp", "/run", "/mnt", "/var/tmp")


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _machine() -> str:
    # What `platform.machine()` returns on POSIX, without importing `platform` into the worker.
    return os.uname().machine


def _arch_index() -> int | None:
    machine = _machine()
    return {"x86_64": 0, "aarch64": 1, "arm64": 1}.get(machine)


def _clear_capabilities(libc: ctypes.CDLL) -> bool:
    """Empty the effective, permitted and inheritable sets (inside the current user namespace)."""
    idx = _arch_index()
    if idx is None:
        return False
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    return libc.syscall(_SYSCALLS["capset"][idx], ctypes.byref(header), data) == 0


def _apply_empty_root() -> bool:
    """Give the process its own mount namespace whose root is an empty tmpfs.

    Neither Landlock nor seccomp can hide what *exists* on the host (the path pointer is in user
    memory, and Landlock does not govern `stat`). A mount namespace can: after this, every path
    lookup finds nothing. The same `unshare` also takes new network, IPC and UTS namespaces, so
    even a bypass of the seccomp filter would find no interface to talk to and no IPC objects.

    This is what bubblewrap does, and it needs unprivileged user namespaces, which some systems
    forbid, so it is a best-effort extra layer: any failure leaves the process as it was.
    `CLONE_NEWUSER` also requires a single-threaded caller; the worker applies the sandbox
    before it starts any thread for exactly that reason.
    """
    idx = _arch_index()
    if idx is None:
        return False
    libc = _libc()
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    root = next((d for d in _ROOT_CANDIDATES if os.path.isdir(d)), None)
    if root is None:
        return False

    uid, gid = os.geteuid(), os.getegid()
    # After a setuid the process is non-dumpable and /proc/self/*_map belongs to root, so the
    # id maps cannot be written; make it dumpable for these few calls, then undo that.
    libc.prctl(_PR_SET_DUMPABLE, 1, 0, 0, 0)
    entered = False
    try:
        flags = (
            _CLONE_NEWUSER
            | _CLONE_NEWNS
            | _CLONE_NEWNET
            | _CLONE_NEWIPC
            | _CLONE_NEWUTS
        )
        if libc.unshare(flags) != 0:
            return False
        entered = True
        with open("/proc/self/setgroups", "w") as fh:
            fh.write("deny")
        with open("/proc/self/gid_map", "w") as fh:
            fh.write(f"{gid} {gid} 1")
        with open("/proc/self/uid_map", "w") as fh:
            fh.write(f"{uid} {uid} 1")
        if libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None) != 0:
            return False
        # tmpfs over an existing directory (nothing to clean up on the host afterwards),
        # then make that the root and let go of the old one.
        if (
            libc.mount(
                b"pydeno-empty",
                root.encode(),
                b"tmpfs",
                _MS_NOSUID | _MS_NODEV | _MS_NOEXEC,
                b"size=4k,mode=0555",
            )
            != 0
        ):
            return False
        os.chdir(root)
        if libc.syscall(_SYSCALLS["pivot_root"][idx], b".", b".") != 0:
            return False
        if libc.umount2(b".", _MNT_DETACH) != 0:
            return False
        os.chdir("/")
        return True
    except OSError:
        return False
    finally:
        if entered:
            # Entering a user namespace grants a full capability set *inside* it. Whatever
            # happened above, it must not stay that way.
            for cap in range(0, 64):
                libc.prctl(_PR_CAPBSET_DROP, cap, 0, 0, 0)
            _clear_capabilities(libc)
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)


#: Most tasks (threads) the worker may have, enforced by the kernel where it can be per worker.
#: Twice the supervisor's sampled cap (`_isolated._MAX_WORKER_THREADS`, 64), so that one still
#: fires first with its own message; this one is the ceiling a thread bomb cannot outrun.
TASK_LIMIT = 128


def _kernel_at_least(major: int, minor: int) -> bool:
    try:
        parts = os.uname().release.split(".")
        return (int(parts[0]), int(parts[1].split("-")[0])) >= (major, minor)
    except (ValueError, IndexError):
        return False


def _limit_tasks() -> bool:
    """Linux, inside the worker's own user namespace: RLIMIT_NPROC at `TASK_LIMIT`.

    RLIMIT_NPROC counts the tasks of a *user*, which on a plain host means every process the
    host user runs, so it cannot cap one worker there. Since Linux 5.14 the count is kept per
    user namespace (ucounts), and the empty-root layer gives the worker a namespace of its own:
    in it, the count is exactly the worker's threads. Only then is it set. The hard limit can be
    raised only with CAP_SYS_RESOURCE in the initial namespace, which the worker never has."""
    if not _kernel_at_least(5, 14):
        return False
    resource.setrlimit(resource.RLIMIT_NPROC, (TASK_LIMIT, TASK_LIMIT))
    return True


_READ_IMPLIES_EXEC = 0x0400000


def _clear_read_implies_exec() -> None:
    """Linux: drop the `READ_IMPLIES_EXEC` personality if this process inherited it.

    With it set, the kernel adds PROT_EXEC to every PROT_READ mapping *after* seccomp has looked at
    the arguments, which would silently defeat the no-executable-mapping rule. It survives exec
    (`setarch -X`, and older x86_64 kernels for binaries without a GNU_STACK header)."""
    libc = _libc()
    current = libc.personality(0xFFFFFFFF)
    if current != -1 and current & _READ_IMPLIES_EXEC:
        libc.personality(current & ~_READ_IMPLIES_EXEC)


def _thread_count_here() -> int:
    """Threads in this process, including ones Python does not know about.

    Landlock and the user-namespace layer only cover the calling thread, so a thread started by
    native code (a library's pool, a runtime's worker) before `apply()` would be left unconfined
    while the worker still reported "landlock". `threading.active_count()` cannot see those."""
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return threading.active_count()


def apply(
    *, empty_root: bool = True, allow_exec: bool = True, verify_kill: bool = False
) -> str:
    """Confine the current process. Returns the layers applied, e.g. "landlock+seccomp",
    "seatbelt", or "none". Bonus layers land in `EXTRAS`.

    `empty_root=False` skips the mount-namespace layer (Linux), for callers that need the
    filesystem to stay visible."""
    layers: list[str] = []
    EXTRAS.clear()
    KERNEL_CAPS.clear()

    def attempt(name: str, fn: Callable[[], bool], into: list[str]) -> None:
        # One layer failing must never stop the next from being tried: each gets its own try.
        try:
            if fn():
                into.append(name)
        except (OSError, ValueError, AttributeError, ctypes.ArgumentError):
            pass

    if sys.platform == "darwin":
        attempt("seatbelt", _apply_seatbelt, layers)
    elif sys.platform.startswith("linux"):
        # Landlock restricts only the calling thread (and what it creates later), and the
        # user-namespace layer is refused outright in a multi-threaded process. This process is
        # single-threaded by construction (the worker reads `init` before starting a thread); if
        # it is not, claiming "landlock" would be a lie about the threads that already exist, so
        # those two layers are skipped and `sandbox="require"` will say so.
        single_threaded = _thread_count_here() == 1
        # The empty root first: it needs the filesystem and the mount syscalls that the layers
        # below take away.
        if empty_root and single_threaded:
            attempt("emptyroot", _apply_empty_root, EXTRAS)
            if "emptyroot" in EXTRAS:
                attempt("tasklimit", _limit_tasks, KERNEL_CAPS)
        if single_threaded:
            attempt("landlock", _apply_landlock, layers)
        if not allow_exec:
            _clear_read_implies_exec()  # before seccomp, which denies `personality`
        # seccomp last, with TSYNC, so it also covers any thread that already exists.
        attempt(
            "seccomp",
            lambda: (
                _seccomp_is_safe_here(allow_exec=allow_exec, verify_kill=verify_kill)
                and _apply_seccomp(allow_exec=allow_exec)
            ),
            layers,
        )
    return "+".join(layers) or "none"


def _procargs_of_parent() -> bool:
    """macOS: can this process read its parent's argv and environment (KERN_PROCARGS2)?

    The buffer is big enough for any realistic environment, and ENOMEM counts as "yes": it is
    what a call that was *allowed* answers when the data does not fit, so mistaking it for a
    refusal would hide the very capability this looks for."""
    libc = _libc()
    mib = (ctypes.c_int * 3)(1, 49, os.getppid())  # CTL_KERN, KERN_PROCARGS2, pid
    buf = ctypes.create_string_buffer(1 << 20)
    size = ctypes.c_size_t(len(buf))
    rc = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0)
    return rc == 0 or ctypes.get_errno() == errno.ENOMEM


def _hardware_uuid_readable() -> bool:
    """macOS: can this process read the machine's permanent hardware identifier?"""
    libc = _libc()
    uuid = ctypes.create_string_buffer(16)

    class _Timespec(ctypes.Structure):
        _fields_ = (("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long))

    wait = _Timespec(1, 0)
    return libc.gethostuuid(uuid, ctypes.byref(wait)) == 0 and any(uuid.raw)


def _creates_sysv_semaphore() -> bool:
    """macOS: can this process create a SysV semaphore (an object that outlives it)? Removes it again."""
    libc = _libc()
    semid = libc.semget(0, 1, 0o1600)  # IPC_PRIVATE, one semaphore, IPC_CREAT | 0600
    if semid == -1:
        return False
    libc.semctl(semid, 0, 0)  # IPC_RMID
    return True


def attest() -> list[str]:
    """Try, from inside the confined process, the things the sandbox exists to stop, and return
    the ones that worked. Empty means every probe was refused.

    The layers are assembled from lists of what to deny, and a list is only as good as its last
    review: the macOS profile once let the worker read the host's environment through a sysctl
    nobody had thought of. This does not trust the lists. It asks the kernel, once, before any
    guest code exists, so a hole of that kind stops the worker from starting instead of waiting
    for a reviewer. Every probe is a refusal we expect, so it costs a handful of syscalls.

    A probe is one security-sensitive call. If that call succeeds it is a breach, recorded at
    once; cleanup afterwards is best effort and never changes the verdict. Only an `OSError` from
    the sensitive call counts as a refusal: anything else (a bug in a probe) propagates, and the
    worker refuses to start rather than report a sandbox it did not actually check.
    """
    breaches: list[str] = []
    ppid = os.getppid()
    if ppid <= 1:
        # Reparented to init (or the parent is gone): the "parent" probes below would aim at pid 1, or,
        # worse, at -1 (every process) if a denied getppid returned an error. Refuse to start.
        raise RuntimeError(
            "the worker was orphaned before its sandbox self-test could run"
        )

    def check(
        name: str,
        call: Callable[[], object],
        cleanup: Callable[[], object] | None = None,
    ) -> None:
        try:
            call()
        except OSError:
            return  # refused: the answer we want
        breaches.append(name)
        if cleanup is not None:
            try:
                cleanup()
            except OSError:
                pass

    def read(path: str) -> None:
        with open(path, "rb") as fh:
            fh.read(1)

    created: list[str] = []

    def write() -> None:
        path = f"/tmp/.pydeno-attest-{os.getpid()}"
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        created.append(path)

    spawned: list[int] = []

    def spawn() -> None:
        spawned.append(os.posix_spawn("/bin/sh", ["sh", "-c", "exit 0"], {}))

    def connect() -> None:
        # Seatbelt lets `socket()` succeed and refuses the connect, so test the connect. Only the
        # sandbox's own refusal (a PermissionError) counts: "connection refused" means the attempt
        # reached the network stack, which the sandbox should have prevented.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect(("127.0.0.1", 9))
            except PermissionError:
                raise
            except OSError:
                return

    pairs: list[tuple[socket.socket, socket.socket]] = []

    def unix_dgram() -> None:
        # A datagram socketpair can `sendto` any path the process can name (journald, notify).
        pairs.append(socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM))

    def cleanup_files() -> None:
        for path in created:
            os.unlink(path)

    def cleanup_children() -> None:
        for pid in spawned:
            os.waitpid(pid, 0)

    def cleanup_sockets() -> None:
        for a, b in pairs:
            a.close()
            b.close()

    check("read-file", lambda: read("/etc/hosts"))
    check("read-parent-environ", lambda: read(f"/proc/{ppid}/environ"))
    check("write-file", write, cleanup_files)
    check("spawn-process", spawn, cleanup_children)
    check("network-socket", connect)
    check("signal-parent", lambda: os.kill(ppid, 0))
    if sys.platform.startswith("linux") and _FILTER_IN_FORCE:
        # Under the filter `execve` is never-legitimate: making it here would kill this process.
        # `_seccomp_is_safe_here` checked the kill action before the filter went up (exercised in
        # a throwaway child for `sandbox_status()`, asked of the kernel for a worker).
        if SECCOMP_KILL in ("not-killed", "unavailable"):
            breaches.append("exec-not-killed")
    elif sys.platform.startswith("linux"):
        # `execve` of a path that cannot exist: a filter refuses at syscall entry, and if exec is
        # allowed the answer is ENOENT. Unlike spawning /bin/sh this does not depend on the image
        # having a shell at all. (Not on macOS: there the path is looked up first, so even a
        # healthy sandbox answers ENOENT; the spawn probe above covers it.)
        try:
            os.execv("/nonexistent-pydeno-attest", ["x"])
        except PermissionError:
            pass  # refused
        except OSError:
            breaches.append("exec-allowed")
    if sys.platform == "darwin":
        # These signal by return value rather than by raising.
        if _creates_sysv_semaphore():
            breaches.append("create-sysv-object")
        if _procargs_of_parent():
            breaches.append("read-parent-argv-environ")
        if _hardware_uuid_readable():
            breaches.append("read-hardware-uuid")
        check("inspect-other-process", lambda: os.getpgid(ppid))
    if sys.platform.startswith("linux"):
        check("unix-datagram-socket", unix_dgram, cleanup_sockets)
    return breaches


# What "every layer this platform has" means, for `sandbox="require"`. Anything else is a
# platform without a sandbox we can apply, so nothing satisfies it.
REQUIRED_LAYERS: dict[str, frozenset[str]] = {
    "darwin": frozenset({"seatbelt"}),
    "linux": frozenset({"landlock", "seccomp"}),
}


def missing_layers(applied: str) -> frozenset[str]:
    """The layers this platform should have had but `applied` (the string `apply()` returned)
    does not contain. Empty means the sandbox is complete."""
    key = (
        "darwin"
        if sys.platform == "darwin"
        else "linux"
        if sys.platform.startswith("linux")
        else ""
    )
    needed = REQUIRED_LAYERS.get(key, frozenset({"a-supported-platform"}))
    have = frozenset() if applied == "none" else frozenset(applied.split("+"))
    return needed - have


_PR_CAPBSET_DROP = 24
_NOBODY = 65534


def drop_privileges() -> dict[str, object]:
    """Make this process unprivileged, whatever it was started as.

    A worker started as root (a container, a service) would otherwise be one capability away
    from changing the clock, the hostname or the mount table, and the seccomp filter would be
    the only thing between a V8 escape and those. So: empty the bounding set (nothing can ever
    regain a capability), clear the supplementary groups, and become `nobody`. Must run before
    `apply()`, which blocks the calls used here.

    Returns what it did, for the tests; nothing here is fatal if the platform refuses.
    """
    report: dict[str, object] = {
        "uid_before": os.geteuid() if hasattr(os, "geteuid") else None
    }
    if not sys.platform.startswith("linux"):
        return report
    try:
        libc = _libc()
        dropped = 0
        for cap in range(0, 64):  # until the kernel says the capability does not exist
            if libc.prctl(_PR_CAPBSET_DROP, cap, 0, 0, 0) == 0:
                dropped += 1
        report["bounding_dropped"] = dropped
    except (OSError, AttributeError):
        pass
    if os.geteuid() == 0:
        try:
            os.setgroups([])
            os.setgid(_NOBODY)
            os.setuid(_NOBODY)
            report["became"] = _NOBODY
        except OSError as exc:
            report["became_error"] = str(exc)
    if os.geteuid() == 0:
        # Still root (an unmapped uid in a rootless container, say): the bounding set is empty, but
        # the capabilities already held are not, so clear them too. `sandbox="require"` refuses to
        # run in this state; the other modes at least do not run it with capabilities.
        try:
            report["capabilities_cleared"] = _clear_capabilities(_libc())
        except (OSError, AttributeError):
            pass
    report["uid_after"] = os.geteuid()
    return report


def harden_process() -> dict[str, object]:
    """Rlimits and environment for a worker that should be boring: no core dumps (they
    would contain guest data), bounded file writes, a fixed timezone so the guest cannot
    read the host's, and no privileges."""
    os.environ["TZ"] = "UTC"
    os.environ["LANG"] = "C.UTF-8"
    # Some CPython builds have no `time.tzset` (the x86_64 macOS builds made by cross-compiling: the
    # configure check for a working tzset cannot run); the C library reads TZ on first use anyway.
    if hasattr(time, "tzset"):
        time.tzset()
    # No core dumps (they would hold guest data); files capped at 1 MiB (the worker's stderr is
    # a file); a small descriptor table (the worker needs a few dozen, a hostile one should not
    # be able to hold thousands); nothing pinned in memory; no POSIX message queues.
    for name, value in (
        ("RLIMIT_CORE", 0),
        ("RLIMIT_FSIZE", 1 << 20),
        ("RLIMIT_NOFILE", 256),
        ("RLIMIT_MEMLOCK", 0),
        ("RLIMIT_MSGQUEUE", 0),
        # A host user who is allowed realtime priority (an audio group, say) would otherwise let
        # the worker run at realtime priority and starve the host.
        ("RLIMIT_RTPRIO", 0),
        ("RLIMIT_NICE", 0),
    ):
        try:
            res = getattr(resource, name)
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError, AttributeError):
            pass
    if sys.platform.startswith("linux"):
        # Not dumpable: a same-user process cannot ptrace it or read its /proc/<pid>/mem, and
        # the filter later refuses `prctl` options, so a guest cannot switch it back on.
        _libc().prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    return drop_privileges()


# How far private writable memory may exceed `max_memory` before the kernel itself refuses more.
# The limit the watchdogs enforce is resident memory, sampled every 20 ms; this is a hard ceiling
# under it that needs no sampling and that code in the worker cannot lift (raising a hard limit
# takes CAP_SYS_RESOURCE, which the worker never has). Private writable mappings run ahead of
# resident memory: thread stacks, malloc arenas, and with the JIT about 250 MiB of code space are
# counted before they are touched (measured: an idle jitless worker 86 MiB of data at 41 MiB
# resident, a JIT worker 342 MiB at 45 MiB), so the headroom is generous. A normal workload meets
# `max_memory` long before this; only a burst faster than the sampling (or native code mapping
# memory directly) reaches it, and then the allocation fails inside the worker.
DATA_HEADROOM = 1 << 30


def _data_bytes_now() -> int:
    """Private writable address space already reserved by this process (`VmData`). A free-threaded
    CPython reserves 1 GiB of it before any script runs, and RLIMIT_DATA counts reservations."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmData:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def limit_data(max_memory: int | None) -> int | None:
    """Linux: cap this process's private writable memory (RLIMIT_DATA) at what it has reserved
    now plus `max_memory + DATA_HEADROOM`, so the interpreter's own baseline does not eat the
    budget (`DATA_HEADROOM` is for what V8 and the guest add). Returns the limit set, or None (no `max_memory`, not Linux, or
    refused). Not RLIMIT_AS: V8 reserves tens of GiB of address space it never uses (the
    pointer-compression cage and its guard regions), so an address-space limit would either stop
    V8 from starting or be too large to bound anything."""
    if max_memory is None or not sys.platform.startswith("linux"):
        return None
    limit = _data_bytes_now() + max_memory + DATA_HEADROOM
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_DATA)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    except (ValueError, OSError, AttributeError):
        return None
    return limit


def rss_reader(pid: int | None = None) -> Callable[[], int | None]:
    """A cheap resident-memory probe that keeps working after `apply()`.

    Under Landlock a sandboxed process can no longer `open()` `/proc/<pid>/statm`, so
    the file is opened here, *before* the sandbox, and read through the descriptor
    (reads on an already-open fd are still allowed).
    """
    pid = pid or os.getpid()
    if sys.platform.startswith("linux"):
        try:
            fd = os.open(f"/proc/{pid}/statm", os.O_RDONLY)
            page = os.sysconf("SC_PAGE_SIZE")
        except OSError:
            return lambda: None

        def read_linux() -> int | None:
            try:
                return int(os.pread(fd, 256, 0).split()[1]) * page
            except (OSError, ValueError, IndexError):
                return None

        return read_linux
    return lambda: rss_bytes(pid)


class _TaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("csw", ctypes.c_int32),
        ("threadnum", ctypes.c_int32),
        ("numrunning", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


class _TimebaseInfo(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


# Resolved once, on first use: looking a symbol up through a fresh `ctypes.CDLL(None)` costs tens
# of microseconds, and the parent reads these on every command it supervises.
_proc_pidinfo: Callable[..., int] | None = None
# Mach absolute-time units (darwin) or clock ticks (Linux) per second.
_TICKS_PER_SECOND = 0.0
_TASK_INFO_SIZE = ctypes.sizeof(_TaskInfo)


def _darwin_task_info(pid: int) -> _TaskInfo | None:
    """One `proc_pidinfo(PROC_PIDTASKINFO)` call: memory, CPU times and thread count together."""
    global _proc_pidinfo, _TICKS_PER_SECOND  # noqa: PLW0603
    fn = _proc_pidinfo
    if fn is None:
        libc = ctypes.CDLL(None)  # libproc is part of libSystem
        base = _TimebaseInfo()
        libc.mach_timebase_info(ctypes.byref(base))
        # task times are in Mach absolute-time units, not nanoseconds
        _TICKS_PER_SECOND = 1e9 * base.denom / base.numer
        fn = libc.proc_pidinfo
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        fn.restype = ctypes.c_int
        _proc_pidinfo = fn
    # A fresh structure per call: the parent samples from more than one thread.
    info = _TaskInfo()
    if fn(pid, 4, 0, ctypes.addressof(info), _TASK_INFO_SIZE) != _TASK_INFO_SIZE:
        return None
    return info


def _linux_stat(pid: int) -> list[bytes]:
    with open(f"/proc/{pid}/stat", "rb") as fh:
        # the command name (field 2) may contain spaces and parentheses: split after it
        return fh.read().rsplit(b")", 1)[1].split()


def usage(pid: int) -> tuple[int | None, float | None, int | None]:
    """`(rss_bytes, cpu_seconds, thread_count)` of `pid` from ONE kernel read, each None if it
    cannot be read. What the parent samples as each command finishes."""
    global _TICKS_PER_SECOND  # noqa: PLW0603
    try:
        if sys.platform.startswith("linux"):
            fields = _linux_stat(pid)
            if not _TICKS_PER_SECOND:
                _TICKS_PER_SECOND = float(os.sysconf("SC_CLK_TCK"))
            # The list starts at field 3: utime 14, stime 15, num_threads 20, rss 24 (pages).
            return (
                int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
                (int(fields[11]) + int(fields[12])) / _TICKS_PER_SECOND,
                int(fields[17]),
            )
        if sys.platform == "darwin":
            info = _darwin_task_info(pid)
            if info is None:
                return (None, None, None)
            return (
                int(info.resident_size),
                (info.total_user + info.total_system) / _TICKS_PER_SECOND,
                int(info.threadnum),
            )
    except (OSError, ValueError, IndexError, TypeError, AttributeError):
        pass
    return (None, None, None)


def cpu_seconds(pid: int) -> float | None:
    """CPU time (user + system, all threads) the process `pid` has consumed so far, or None.

    This is what a guest cannot hide. Wall-clock deadlines have to stop counting while the host
    runs a callback, and a guest can arrange for a callback to always be outstanding; CPU time
    only goes up when something is actually computing.
    """
    return usage(pid)[1]


def thread_count(pid: int) -> int | None:
    """How many threads the process `pid` has, or None. A worker has about 13 on Linux and 17 on
    macOS; a guest that gets native code can start thousands within a second, well under a
    memory ceiling, and a handful of such workers exhausts the host's thread table."""
    return usage(pid)[2]


def rss_bytes(pid: int) -> int | None:
    """Resident memory of `pid` without spawning anything, or None if unreadable."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/statm", "rb") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            info = _darwin_task_info(pid)
            return None if info is None else int(info.resident_size)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return None
