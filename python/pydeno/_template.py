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
template's address-space layout (including V8's code, which is statically linked into `_pydeno`, a
module the template imports), its stack canary and pointer guard, Python's hash seed and `id()`
layout. (Python reseeds `random` after a fork, so that state is not shared.) An information leak in
one session therefore helps against the next one from the same template. Rotation (`max_forks`
forks or `max_age_seconds`, whichever comes first) only bounds how many workers share a layout; it
bounds neither a worker's lifetime nor how many tenants are live on one template at once. V8's heap
and its seed are created only in the child.

Process tree: parent -> template -> workers. The parent cannot `wait()` for a worker it did not
start, so the template reaps its children and reports exit codes over the control socket;
`ForkedProc` is the small `subprocess.Popen` look-alike the rest of the package already speaks.
If the parent dies the control socket closes and the template exits at once, which changes its
workers' parent pid, which their own watchdog (`_worker._watch`) treats as "the parent is gone".

Protocol (all frames fixed size, every request that expects a reply carries a sequence number):
host -> template `F seq` (+ three descriptors), `R` (retire), `A pid` (the host has the exit status
of `pid`); template -> host `P seq pid`, `E seq errno`, `X pid code`. The template does not reap a
finished worker until the host acks it (`waitid(WNOWAIT)` reports the status first), so the pid
cannot be recycled while the host may still signal it. A reader thread per template, in the host,
owns the socket; nothing holds a lock while it waits.

