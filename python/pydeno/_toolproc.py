"""`ToolProcess`: run host tools in a supervised child process (experimental, off by default).

A tool bug, a segfault in a C extension a tool calls, or a runaway tool is then not in the
parent's address space. The parent starts the tool host (`_toolhost`) like a worker
(`start_new_session`, an empty environment unless the caller passes one), forwards each call over
the framed wire (`_wire`), enforces a deadline, a result-size cap, a memory ceiling and a CPU cap,
kills the whole process group when one is broken, and starts a new tool host for the next call.

Without `sandbox=` this is crash isolation and resource limits. It is NOT a sandbox: the tool host
runs with the authority of the user that started the parent.

`ToolProcess.tool(...)` returns an ordinary asynchronous callable, so it goes wherever a host tool
goes (`bind_function`, `bind_object`, `ToolBridge`, `AgentSandbox(tools=...)`) and every path that
treats tools as callables (budgets, `tool_timeout`, the agent journal and its replay,
`redact_host_errors`) works unchanged: a call either returns, or raises an exception with a class
name and a message, which is what those paths record.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import functools
import inspect
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import _sandbox, _wire
from ._isolated import _PACKAGE_PARENT, tool_timeout_error
from ._limits import limit_int, limit_seconds

__all__ = [
    "ToolProcess",
    "ToolProcessDied",
    "ToolProcessError",
    "ToolProcessStartError",
    "ToolResultTooLarge",
]

DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_MAX_RESULT_BYTES = 1 << 20
# How often the supervisor looks at deadlines, resident memory and CPU time.
_WATCH_SECONDS = 0.025
_BOOT = (
    f"import sys; sys.path.append({_PACKAGE_PARENT!r}); "
    "from pydeno._toolhost import main; main()"
)
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_SPEC = re.compile(rf"{_NAME}(?:\.{_NAME})*:{_NAME}(?:\.{_NAME})*")
_SAFE_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_MAX_REMOTE_MESSAGE = 64 * 1024


class ToolProcessError(RuntimeError):
    """Base class of what a `ToolProcess` itself raises. The message is pydeno's own text (it holds
    nothing the tool produced), so `redact_host_errors` leaves it alone; the flag is on the class,
    because asyncio rebuilds some exceptions that cross a thread."""

    _pydeno_public = True


class ToolProcessDied(ToolProcessError):
    """The tool host died during the call (a crash, a signal, a limit, a close). The call may
    already have had side effects: treat it as failed, not as not-run. Agent journals record it as
    that failure and replay it without running the tool."""


class ToolResultTooLarge(ToolProcessError):
    """The tool's result, encoded, was over `max_result_bytes`. Nothing was sent to the parent."""


class ToolProcessStartError(ToolProcessError):
    """The tool host could not start (an import of a tool failed, or `sandbox="require"` found a
    layer missing). The message has the reason, so it is redacted like any tool error."""

    _pydeno_public = False


_REMOTE_CLASSES: dict[str, type[Exception]] = {}


def _remote_class(name: object) -> type[Exception]:
    """A plain exception class named like the one the tool raised. The guest learns a host error's
    class name and message, nothing else, so this reproduces what a tool run in the parent shows
    (and not the builtin of that name: `str(KeyError(m))` is `repr(m)`, not `m`)."""
    if not isinstance(name, str) or not _SAFE_CLASS_NAME.fullmatch(name):
        name = "Exception"
    cls = _REMOTE_CLASSES.get(name)
    if cls is None:
        cls = _REMOTE_CLASSES.setdefault(name, type(name, (Exception,), {}))
    return cls


class _Call:
    __slots__ = ("cpu_start", "deadline", "future")

    def __init__(self, deadline: float | None, cpu_start: float | None) -> None:
        self.future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self.deadline = deadline
        self.cpu_start = cpu_start

    def fail(self, exc: BaseException) -> None:
        try:
            self.future.set_exception(exc)
        except concurrent.futures.InvalidStateError:  # the caller cancelled it
            pass

    def succeed(self, value: Any) -> None:
        try:
            self.future.set_result(value)
        except concurrent.futures.InvalidStateError:
            pass


_LIVE: set[_Child] = set()
_LIVE_LOCK = threading.Lock()


