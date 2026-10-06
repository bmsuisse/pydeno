"""Opt-in fork-from-template worker start (Linux only). See docs/guides/advanced/fork-template.md.

A *template* is a worker-shaped Python process that has finished every import the worker will ever
need and then waits on a control socket, single-threaded and with no V8 and no isolate. Starting a
sandboxed worker is then a `fork()` of the template (a few milliseconds) instead of a fresh
interpreter plus the `asyncio` import chain (tens of milliseconds). Everything after the fork is the
ordinary worker: the child runs `_worker.main()`, which applies the same OS sandbox, runs the same
start-up self-test and creates its V8 isolate and seed itself.

What this does *not* change: workers stay single-use; `sandbox="require"` and the self-test run in
the child exactly as before; limits, wire checks and orphan handling are the worker's own.

What it does change, and why it is off by default: every worker forked from one template shares that
template's address-space layout and stack canary, and Python's hash seed and module-level random
state. An information leak in one session therefore helps against the next one from the same
template. The mitigations are in this file: the template is replaced after `max_forks` forks or
`max_age_seconds` (whichever comes first), and V8 and its seed are created only in the child.

Process tree: parent -> template -> workers. The parent cannot `wait()` for a worker it did not
start, so the template reaps its children and reports exit codes over the control socket;
`ForkedProc` is the small `subprocess.Popen` look-alike the rest of the package already speaks.
If the parent dies the control socket closes and the template exits at once, which changes its
workers' parent pid, which their own watchdog (`_worker._watch`) treats as "the parent is gone".
"""

from __future__ import annotations

import os
import select
import signal
import socket
import struct
import sys
import threading
import time
from typing import Any

# One event on the control socket, template -> parent: kind, pid, value.
#   b"P" pid, 0        a fork succeeded
#   b"E" 0, errno      a fork failed
#   b"X" pid, code     a worker exited (Popen-style: -N for signal N)
_EVENT = struct.Struct(">cii")
_FORK = b"F"  # parent -> template, with three descriptors: the worker's stdin, stdout, stderr
_RETIRE = b"R"  # parent -> template: fork no more, exit when the last worker has gone

#: Defaults for rotating the template (see the module docstring).
DEFAULT_MAX_FORKS = 64
DEFAULT_MAX_AGE_SECONDS = 300.0

_SPAWN_REPLY_SECONDS = 30.0
# Exit codes are kept after being read (several threads ask about one worker); the oldest go first.
_MAX_REMEMBERED_EXITS = 4096


def supported() -> bool:
    return sys.platform.startswith("linux") and hasattr(socket, "send_fds")


# ---------------------------------------------------------------------------
# the template process
# ---------------------------------------------------------------------------


def template_main(ctrl_fd: int) -> None:
    """Entry point of the template process (`python -I -S -c ...`, see `_template_argv`)."""
    # Every import the worker needs, done once. After this the process is what a worker is just
    # before it reads `init`, minus its pipes.
    from . import _worker

    ctrl = socket.socket(fileno=ctrl_fd)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    signal.signal(signal.SIGCHLD, lambda *_: None)  # the wake-up fd does the work
    # The template must not be killed by the terminal-style signals a worker's group gets.
    children: set[int] = set()
    retiring = False
    while True:
        readable, _, _ = select.select([ctrl, wake_r], [], [])
        if wake_r in readable:
            try:
                os.read(wake_r, 4096)
            except BlockingIOError:
                pass
            _reap(ctrl, children)
        if ctrl in readable:
            try:
                data, fds, _flags, _addr = socket.recv_fds(ctrl, 1, 3)
            except OSError:
                os._exit(0)
            if not data:
                # The parent is gone (or let go): leaving takes the workers with it.
                os._exit(0)
            if data == _RETIRE:
                retiring = True
            elif data == _FORK and len(fds) == 3 and not retiring:
                _fork_worker(ctrl, fds, children, _worker)
            else:
                for fd in fds:
                    os.close(fd)
                if data == _FORK:
                    _send(
                        ctrl, b"E", 0, 22
                    )  # EINVAL: wrong descriptor count, or retiring
        if retiring and not children:
            os._exit(0)


def _send(ctrl: socket.socket, kind: bytes, pid: int, value: int) -> None:
    try:
        ctrl.sendall(_EVENT.pack(kind, pid, value))
    except OSError:
        os._exit(0)


def _reap(ctrl: socket.socket, children: set[int]) -> None:
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        children.discard(pid)
        _send(ctrl, b"X", pid, os.waitstatus_to_exitcode(status))


def _fork_worker(
    ctrl: socket.socket, fds: list[int], children: set[int], worker: Any
) -> None:
    stdin_fd, stdout_fd, stderr_fd = fds
    try:
        pid = os.fork()
    except OSError as exc:
        for fd in fds:
            os.close(fd)
        _send(ctrl, b"E", 0, exc.errno or 12)
        return
    if pid:
        for fd in fds:
            os.close(fd)
        children.add(pid)
        _send(ctrl, b"P", pid, 0)
        return
    # --- the child: from here on this is a worker, nothing of the template's remains ---
    code = 1
    try:
        signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        os.setsid()  # like `start_new_session=True`: a kill of the group takes strays with it
        os.dup2(stdin_fd, 0)
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        # Everything else (the control socket, the wake-up pipe, the originals) goes.
        _close_other_fds()
        worker.main()
        code = 0
    finally:
        os._exit(code)