Kept as-is on purpose: the template's working directory, umask, resource limits and environment
(empty) are frozen at the template's start, so a later `os.chdir()` or `os.umask()` in the host is
not seen by workers (the exec path has the same empty environment and `-I`).
"""

from __future__ import annotations

import atexit
import os
import select
import signal
import socket
import struct
import sys
import threading
import time
import traceback
import weakref
from typing import Any

# One event on the control socket, template -> host: kind, seq, pid, value.
#   b"P" seq pid, 0    a fork succeeded
#   b"E" seq 0, errno  a fork failed
#   b"X" 0 pid, code   a worker exited (Popen-style: -N for signal N); the template keeps the
#                      zombie until the host acks it
_EVENT = struct.Struct(">cIii")
# One request, host -> template: kind, seq, pid.
_REQUEST = struct.Struct(">cIi")
_FORK = b"F"  # with three descriptors: the worker's stdin, stdout, stderr
_RETIRE = b"R"  # fork no more, exit when the last worker has gone and been acked
_ACK = b"A"  # the host has the exit status of `pid`: the template may reap it

#: Defaults for rotating the template (see the module docstring).
DEFAULT_MAX_FORKS = 64
DEFAULT_MAX_AGE_SECONDS = 300.0

_SPAWN_REPLY_SECONDS = 30.0
# How long a retired template that has exited may take to deliver its last frames before it is
# closed regardless (a stray copy of its socket somewhere would otherwise keep the reader waiting).
_EOF_GRACE_SECONDS = 2.0


def supported() -> bool:
    return sys.platform.startswith("linux") and hasattr(socket, "send_fds")


# ---------------------------------------------------------------------------
# the template process
# ---------------------------------------------------------------------------


def template_main(ctrl_fd: int) -> None:
    """Entry point of the template process (`python -I -S -c ...`, see `_Template.__init__`)."""
    # Every import the worker needs, done once. After this the process is what a worker is just
    # before it reads `init`, minus its pipes.
    from . import _worker

    ctrl = socket.socket(fileno=ctrl_fd)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    signal.signal(signal.SIGCHLD, lambda *_: None)  # the wake-up fd does the work
    # `poll`, not `select`: the control descriptor keeps the number it had in the host
    # (`pass_fds`), which can be 1024 or more in a host with many files open.
    poller = select.poll()
    poller.register(ctrl_fd, select.POLLIN)
    poller.register(wake_r, select.POLLIN)
    running: set[int] = set()  # forked, not yet exited
    zombies: set[int] = set()  # exited and reported, not yet acked: still unreaped
    early_acks: set[int] = set()  # acked before they exited
    buffer = b""
    fds: list[int] = []
    retiring = False
    while True:
        ready = {fd for fd, _event in poller.poll()}
        if wake_r in ready:
            try:
                os.read(wake_r, 4096)
            except BlockingIOError:
                pass
            _reap(ctrl, running, zombies, early_acks)
        if ctrl_fd in ready:
            try:
                data, got, _flags, _addr = socket.recv_fds(ctrl, _REQUEST.size * 16, 3)
            except OSError:
                os._exit(0)
            if not data:
                # The parent is gone (or let go): leaving takes the workers with it.
                os._exit(0)
            buffer += data
            fds.extend(got)
            while len(buffer) >= _REQUEST.size:
                kind, seq, arg = _REQUEST.unpack(buffer[: _REQUEST.size])
                buffer = buffer[_REQUEST.size :]
                if kind == _RETIRE:
                    retiring = True
                elif kind == _ACK:
                    if arg in zombies:
                        zombies.discard(arg)
                        _waitpid(arg)
                    elif arg in running:
                        early_acks.add(arg)
                elif kind == _FORK:
                    mine, fds = fds[:3], fds[3:]
                    if len(mine) == 3 and not retiring:
                        _fork_worker(ctrl, seq, mine, running, _worker)
                    else:
                        for fd in mine:
                            os.close(fd)
                        _send(
                            ctrl, b"E", seq, 0, 22
                        )  # EINVAL: bad descriptors, or retiring
            if not buffer:  # descriptors that belong to no frame
                for fd in fds:
                    os.close(fd)
                fds = []
        if retiring and not running and not zombies:
            os._exit(0)


def _send(ctrl: socket.socket, kind: bytes, seq: int, pid: int, value: int) -> None:
    try:
        ctrl.sendall(_EVENT.pack(kind, seq, pid, value))
    except OSError:
        os._exit(0)


def _waitpid(pid: int) -> None:
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _reap(
    ctrl: socket.socket, running: set[int], zombies: set[int], early_acks: set[int]
) -> None:
    """Report workers that have exited. The status is read with WNOWAIT, so the process stays a
    zombie (and its pid stays ours) until the host acks the report."""
    for pid in list(running):
        try:
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            running.discard(pid)
            continue
        if info is None:
            continue
        code = (
            -info.si_status
            if info.si_code in (os.CLD_KILLED, os.CLD_DUMPED)
            else info.si_status
        )
        running.discard(pid)
        if pid in early_acks:
            early_acks.discard(pid)
            _waitpid(pid)
        else:
            zombies.add(pid)
        _send(ctrl, b"X", 0, pid, code)


def _fork_worker(
    ctrl: socket.socket, seq: int, fds: list[int], running: set[int], worker: Any
) -> None:
    stdin_fd, stdout_fd, stderr_fd = fds
    try:
        pid = os.fork()
    except OSError as exc:
        for fd in fds:
            os.close(fd)
        _send(ctrl, b"E", seq, 0, exc.errno or 12)
        return
    if pid:
        for fd in fds:
            os.close(fd)
        running.add(pid)
        _send(ctrl, b"P", seq, pid, 0)
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
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        try:
            traceback.print_exc()  # the host reads this file when it reports the crash
            sys.stderr.flush()
        except BaseException:  # noqa: BLE001, S110
            pass
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
# the host's side
# ---------------------------------------------------------------------------


def _killpg_or_kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class ForkedProc:
    """What the rest of the package needs of a `subprocess.Popen`, for a worker the template forked."""

    def __init__(self, template: _Template, pid: int, stdin: Any, stdout: Any) -> None:
        self._template = template
        self.pid = pid
        self.stdin = stdin
        self.stdout = stdout
        self.returncode: int | None = None
        self.args = ["pydeno-forked-worker"]
        self._lock = threading.Lock()
        # Shared with the finalizer: a dropped proc still acks its pid, once.
        self._acked = [False]
        weakref.finalize(self, _ack_once, template, pid, self._acked)

    def _resolve(self, code: int) -> None:
        with self._lock:
            if self.returncode is None:
                self.returncode = code
            _ack_once(self._template, self.pid, self._acked)

    def poll(self) -> int | None:
        if self.returncode is None:
            code = self._template.exit_code(self.pid, 0.0)
            if code is not None:
                self._resolve(code)
        MANAGER.reap_retired()  # a retired template that has left is a zombie until someone waits
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            code = self._template.exit_code(self.pid, timeout)
            if code is None:
                import subprocess

                raise subprocess.TimeoutExpired(self.args, timeout or 0.0)
            self._resolve(code)
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        # Until the exit is acked the template still holds the zombie, so the pid is ours.
        with self._lock:
            if not self._acked[0]:
                _killpg_or_kill(self.pid)

    terminate = kill


def _ack_once(template: _Template, pid: int, acked: list[bool]) -> None:
    if not acked[0]:
        acked[0] = True
        template.ack(pid)


class _Template:
    """One template process, the control socket to it, and the thread that reads it."""

    def __init__(self, max_forks: int, max_age_seconds: float) -> None:
        import subprocess
        import tempfile

        from . import _isolated

        self.max_forks = max_forks
        self.max_age_seconds = max_age_seconds
        self.forks = (
            0  # reserved forks (see `reserve`); touched under the manager's lock
        )
        self.born = time.monotonic()
        self._cv = threading.Condition(threading.Lock())  # guards everything below
        self._send_lock = threading.Lock()
        self._exits: dict[int, int] = {}
        self._started: set[int] = set()
        self._replies: dict[int, tuple[bytes, int, int]] = {}
        self._waiting: set[int] = set()
        self._seq = 0
        self.dead = False  # no more frames will arrive (EOF) or we closed it
        self.eof = False
        self.broken = (
            False  # a fork request went unanswered: no more forks from this one
        )
        self.retire_requested = False
        self._retire_sent = False
        self._outstanding = 0  # reserved forks whose reply has not been dealt with yet
        self._closing = False
        self._exited_seen: float | None = None
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
        self._reader = threading.Thread(
            target=self._read_loop, name="pydeno-template-reader", daemon=True
        )
        self._reader.start()

    # -- reading -----------------------------------------------------------

    def _read_loop(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = self.sock.recv(4096)
            except InterruptedError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= _EVENT.size:
                self._on_event(*_EVENT.unpack(buffer[: _EVENT.size]))
                buffer = buffer[_EVENT.size :]
        self._on_eof()

    def _on_event(self, kind: bytes, seq: int, pid: int, value: int) -> None:
        abandoned = False
        with self._cv:
            if kind == b"X":
                self._exits[pid] = value
            else:
                if kind == b"P":
                    self._exits.pop(
                        pid, None
                    )  # a reused pid must not inherit the old answer
                if seq in self._waiting:
                    if kind == b"P":
                        self._started.add(pid)
                    self._replies[seq] = (kind, pid, value)
                else:
                    abandoned = kind == b"P"
            self._cv.notify_all()
        if abandoned:
            # Nobody is waiting for this worker any more (the request timed out or was
            # interrupted): it is ours to stop. The template still holds its pid.
            _killpg_or_kill(pid)
            self.ack(pid)

    def _unresolved(self) -> list[int]:
        with self._cv:
            return [pid for pid in self._started if pid not in self._exits]

    def _on_eof(self) -> None:
        with self._cv:
            self.eof = True
            closing = self._closing
        if not closing:
            # The template went away: its workers are orphans and will notice, but the host
            # keeps the authority to stop them and does not wait for that.
            lost = self._unresolved()
            for pid in lost:
                _killpg_or_kill(pid)
            if lost or not self.retire_requested:
                self._log_unexpected_exit(len(lost))
        with self._cv:
            self.dead = True
            self._cv.notify_all()

    def _log_unexpected_exit(self, lost: int) -> None:
        import logging

        try:
            self._stderr.seek(0)
            tail = self._stderr.read(2048).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            tail = ""
        logging.getLogger("pydeno").warning(
            "the worker template exited unexpectedly (%d live worker(s) killed)%s",
            lost,
            f": {tail}" if tail else "",
        )

    def exit_code(self, pid: int, wait: float | None) -> int | None:
        """The worker's exit code, waiting up to `wait` seconds (None: as long as it takes).
        Holds no lock while it waits."""
        deadline = None if wait is None else time.monotonic() + wait
        with self._cv:
            while True:
                if pid in self._exits:
                    # Not popped: several threads ask about one worker (the pump and a close()).
                    return self._exits[pid]
                if self.dead:
                    # The template is gone; `_on_eof` has already SIGKILLed what it had left.
                    return -signal.SIGKILL
                if deadline is None:
                    self._cv.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    # -- writing -----------------------------------------------------------

    def _send(self, data: bytes, fds: list[int] | None = None) -> None:
        with self._send_lock:
            if fds:
                socket.send_fds(self.sock, [data], fds)
            else:
                self.sock.sendall(data)

    def ack(self, pid: int) -> None:
        """Tell the template the host has this worker's exit status: it may reap the zombie."""
        try:
            self._send(_REQUEST.pack(_ACK, 0, pid))
        except (OSError, ValueError):
            pass

    # -- spawning ----------------------------------------------------------

    def exhausted(self) -> bool:
        return (
            self.dead
            or self.broken
            or self._closing
            or self.proc.poll() is not None
            or self.forks >= self.max_forks
            or time.monotonic() - self.born >= self.max_age_seconds
        )

    def reserve(self) -> None:
        """Count a fork against `max_forks`. The manager does this under its lock, so the template
        is not handed out past its limit, and then forks outside the lock."""
        with self._cv:
            self.forks += 1
            self._outstanding += 1

    def fork_worker(self, stderr_fd: int) -> ForkedProc:
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        seq = 0
        pid = 0
        clean_failure = False  # the template answered with an error and is still sound
        try:
            with self._cv:
                if self.dead or self.broken or self._closing:
                    clean_failure = True
                    raise OSError("the worker template is gone")
                self._seq += 1
                seq = self._seq
                self._waiting.add(seq)
            self._send(_REQUEST.pack(_FORK, seq, 0), [stdin_r, stdout_w, stderr_fd])
            deadline = time.monotonic() + _SPAWN_REPLY_SECONDS
            with self._cv:
                while seq not in self._replies:
                    if self.dead:
                        raise OSError("the worker template exited")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OSError("the worker template did not answer")
                    self._cv.wait(remaining)
                kind, pid, value = self._replies.pop(seq)
                self._waiting.discard(seq)
            if kind == b"E":
                clean_failure = True
                pid = 0
                raise OSError(value, "the worker template could not fork")
            stdin_file = os.fdopen(stdin_w, "wb", buffering=0)
            stdin_w = -1
            try:
                stdout_file = os.fdopen(stdout_r, "rb", buffering=0)
            except BaseException:
                stdin_file.close()
                raise
            stdout_r = -1
            return ForkedProc(self, pid, stdin_file, stdout_file)
        except BaseException:
            if not clean_failure:
                # A request that may still be in flight: its answer must not be taken for the
                # next one's (it is keyed by `seq`, and a late `P` is killed on arrival), and
                # nothing more is forked from a template that stopped answering.
                self.broken = True
                with self._cv:
                    self._waiting.discard(seq)
                    self._replies.pop(seq, None)
                if pid:
                    _killpg_or_kill(pid)
                    self.ack(pid)
            for fd in (stdin_w, stdout_r):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            raise
        finally:
            os.close(stdin_r)
            os.close(stdout_w)
            with self._cv:
                self._outstanding -= 1
                send = self._retire_due()
            if send:
                self._send_retire()

    def _retire_due(self) -> bool:
        """Caller holds the lock. True once, when a retire was asked for and no fork that was
        reserved before it is still on its way (the template refuses forks after `R`)."""
        if self.retire_requested and self._outstanding == 0 and not self._retire_sent:
            self._retire_sent = True
            return True
        return False

    def _send_retire(self) -> None:
        try:
            self._send(_REQUEST.pack(_RETIRE, 0, 0))
        except (OSError, ValueError):
            pass

    def retire(self) -> None:
        """No more forks from this template; it exits once its last worker has (the request goes
        out when the forks already reserved on it have been sent)."""
        with self._cv:
            self.retire_requested = True
            send = self._retire_due()
        if send:
            self._send_retire()

    def closable(self) -> bool:
        """A retired template may be closed once it has exited *and* everything it said has been
        read: closing earlier would drop the last worker's exit frame."""
        if self.proc.poll() is None:
            return False
        if self.eof:
            return True
        now = time.monotonic()
        if self._exited_seen is None:
            self._exited_seen = now
        return now - self._exited_seen >= _EOF_GRACE_SECONDS

    def close(self) -> None:
        """Stop the template and every worker it still has (their exit status is lost, but they
        are not left running)."""
        with self._cv:
            if self._closing:
                return
            self._closing = True
        # Workers first, while the template still holds their pids.
        for pid in self._unresolved():
            _killpg_or_kill(pid)
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the reader
        except OSError:
            pass
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass
        with self._cv:
            self.dead = True
            self._cv.notify_all()
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
        self.fallbacks = (
            0  # worker starts that wanted a fork and got a fresh interpreter
        )
        self.last_fallback = ""
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
                except Exception:  # noqa: BLE001, S110 - the first spawn will try again
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
            template.reserve()
        # Outside the lock: one slow or stuck fork must not stall every other start.
        return template.fork_worker(stderr_fd)

    def note_fallback(self, exc: BaseException) -> None:
        """A worker start fell back from the template to a fresh interpreter."""
        import logging

        self.fallbacks += 1
        self.last_fallback = f"{type(exc).__name__}: {exc}"
        logging.getLogger("pydeno").warning(
            "fork-template worker start failed, starting a fresh interpreter instead (%s)",
            self.last_fallback,
        )

    def reap_retired(self) -> None:
        """Close retired templates that have exited and been read to the end. Never blocks
        (skips if busy)."""
        if not self._retired or not self._lock.acquire(blocking=False):
            return
        try:
            self._reap_retired()
        finally:
            self._lock.release()

    def _reap_retired(self) -> None:
        keep = []
        for template in self._retired:
            if template.closable():
                template.close()
            else:
                keep.append(template)
        self._retired = keep

    def shutdown(self) -> None:
        """At interpreter exit: nothing outlives this process, workers included. Registered when
        this module is imported, so it runs after every other exit hook of the package."""
        with self._lock:
            everything = [self._current, *self._retired]
            self._current, self._retired = None, []
        for template in everything:
            if template is not None:
                template.close()

    def forget(self) -> None:
        """In a forked child of the host: the sockets belong to the host. Close our copies (a
        template only exits on EOF when *every* copy is closed) and say nothing to it. The mode
        stays on: the child starts a template of its own when it first needs one."""
        self._lock = threading.Lock()
        for template in [self._current, *self._retired]:
            if template is not None:
                try:
                    template.sock.close()
                except OSError:
                    pass
        self._current, self._retired = None, []


MANAGER = TemplateManager()
atexit.register(MANAGER.shutdown)


def enable_fork_template(
    *,
    max_forks: int = DEFAULT_MAX_FORKS,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> None:
    """Start sandboxed workers by forking a prepared template instead of launching a fresh
    interpreter. Opt-in, Linux only; see the module docstring for the trade-off (workers from one
    template share an address-space layout, V8's code image and the stack canary) and `docs/guides/advanced/fork-template.md`.

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