class _Child:
    """One tool host process, its pipes, and the two threads that serve it (a reader of its frames
    and a supervisor of its limits). Self-contained: it holds no reference to the `ToolProcess`, so
    that a forgotten `ToolProcess` can be collected (and its child killed by the finalizer)."""

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        init: dict[str, Any],
        max_memory: int | None,
        cpu_seconds: float | None,
        max_frame: int,
    ) -> None:
        self.owner_pid = os.getpid()
        self.max_memory = max_memory
        self.cpu_seconds = cpu_seconds
        self.lock = threading.Lock()
        self.pending: dict[int, _Call] = {}
        self.next_cid = 1
        self.dead = False
        self.reason: str | None = None
        self.start_error: str | None = None
        self.sandbox = "none"
        self.ready = threading.Event()
        self.finished = threading.Event()
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed when the child ends
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [sys.executable, "-I", "-c", _BOOT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                env=dict(env),
                close_fds=True,
                start_new_session=True,  # so a kill takes any stray child of a tool with it
                bufsize=0,
            )
        except BaseException:
            self._stderr.close()
            raise
        stdin_fd = self.proc.stdin.fileno()  # type: ignore[union-attr]
        os.set_blocking(stdin_fd, False)
        self.writer = _wire.FrameWriter(stdin_fd, stall_timeout=5.0)
        self.reader = _wire.FrameReader(
            self.proc.stdout.fileno(),  # type: ignore[union-attr]
            max_frame=max_frame,
        )
        init = {**init, "t": "init", "parent": os.getpid()}
        try:
            self.writer.send(init)
        except BaseException:
            self.kill("tool process failed to start")
            self._cleanup()
            raise
        with _LIVE_LOCK:
            _LIVE.add(self)
        self.stop = threading.Event()
        threading.Thread(
            target=self._read_loop, name="pydeno-toolproc-reader", daemon=True
        ).start()
        threading.Thread(
            target=self._watch_loop, name="pydeno-toolproc-watch", daemon=True
        ).start()

    # -- calls --------------------------------------------------------------

    def submit(
        self, payload_for: Callable[[int], bytes], deadline: float | None
    ) -> _Call | None:
        """Register a call and send it. None: this child is already dead (the caller starts
        another). `payload_for(cid)` is encoded before anything is registered."""
        cpu_start = None
        if self.cpu_seconds is not None:
            cpu_start = _sandbox.usage(self.proc.pid)[1]
        with self.lock:
            if self.dead or self.reason is not None:
                return None
            cid = self.next_cid
            self.next_cid += 1
            call = self.pending[cid] = _Call(deadline, cpu_start)
        try:
            self.writer.send_encoded(payload_for(cid))
        except (OSError, _wire.WireError) as exc:
            # Dead or not reading: the reader sees the death and fails the call (and any other).
            self.kill(
                "the tool process stopped reading its calls"
                if isinstance(exc, _wire.StalledWrite)
                else "the tool process died before it could be sent the call"
            )
        return call

    def expire(self, cids: list[int]) -> None:
        """Calls that outlasted their deadline: each gets the guest-visible timeout, then the
        process dies and every other call in flight with it fails as a death."""
        with self.lock:
            calls = [c for c in (self.pending.pop(cid, None) for cid in cids) if c]
        for call in calls:
            call.fail(tool_timeout_error())
        self.kill("the tool process was killed: a call outlasted its deadline")

    # -- supervision --------------------------------------------------------

    def kill(self, reason: str) -> None:
        with self.lock:
            if self.reason is None:
                self.reason = reason
        _terminate(self.proc)

    def _watch_loop(self) -> None:
        pid = self.proc.pid
        while not self.stop.wait(_WATCH_SECONDS):
            now = time.monotonic()
            with self.lock:
                calls = list(self.pending.items())
            late = [
                cid
                for cid, call in calls
                if call.deadline is not None and now >= call.deadline
            ]
            if late:
                self.expire(late)
            if self.max_memory is None and self.cpu_seconds is None:
                continue
            rss, cpu, _ = _sandbox.usage(pid)
            if (
                self.max_memory is not None
                and rss is not None
                and rss > self.max_memory
            ):
                self.kill(
                    f"the tool process was killed: it used {rss} bytes, over "
                    f"max_memory={self.max_memory}"
                )
            elif self.cpu_seconds is not None and cpu is not None:
                for _, call in calls:
                    if (
                        call.cpu_start is not None
                        and cpu - call.cpu_start > self.cpu_seconds
                    ):
                        self.kill(
                            "the tool process was killed: it used more than "
                            f"cpu_seconds={self.cpu_seconds:g}s of CPU in one call"
                        )
                        break

    def _read_loop(self) -> None:
        try:
            while True:
                try:
                    payload = self.reader.read(deadline=time.monotonic() + 0.25)
                except TimeoutError:
                    if (
                        self.reason is not None
                    ):  # killed, and something still holds the pipe
                        break
                    continue
                except (_wire.WireError, OSError) as exc:
                    if self.reason is None:
                        self.reason = f"the tool process broke the protocol ({exc})"
                    break
                if payload is None:
                    break
                try:
                    message = _wire.loads_decoded(payload)
                except _wire.WireError:
                    self.kill("the tool process sent a frame that is not valid")
                    break
                self._on_frame(message)
        finally:
            self._finish()

    def _on_frame(self, message: dict[str, Any]) -> None:
        kind, cid = message.get("t"), message.get("cid")
        if kind == "ready":
            applied = message.get("sandbox")
            self.sandbox = applied if isinstance(applied, str) else "none"
            self.ready.set()
            return
        if kind == "error" and cid == 0:
            etype, text = message.get("etype"), message.get("msg")
            self.start_error = (
                f"{etype if isinstance(etype, str) else 'error'}: "
                f"{text if isinstance(text, str) else ''}"[:_MAX_REMOTE_MESSAGE]
            )
            return
        with self.lock:
            call = self.pending.pop(cid, None) if isinstance(cid, int) else None
        if call is None:
            return  # a call that was given up on, or a frame for nothing
        if kind == "result":
            call.succeed(message.get("v"))
        elif kind == "error":
            text = message.get("msg")
            text = text if isinstance(text, str) else ""
            if len(text) > _MAX_REMOTE_MESSAGE:
                text = text[:_MAX_REMOTE_MESSAGE] + "..."
            call.fail(_remote_class(message.get("etype"))(text))
        elif kind == "toolarge":
            size = message.get("size")
            call.fail(
                ToolResultTooLarge(
                    f"the tool's result is {size if isinstance(size, int) else 'too many'} "
                    "bytes encoded, over max_result_bytes"
                )
            )
        else:
            call.fail(
                ToolProcessDied("the tool process sent a frame that is not valid")
            )
            self.kill("the tool process sent a frame that is not valid")

    def _describe_death(self) -> str:
        if self.reason is not None:
            return self.reason
        code = self.proc.returncode
        if self.start_error is not None:
            return f"the tool process failed to start: {self.start_error}"
        if code is not None and code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = str(-code)
            return f"the tool process died (killed by signal {name})"
        if code:
            return f"the tool process died (exit code {code})"
        return "the tool process exited"

    def _finish(self) -> None:
        """The child is gone (or being got rid of): fail what is in flight, release everything.
        Runs once, on the reader thread."""
        _terminate(self.proc)
        message = self._describe_death()
        started_badly = self.start_error is not None and self.reason is None
        with self.lock:
            self.dead = True
            calls = list(self.pending.values())
            self.pending.clear()
        for call in calls:
            call.fail(
                ToolProcessStartError(message)
                if started_badly
                else ToolProcessDied(message)
            )
        self.stop.set()
        self.ready.set()
        self._cleanup()
        with _LIVE_LOCK:
            _LIVE.discard(self)
        self.finished.set()

    def _cleanup(self) -> None:
        self.writer.invalidate()
        self.reader.invalidate()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            self._stderr.close()
        except OSError:
            pass

    def drop_inherited(self) -> None:
        """In a `fork()`ed child: let go of this process's copies of the pipes, without signalling
        or waiting on a tool host that belongs to the parent."""
        self.dead = True
        self.stop.set()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._stderr.close()
        except (OSError, ValueError):
            pass


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the tool host's whole process group and reap it. Safe on one that is gone."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL does not time out
        pass