def _close_other_fds() -> None:
    """Close every descriptor above 2. Not `os.closerange(3, SC_OPEN_MAX)`: where that loops over
    `close()` it is half a million syscalls (about 100 ms) on a host with a high descriptor limit."""
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        os.closerange(3, 4096)
        return
    for name in names:
        fd = int(name)
        if fd > 2:
            try:
                os.close(fd)
            except OSError:  # the listing's own descriptor, already gone
                pass


# ---------------------------------------------------------------------------
# the parent's side
# ---------------------------------------------------------------------------


class ForkedProc:
    """What the rest of the package needs of a `subprocess.Popen`, for a worker the template forked."""

    def __init__(self, template: _Template, pid: int, stdin: Any, stdout: Any) -> None:
        self._template = template
        self.pid = pid
        self.stdin = stdin
        self.stdout = stdout
        self.returncode: int | None = None
        self.args = ["pydeno-forked-worker"]

    def poll(self) -> int | None:
        if self.returncode is None:
            self.returncode = self._template.exit_code(self.pid, wait=0.0)
        MANAGER.reap_retired()  # a retired template that has left is a zombie until someone waits
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            code = self._template.exit_code(self.pid, wait=timeout)
            if code is None:
                import subprocess

                raise subprocess.TimeoutExpired(self.args, timeout or 0.0)
            self.returncode = code
        return self.returncode

    def kill(self) -> None:
        if self.returncode is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    terminate = kill