def _kill_box(box: list[_Child | None], owner_pid: int) -> None:
    child = box[0]
    box[0] = None
    if child is None:
        return
    if os.getpid() != owner_pid:
        child.drop_inherited()
        return
    child.kill("the tool process was closed")


def _kill_all_at_exit() -> None:
    with _LIVE_LOCK:
        children = list(_LIVE)
    for child in children:
        if child.owner_pid == os.getpid():
            _terminate(child.proc)


def _forget_parents_children() -> None:
    global _LIVE_LOCK  # noqa: PLW0603
    _LIVE_LOCK = threading.Lock()
    for child in list(_LIVE):
        child.drop_inherited()
    _LIVE.clear()


atexit.register(_kill_all_at_exit)
os.register_at_fork(after_in_child=_forget_parents_children)


class ToolProcess:
    """A supervised child process that runs host tools (**experimental**; off unless you use it).

    ```python
    tools = ToolProcess(call_timeout=10, max_memory=256 * 2**20)
    rt.bind_function("lookup", tools.tool("myapp.tools:lookup"))
    ```

    Tools are named ``"module:function"`` (or an importable module-level function; closures,
    lambdas and ``__main__`` functions are refused with a clear error), because another process
    cannot receive the parent's closures. Whatever state a tool keeps lives in the tool host and
    survives between calls, but not a restart. The module must be importable by the tool host: it
    gets the parent's `sys.path` (or `path=`).

    From the guest every tool of a process is **asynchronous** (a Promise): the parent never blocks
    on the child. Calls run concurrently (async tools on one event loop, sync tools on up to 32
    threads). A tool host that dies takes its in-flight calls with it: each fails with
    `ToolProcessDied` and the next call starts a new one.

    **This is crash isolation and resource limits, not a sandbox**, unless you pass `sandbox=`.
    Without it the tool host runs with your user's full authority (files, network).

    Args:
        call_timeout: Most seconds one call may take, queueing and start-up included (default 60,
            `None` removes it). Past it the tool host is killed and the guest gets the same
            ``TimeoutError`` ("host function timed out") that `tool_timeout` gives.
        max_result_bytes: Cap on a result's encoded size (default 1 MiB), checked in the tool host
            before anything is sent; over it the call fails with `ToolResultTooLarge`.
        max_memory: Cap in bytes on the tool host's resident memory (sampled every 25 ms, the host
            is killed over it), plus a kernel ceiling (`RLIMIT_DATA`, Linux) just above it.
        cpu_seconds: Most CPU seconds the tool host may use while one call runs (user plus system,
            all threads, so concurrent calls count each other's). Over it the host is killed.
        env: The tool host's environment. Empty unless you pass one.
        path: The tool host's `sys.path` (default: the parent's).
        sandbox: ``None`` (default) for no confinement. ``"auto"`` or ``"require"`` applies the
            worker's OS sandbox (Landlock + seccomp on Linux, Seatbelt on macOS, an empty root) to
            the tool host: it then has no filesystem and no network, and every tool must be
            registered before the first call (they are imported before confinement). ``"require"``
            refuses to start when a layer is missing (`ToolProcessStartError`).
    """

    def __init__(
        self,
        *,
        call_timeout: float | None = DEFAULT_CALL_TIMEOUT,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        max_memory: int | None = None,
        cpu_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
        path: Sequence[str] | None = None,
        sandbox: str | None = None,
    ) -> None:
        if sandbox not in (None, "auto", "require"):
            raise ValueError("sandbox must be None, 'auto' or 'require'")
        self._call_timeout = limit_seconds("call_timeout", call_timeout)
        result_cap = limit_int("max_result_bytes", max_result_bytes, minimum=1)
        assert result_cap is not None
        # A reply is the result plus a few dozen bytes of framing: anything much beyond the cap is
        # the tool host lying about it.
        self._max_result_bytes = result_cap
        self._max_memory = limit_int("max_memory", max_memory, minimum=1)
        self._cpu_seconds = limit_seconds("cpu_seconds", cpu_seconds)
        if env is not None and not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise TypeError("env must map strings to strings")
        self._env: dict[str, str] = dict(env or {})
        self._path = (
            [os.path.abspath(p) for p in path]
            if path is not None
            else [os.path.abspath(p) for p in sys.path if p]
        )
        self._sandbox = sandbox
        self._specs: list[str] = []
        self._lock = threading.Lock()
        self._owner_pid = os.getpid()
        self._closed = False
        self._started_once = False
        self._box: list[_Child | None] = [None]
        #: How many tool hosts have been started (1 after the first call, +1 per restart).
        self.starts = 0
        self._finalizer = weakref.finalize(self, _kill_box, self._box, self._owner_pid)

    # -- registering tools ---------------------------------------------------

    def tool(self, target: str | Callable[..., Any]) -> Callable[..., Any]:
        """The host tool for `target`: a ``"module:function"`` string, or an importable
        module-level function. Returns an async callable to bind wherever a host function goes.
        A function keeps its name, docstring and signature (so catalogs and schemas describe it).
        """
        spec, template = _spec_of(target)
        with self._lock:
            if spec not in self._specs:
                if self._sandbox is not None and self._started_once:
                    raise RuntimeError(
                        "with sandbox set, register every tool before the first call: the tool "
                        "host imports them before it is confined"
                    )
                self._specs.append(spec)

        async def call(*args: Any) -> Any:
            return await self._call(spec, args)

        if template is not None:
            functools.update_wrapper(call, template)
        else:
            call.__name__ = call.__qualname__ = spec.rpartition(":")[2]
        return call

    # -- calling -------------------------------------------------------------

    async def _call(self, spec: str, args: tuple[Any, ...]) -> Any:
        call = self._submit(spec, args)
        return await asyncio.wrap_future(call.future)

    def _submit(self, spec: str, args: tuple[Any, ...]) -> _Call:
        def payload_for(cid: int) -> bytes:
            return _wire.dumps(
                {
                    "t": "call",
                    "cid": cid,
                    "tool": spec,
                    "args": [_wire.Enc(a) for a in args],
                }
            )

        # An argument that cannot cross is the caller's TypeError; nothing is registered or sent.
        payload_for(0)
        deadline = (
            None
            if self._call_timeout is None
            else time.monotonic() + self._call_timeout
        )
        for _ in range(3):
            child = self._child()
            call = child.submit(payload_for, deadline)
            if call is not None:
                return call
        raise ToolProcessDied("the tool process kept dying before it took the call")

    def _child(self) -> _Child:
        with self._lock:
            if self._closed:
                raise ToolProcessError("the tool process is closed")
            if os.getpid() != self._owner_pid:
                raise ToolProcessError(
                    "this ToolProcess belongs to another process (it was inherited by a fork)"
                )
            child = self._box[0]
            if child is not None and not child.dead and child.reason is None:
                return child
            self._started_once = True
            self.starts += 1
            child = self._box[0] = _Child(
                env=self._env,
                init={
                    "path": self._path,
                    "tools": list(self._specs),
                    "sandbox": self._sandbox,
                    "max_memory": self._max_memory,
                    "max_result_bytes": self._max_result_bytes,
                },
                max_memory=self._max_memory,
                cpu_seconds=self._cpu_seconds,
                max_frame=self._max_result_bytes + (256 << 10),
            )
            return child

    # -- lifecycle -----------------------------------------------------------

    def start(self, timeout: float = 30.0) -> ToolProcess:
        """Start the tool host now and wait until it is ready (and confined, with `sandbox=`).
        Raises `ToolProcessStartError` if it cannot start. Optional: the first call starts it."""
        child = self._child()
        if not child.ready.wait(timeout):
            raise ToolProcessStartError("the tool process did not become ready in time")
        if child.dead:
            raise ToolProcessStartError(child._describe_death())  # noqa: SLF001
        return self

    @property
    def pid(self) -> int | None:
        """The pid of the running tool host, or None (not started, or it died)."""
        child = self._box[0]
        return child.proc.pid if child is not None and not child.dead else None

    @property
    def sandbox(self) -> str:
        """The OS layers the running tool host reports (``"none"`` without `sandbox=`)."""
        child = self._box[0]
        return child.sandbox if child is not None else "none"

    def close(self) -> None:
        """Kill the tool host (in-flight calls fail with `ToolProcessDied`) and release its pipes
        and threads. Later calls raise `ToolProcessError`. Idempotent."""
        with self._lock:
            self._closed = True
            child = self._box[0]
        if child is None:
            return
        if os.getpid() != self._owner_pid:
            child.drop_inherited()
            self._box[0] = None
            return
        child.kill("the tool process was closed")
        child.finished.wait(10)
        self._box[0] = None

    def __enter__(self) -> ToolProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _spec_of(target: object) -> tuple[str, Callable[..., Any] | None]:
    """`'module:function'` for a tool, and the function itself when one was given (to describe it).
    Anything another process could not import is refused, saying why."""
    if isinstance(target, str):
        if not _SPEC.fullmatch(target):
            raise ValueError(
                f"{target!r} is not a tool name: write 'module:function' "
                "(for example 'myapp.tools:lookup')"
            )
        if target.partition(":")[0] == "__main__":
            raise ValueError(
                "'__main__' cannot be imported by the tool host: put the function in a module"
            )
        return target, None
    if not inspect.isfunction(target):
        raise TypeError(
            "a tool process runs importable module-level functions, named 'module:function'; "
            f"got {type(target).__name__} (a bound method, partial or callable object carries "
            "state from this process, which the tool host cannot receive)"
        )
    name, module, qualname = target.__name__, target.__module__, target.__qualname__
    if name == "<lambda>" or "<locals>" in qualname:
        raise TypeError(
            f"{qualname!r} is a {'lambda' if name == '<lambda>' else 'closure'}: a tool process "
            "cannot receive it, it only runs functions it can import. Move it to a module and "
            "pass 'module:function'"
        )
    if module == "__main__":
        raise TypeError(
            f"{qualname!r} is defined in __main__, which the tool host cannot import: move it "
            "to a module and pass 'module:function'"
        )
    found: Any = sys.modules.get(module)
    for part in qualname.split("."):
        found = getattr(found, part, None)
    if found is not target:
        raise TypeError(
            f"{module}.{qualname} is not the function that module exports under that name "
            "(a decorator replaced it?), so the tool host would import something else; "
            "pass 'module:function' naming the one to run"
        )
    return f"{module}:{qualname}", target