class _Template:
    """One template process and the control socket to it."""

    def __init__(self, max_forks: int, max_age_seconds: float) -> None:
        import subprocess
        import tempfile

        from . import _isolated

        self.max_forks = max_forks
        self.max_age_seconds = max_age_seconds
        self.forks = 0
        self.born = time.monotonic()
        self._lock = threading.Lock()
        self._exits: dict[int, int] = {}
        self._buffer = b""
        self._dead = False
        parent_end, child_end = socket.socketpair()
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed with the template
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _isolated._WORKER_BOOT_PREFIX  # noqa: SLF001
                    + "from pydeno._template import template_main; "
                    + f"template_main({child_end.fileno()})",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                env={},
                pass_fds=[child_end.fileno()],
                close_fds=True,
                start_new_session=True,
            )
        except BaseException:
            parent_end.close()
            self._stderr.close()
            raise
        finally:
            child_end.close()
        self.sock = parent_end

    # -- events ------------------------------------------------------------

    def _drain(self, timeout: float) -> bool:
        """Read what the template has sent; True if anything arrived. Caller holds the lock."""
        if self._dead:
            return False
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return False
        try:
            chunk = self.sock.recv(4096, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            chunk = b""
        if not chunk:
            self._dead = True
            return True
        self._buffer += chunk
        return True

    def _events(self) -> list[tuple[bytes, int, int]]:
        out = []
        while len(self._buffer) >= _EVENT.size:
            out.append(_EVENT.unpack(self._buffer[: _EVENT.size]))
            self._buffer = self._buffer[_EVENT.size :]
        for kind, pid, value in out:
            if kind == b"X":
                self._exits[pid] = value
                if len(self._exits) > _MAX_REMEMBERED_EXITS:
                    del self._exits[next(iter(self._exits))]
            elif kind == b"P":
                self._exits.pop(
                    pid, None
                )  # a reused pid must not inherit the old answer
        return out

    def exit_code(self, pid: int, wait: float | None) -> int | None:
        deadline = None if wait is None else time.monotonic() + wait
        with self._lock:
            while True:
                self._drain(0.0)
                self._events()
                if pid in self._exits:
                    # Not popped: several threads ask about the same worker (the pump and a
                    # `close()`), and each must get the answer.
                    return self._exits[pid]
                if self._dead:
                    # The template is gone, and with it every worker it had (they watch their
                    # parent). The real status died with it.
                    return -signal.SIGKILL
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._drain(0.05 if remaining is None else min(0.05, remaining))

    # -- spawning ----------------------------------------------------------

    def exhausted(self) -> bool:
        return (
            self._dead
            or self.proc.poll() is not None
            or self.forks >= self.max_forks
            or time.monotonic() - self.born >= self.max_age_seconds
        )

    def fork_worker(self, stderr_fd: int) -> ForkedProc:
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        try:
            with self._lock:
                socket.send_fds(self.sock, [_FORK], [stdin_r, stdout_w, stderr_fd])
                self.forks += 1
                deadline = time.monotonic() + _SPAWN_REPLY_SECONDS
                while True:
                    self._drain(0.05)
                    for kind, pid, value in self._events():
                        if kind == b"P":
                            proc = ForkedProc(
                                self,
                                pid,
                                os.fdopen(stdin_w, "wb", buffering=0),
                                os.fdopen(stdout_r, "rb", buffering=0),
                            )
                            return proc
                        if kind == b"E":
                            raise OSError(value, "the worker template could not fork")
                    if self._dead or time.monotonic() > deadline:
                        raise OSError("the worker template did not answer")
        except BaseException:
            for fd in (stdin_w, stdout_r):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        finally:
            os.close(stdin_r)
            os.close(stdout_w)

    def retire(self) -> None:
        """No more forks from this template; it exits once its last worker has."""
        with self._lock:
            if not self._dead:
                try:
                    self.sock.send(_RETIRE)
                except OSError:
                    pass

    def close(self) -> None:
        """Let go of the control socket: the template exits and takes its workers with it."""
        try:
            self.sock.close()
        except OSError:
            pass
        self._dead = True
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001, S110
            pass
        self._stderr.close()


class TemplateManager:
    """Owns the current template and replaces it when it has made `max_forks` workers or is
    `max_age_seconds` old. A replaced template is *retired*, not killed: killing it would take its
    live workers with it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enabled = False
        self.max_forks = DEFAULT_MAX_FORKS
        self.max_age_seconds = DEFAULT_MAX_AGE_SECONDS
        self._current: _Template | None = None
        self._retired: list[_Template] = []

    def enable(self, max_forks: int, max_age_seconds: float) -> None:
        with self._lock:
            self.max_forks = max_forks
            self.max_age_seconds = max_age_seconds
            self.enabled = True

    def disable(self) -> None:
        """Start no more workers from a template. The current one is retired, not killed: it exits
        by itself once its last worker has."""
        with self._lock:
            self.enabled = False
            current, self._current = self._current, None
            if current is not None:
                current.retire()
                self._retired.append(current)

    def prestart(self) -> None:
        """Launch the template now, so its imports overlap with whatever the caller does next
        instead of delaying the first worker."""
        with self._lock:
            if self.enabled and self._current is None:
                try:
                    self._current = _Template(self.max_forks, self.max_age_seconds)
                except OSError:
                    pass

    def spawn(self, stderr_fd: int) -> ForkedProc:
        with self._lock:
            template = self._current
            if template is None or template.exhausted():
                if template is not None:
                    template.retire()
                    self._retired.append(template)
                self._reap_retired()
                template = self._current = _Template(
                    self.max_forks, self.max_age_seconds
                )
            return template.fork_worker(stderr_fd)

    def reap_retired(self) -> None:
        """Wait for retired templates that have exited. Never blocks (skips if busy)."""
        if not self._retired or not self._lock.acquire(blocking=False):
            return
        try:
            self._reap_retired()
        finally:
            self._lock.release()

    def _reap_retired(self) -> None:
        keep = []
        for template in self._retired:
            if template.proc.poll() is None:
                keep.append(template)
            else:
                template.close()
        self._retired = keep

    def shutdown(self) -> None:
        """At interpreter exit and after `fork()` in the parent: nothing outlives this process."""
        with self._lock:
            everything = [self._current, *self._retired]
            self._current, self._retired = None, []
        for template in everything:
            if template is not None:
                template.close()

    def forget(self) -> None:
        """In a forked child of the parent: the sockets belong to the parent. Close our copies
        (a template only exits on EOF when *every* copy is closed) and say nothing to it."""
        self._lock = threading.Lock()
        for template in [self._current, *self._retired]:
            if template is not None:
                try:
                    template.sock.close()
                except OSError:
                    pass
        self._current, self._retired = None, []
        self.enabled = False


MANAGER = TemplateManager()


def enable_fork_template(
    *,
    max_forks: int = DEFAULT_MAX_FORKS,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> None:
    """Start sandboxed workers by forking a prepared template instead of launching a fresh
    interpreter. Opt-in, Linux only; see the module docstring for the trade-off (workers from one
    template share an address-space layout and stack canary) and `docs/guides/advanced/fork-template.md`.

    The template is replaced after `max_forks` workers or `max_age_seconds`, whichever comes first.
    Applies to workers started after this call, by `IsolatedRuntime`, `AsyncIsolatedRuntime`,
    `SandboxPool` and everything built on them. A custom `python=` interpreter is never forked.
    Environment equivalent: `PYDENO_FORK_TEMPLATE=1` (default limits)."""
    if not supported():
        raise NotImplementedError("the fork template is Linux only")
    if isinstance(max_forks, bool) or not isinstance(max_forks, int) or max_forks < 1:
        raise ValueError("max_forks must be a positive integer")
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not max_age_seconds > 0
    ):
        raise ValueError("max_age_seconds must be a positive number")
    MANAGER.enable(max_forks, float(max_age_seconds))
    MANAGER.prestart()


def disable_fork_template() -> None:
    """Go back to launching a fresh interpreter per worker. Workers already running are not
    touched."""
    MANAGER.disable()


def fork_template_enabled() -> bool:
    return MANAGER.enabled


if os.environ.get("PYDENO_FORK_TEMPLATE") == "1" and supported():
    MANAGER.enable(DEFAULT_MAX_FORKS, DEFAULT_MAX_AGE_SECONDS)
