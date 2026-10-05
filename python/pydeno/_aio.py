"""`AsyncIsolatedRuntime`: an `IsolatedRuntime` driven by asyncio instead of threads.

`IsolatedRuntime` is built for a thread: each runtime has an idle-watchdog thread, `eval_async`
hops to a command thread the runtime owns, and construction, `bind_function` and `close` block.
That is right for a script and wrong for an asyncio service with thousands of sessions, where a
thread per session is thousands of threads and every blocking call stalls every other session.

This runtime keeps the same worker, the same wire format and every security property, and moves
the parent side onto the caller's event loop:

* the worker's pipes are asyncio transports; a command is a coroutine on the caller's loop and
  needs no thread;
* one **supervisor task per event loop** enforces the limits of every live runtime on that loop
  (hard deadline, `max_host_wait`, CPU cap, memory ceiling, thread cap, idle CPU), sampling the
  workers' memory/CPU/thread counts in batches on one shared, bounded thread pool, never on the loop;
* the only threads are three small shared pools: one for those samples, one for spawning and
  reaping workers (the blocking `fork`/`exec`), one for synchronous host functions (so a slow one
  never blocks the loop). None of them grows with the number of runtimes.

The worker is untrusted exactly as in `IsolatedRuntime`: the frame cap is checked as soon as a
header arrives, before the payload is buffered; every frame is decoded by `_wire.loads_decoded`
under its size, depth and node limits; anything outside the protocol kills the worker.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import contextlib
import contextvars
import errno
import inspect
import itertools
import os
import signal
import struct
import sys
import threading
import time
import warnings
import weakref
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from . import _compat, _isolated, _sandbox, _wasm, _wire
from ._isolated import (
    DEFAULT_MAX_MEMORY,
    DEFAULT_REQUEST_TIMEOUT,
    IsolatedRuntime,
    WorkerCrashed,
    _CONFIG_KEYS,
    _CPU_CAP_FACTOR,
    _check_wire_limits,
    _DEFAULT,
    _HostCallBudgetExceeded,
    _IDLE_CHECK_SECONDS,
    _IDLE_CPU_LIMIT_SECONDS,
    _MAX_WORKER_THREADS,
    _NATIVE_FRAME,
    _Pump,
    _REVOKED_MEMORY,
    _STDERR_TAIL_BYTES,
    _UNSUPPORTED_CONFIG,
    _await,
    _checked_console,
    _checked_specifiers,
    _clean,
    _clock_ms,
    _error_reply,
    _is_token,
    _limit_int,
    _limit_seconds,
    _revoked_handler,
    _session_options,
    _start_worker,
    _strict_eval_setting,
    _terminate_process,
    _worker_v8_flags,
)
from ._pydeno import RuntimeConfig, RuntimeTimeout

__all__ = ["AsyncIsolatedRuntime"]

_HEADER = struct.Struct("<I")

# Frames (in) and messages (out) larger than this are decoded / encoded on a thread, not on the loop.
_OFFLOAD_BYTES = 64 * 1024
# How often the supervisor wakes. Deadlines are checked every tick; a runtime with a command in
# flight is sampled every tick (the sync runtime samples memory every 50 ms), an idle one every
# `_IDLE_CHECK_SECONDS`, as the sync runtime's idle watchdog does.
_TICK_SECONDS = 0.05
_SAMPLE_CHUNK = 64
# Stop reading a worker's pipe once this much is buffered and unconsumed (a hostile worker flooding
# frames between commands). Above one maximal frame, so a single legitimate frame always fits.
_READ_HIGH_WATER = 2 * _wire.MAX_FRAME_BYTES + 8
_READ_LOW_WATER = _wire.MAX_FRAME_BYTES
# Payload bytes do not account for deque entries and bytes objects (empty frames cost zero).
# As with the byte watermark, the current transport delivery may overshoot this threshold.
# CPython's pipe transport reads at most 256 KiB per delivery: up to 65,536 empty frames
# (roughly 0.5 MiB of deque entries) beyond the point where a pause becomes necessary.
_READ_HIGH_FRAMES = 1024
_READ_LOW_FRAMES = 512
_HANDSHAKE_SECONDS = 30.0
_CLOSE_GRACE_SECONDS = 1.0
# Frames already buffered are handled without suspending; yield to the loop every so many so that
# a worker flooding cheap frames cannot monopolise it.
_YIELD_EVERY = 32
DEFAULT_HANDLER_THREADS = 32
# Worker start-ups in flight per event loop. A start-up is ~55 ms of CPU (Python, imports, the
# sandbox, V8); a burst of 64 at once oversubscribes the machine and the loop itself then waits
# for a CPU (measured: 35 ms worst heartbeat lag for 64 at once, 4 ms with 4 at a time, for the
# same total time). Creation throughput is CPU-bound either way.
_START_SLOTS = max(2, (os.cpu_count() or 4) // 2)

# Which runtimes' host functions the current context is running inside (by serial number). A host
# function may not call back into the runtime that is waiting for it (that would deadlock on the
# command slot); calling a *different* runtime is fine.
_IN_CALL_OF: contextvars.ContextVar[frozenset[int]] = contextvars.ContextVar(
    "pydeno_aio_in_call_of", default=frozenset()
)
_SERIALS = itertools.count(1)

_Sample = tuple[int | None, float | None, int | None]  # rss bytes, cpu seconds, threads
_Verdict = tuple[type[Exception], str]


# ---------------------------------------------------------------------------
# shared thread pools
# ---------------------------------------------------------------------------

_POOL_SIZES = {
    "metrics": 1,  # /proc and proc_pidinfo reads, in batches
    "io": 2,  # fork/exec of workers, refilling the spare, stderr tails
    "codec": 1,  # decoding / encoding frames over _OFFLOAD_BYTES
    "handlers": DEFAULT_HANDLER_THREADS,  # synchronous host functions
}
_POOLS: dict[str, ThreadPoolExecutor] = {}
_POOLS_LOCK = threading.Lock()


def _pool(name: str) -> ThreadPoolExecutor:
    pool = _POOLS.get(name)
    if pool is None:
        with _POOLS_LOCK:
            pool = _POOLS.get(name)
            if pool is None:
                pool = _POOLS[name] = ThreadPoolExecutor(
                    _POOL_SIZES[name], thread_name_prefix=f"pydeno-aio-{name}"
                )
    return pool


def _sample_many(pids: list[int]) -> list[_Sample]:
    """Runs on the metrics thread: one batch of blocking reads, never on the loop."""
    return [_sandbox.usage(p) for p in pids]  # (rss, cpu, threads) from one read each


# ---------------------------------------------------------------------------
# reaping
# ---------------------------------------------------------------------------

# Workers that were killed but not yet waited for. Reaped (non-blocking `poll`) by every supervisor
# tick, by `close()`, and with a short blocking wait when a loop shuts down or the interpreter exits.
_ZOMBIES: set[Any] = set()
_ZOMBIES_LOCK = threading.Lock()


def _signal_group(proc: Any) -> None:
    """SIGKILL the worker's process group, if the worker has not been reaped yet. (Once reaped,
    its pid may belong to someone else, so it is never signalled again.)"""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass


def _bury(proc: Any) -> None:
    with _ZOMBIES_LOCK:
        _ZOMBIES.add(proc)


def _reap_zombies(block: float = 0.0) -> None:
    with _ZOMBIES_LOCK:
        procs = list(_ZOMBIES)
    for proc in procs:
        if block:
            try:
                proc.wait(timeout=block)
            except Exception:  # noqa: BLE001, S110
                pass
        if proc.poll() is not None:
            with _ZOMBIES_LOCK:
                _ZOMBIES.discard(proc)


def _finalize_worker(
    proc: Any,
    stderr: Any,
    loop: asyncio.AbstractEventLoop,
    transports: tuple[Any, ...],
) -> None:
    """A runtime that was dropped without `close()`: kill its worker without blocking (it may run
    on the loop, from the garbage collector). The zombie is reaped by the next supervisor tick."""
    _signal_group(proc)
    _bury(proc)
    for transport in transports:
        try:
            loop.call_soon_threadsafe(transport.close)
        except RuntimeError:  # the loop is closed
            pass
    try:
        stderr.close()
    except (OSError, ValueError):
        pass


def _stderr_tail(stderr: Any) -> str:
    try:
        stderr.seek(0, os.SEEK_END)
        size = stderr.tell()
        stderr.seek(max(0, size - _STDERR_TAIL_BYTES))
        return stderr.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# the prewarmed spare, shared with IsolatedRuntime
# ---------------------------------------------------------------------------

_REFILLING = False


def _spawn(python: str | None, prewarm: bool) -> tuple[Any, Any]:
    if prewarm and python is None:
        return _isolated._take_worker()  # noqa: SLF001
    return _start_worker(python or sys.executable)


def _refill_spare() -> None:
    """Start one spare worker in the background, unless one is already being started.
    (`IsolatedRuntime` starts a thread per refill; a burst of 50 async creations would then spawn
    50 spares to keep one.)"""
    global _REFILLING  # noqa: PLW0603
    if _REFILLING:
        return
    _REFILLING = True

    def fill() -> None:
        global _REFILLING  # noqa: PLW0603
        try:
            with _isolated._SPARE_LOCK:  # noqa: SLF001
                if _isolated._SPARE is not None:  # noqa: SLF001
                    return
            try:
                worker = _start_worker(sys.executable)
            except OSError:
                return
            with _isolated._SPARE_LOCK:  # noqa: SLF001
                if _isolated._SPARE is None:  # noqa: SLF001
                    _isolated._SPARE = worker  # noqa: SLF001
                    return
            _terminate_process(*worker)
        finally:
            _REFILLING = False

    try:
        _pool("io").submit(fill)
    except RuntimeError:  # interpreter shutting down
        _REFILLING = False


# ---------------------------------------------------------------------------
# framing over asyncio pipes
# ---------------------------------------------------------------------------


def _frame(message: dict[str, Any]) -> bytes:
    payload = _wire.dumps(message)
    return _HEADER.pack(len(payload)) + payload


def _big_message(message: dict[str, Any]) -> bool:
    return any(
        isinstance(v, (str, bytes)) and len(v) > _OFFLOAD_BYTES
        for v in message.values()
    )


def _big_value(value: Any) -> bool:
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        return len(value) > _OFFLOAD_BYTES
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return len(value) > 1024
    return False


class _FrameReader(asyncio.Protocol):
    """Splits the worker's stdout into frames as bytes arrive.

    The length header is the worker's claim: it is checked against the cap the moment its four
    bytes are in, so an oversized frame is refused before any of its payload is buffered."""

    def __init__(self, max_frame: int = _wire.MAX_FRAME_BYTES) -> None:
        self._max = max_frame
        self._buf = bytearray()
        self.frames: collections.deque[bytes] = collections.deque()
        self._queued = 0
        self.error: str | None = None
        self.eof = False
        self._paused = False
        self._waiter: asyncio.Future[None] | None = None
        self._transport: asyncio.ReadTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        if self.error is not None:
            return
        buf = self._buf
        buf += data
        while len(buf) >= _HEADER.size:
            (length,) = _HEADER.unpack_from(buf)
            if length > self._max:
                self.error = f"frame of {length} bytes exceeds the {self._max} byte cap"
                buf.clear()
                self._pause()
                break
            end = _HEADER.size + length
            if len(buf) < end:
                break
            self.frames.append(bytes(buf[_HEADER.size : end]))
            self._queued += length
            del buf[:end]
        if (
            self._queued + len(buf) > _READ_HIGH_WATER
            or len(self.frames) >= _READ_HIGH_FRAMES
        ):
            self._pause()
        self.wake()

    def eof_received(self) -> None:
        self.eof = True
        self.wake()

    def connection_lost(self, exc: Exception | None) -> None:
        self.eof = True
        self.wake()

    def partial(self) -> bool:
        return bool(self._buf)

    def pop(self) -> bytes:
        payload = self.frames.popleft()
        self._queued -= len(payload)
        if (
            self._paused
            and self.error is None
            and (
                not self.frames  # an unfinished frame needs input when nothing can be popped
                or (
                    self._queued + len(self._buf) < _READ_LOW_WATER
                    and len(self.frames) < _READ_LOW_FRAMES
                )
            )
        ):
            self._paused = False
            if self._transport is not None:
                self._transport.resume_reading()
        return payload

    def _pause(self) -> None:
        if not self._paused and self._transport is not None:
            self._paused = True
            self._transport.pause_reading()

    def wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def wait(self) -> None:
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None


class _Writer(asyncio.BaseProtocol):
    """Flow control for the worker's stdin: `drain` waits while the transport's buffer is full."""

    def __init__(self) -> None:
        self.paused = False
        self._waiters: collections.deque[asyncio.Future[None]] = collections.deque()

    def pause_writing(self) -> None:
        self.paused = True

    def resume_writing(self) -> None:
        self.paused = False
        self._release()

    def connection_lost(self, exc: Exception | None) -> None:
        self.paused = False
        self._release()

    def _release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)

    async def drain(self) -> None:
        if not self.paused:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)


# ---------------------------------------------------------------------------
# the per-loop supervisor
# ---------------------------------------------------------------------------


class _Supervisor:
    """Enforces the limits of every live runtime on one event loop, from one task.

    Replaces `IsolatedRuntime`'s idle-watchdog thread *and* the clock checks its pump makes. Holds
    runtimes weakly, so a runtime dropped without `close()` can still be collected (its finalizer
    kills the worker). Stops when no runtime is left; the next runtime starts it again."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.runtimes: weakref.WeakSet[AsyncIsolatedRuntime] = weakref.WeakSet()
        self.task: asyncio.Task[None] | None = None
        self._pending: list[tuple[int, asyncio.Future[_Sample | None]]] = []
        self._sampling = False
        self.start_slots = asyncio.Semaphore(_START_SLOTS)

    # -- sampling ----------------------------------------------------------

    def sample(self, pid: int) -> asyncio.Future[_Sample | None]:
        """Memory, CPU and thread count of `pid`, read on the metrics thread. Requests that arrive
        together go out as one batch."""
        fut: asyncio.Future[_Sample | None] = self.loop.create_future()
        self._pending.append((pid, fut))
        self._kick()
        return fut

    def _kick(self) -> None:
        if self._sampling or not self._pending:
            return
        batch = self._pending[:_SAMPLE_CHUNK]
        del self._pending[:_SAMPLE_CHUNK]
        self._sampling = True
        loop = self.loop
        try:
            job = _pool("metrics").submit(_sample_many, [pid for pid, _ in batch])
        except RuntimeError:  # interpreter shutting down
            self._sampling = False
            for _, fut in batch:
                if not fut.done():
                    fut.set_result(None)
            return

        def done(job: Any) -> None:
            try:
                loop.call_soon_threadsafe(self._finish, batch, job)
            except RuntimeError:  # the loop has closed
                pass

        job.add_done_callback(done)

    def _finish(
        self, batch: list[tuple[int, asyncio.Future[_Sample | None]]], job: Any
    ) -> None:
        self._sampling = False
        try:
            results: list[_Sample | None] = job.result()
        except Exception:  # noqa: BLE001
            results = [None] * len(batch)
        for (_, fut), result in zip(batch, results, strict=True):
            if not fut.done():
                fut.set_result(result)
        self._kick()

    # -- the task ------------------------------------------------------------

    def add(self, rt: AsyncIsolatedRuntime) -> None:
        self.runtimes.add(rt)
        self.ensure_running()

    def ensure_running(self) -> None:
        """Start the task if it is not running: for a new runtime, or for a killed worker that
        must be reaped (a failed or cancelled start has no runtime to supervise, but a zombie)."""
        if self.loop.is_closed():
            return
        _SUPERVISORS.setdefault(self.loop, self)
        if self.task is None or self.task.done():
            # An empty context: the supervisor must not carry the creating task's contextvars. A
            # task copies the context it is created in, so create it inside an empty one
            # (`create_task(context=...)` needs Python 3.11; this supports 3.10).
            try:
                self.task = contextvars.Context().run(
                    self.loop.create_task, self._run(), name="pydeno-aio-supervisor"
                )
            except RuntimeError:  # a loop on its way down; atexit reaps what is left
                pass

    async def _tick(self) -> bool:
        """One pass over this loop's runtimes; False if none is live.

        A method of its own so that no runtime stays referenced from the supervisor's frame
        between ticks (a loop variable would keep the last one alive, and a runtime dropped
        without `close()` would then never be collected, and its worker never killed)."""
        live = [rt for rt in list(self.runtimes) if not rt._closed]  # noqa: SLF001
        if not live:
            return False
        now = time.monotonic()
        due: list[tuple[AsyncIsolatedRuntime, int]] = []
        for rt in live:
            rt._check_clock()  # noqa: SLF001
            if rt._closed:  # noqa: SLF001
                continue
            if (
                rt._cmd is not None  # noqa: SLF001
                or now - rt._last_idle_sample >= _IDLE_CHECK_SECONDS  # noqa: SLF001
            ):
                if rt._cmd is None:  # noqa: SLF001
                    rt._last_idle_sample = now  # noqa: SLF001
                due.append((rt, rt._gen))  # noqa: SLF001
        del live
        if due:
            samples = await asyncio.gather(
                *(self.sample(rt._proc.pid) for rt, _ in due)  # noqa: SLF001
            )
            for (rt, gen), sample in zip(due, samples, strict=True):
                rt._apply_sample(sample, gen)  # noqa: SLF001
        return True

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(_TICK_SECONDS)
                _reap_zombies()
                if not await self._tick():
                    with _ZOMBIES_LOCK:
                        idle = not _ZOMBIES
                    if idle:
                        return
        except asyncio.CancelledError:
            # The loop is shutting down: its runtimes cannot be used any more. Kill them and reap
            # every worker, so nothing outlives the loop.
            for rt in list(self.runtimes):
                rt._kill((WorkerCrashed, "the event loop shut down"))  # noqa: SLF001
            _reap_zombies(block=2.0)
            raise
        finally:
            if _SUPERVISORS.get(self.loop) is self:
                del _SUPERVISORS[self.loop]


_SUPERVISORS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Supervisor] = (
    weakref.WeakKeyDictionary()
)


def _supervisor_for(loop: asyncio.AbstractEventLoop) -> _Supervisor:
    sup = _SUPERVISORS.get(loop)
    if sup is None:
        sup = _SUPERVISORS[loop] = _Supervisor(loop)
    return sup


_LIVE: weakref.WeakSet[AsyncIsolatedRuntime] = weakref.WeakSet()


# ---------------------------------------------------------------------------
# host calls
# ---------------------------------------------------------------------------


def _sync_call(
    handler: Callable[..., Any], args: list[Any], cid: int, redact: bool, serial: int
) -> bytes:
    """Run a synchronous host function on a handler thread, in a copy of the caller's context,
    and encode its reply there too, so neither the call nor a large result touches the loop."""
    _IN_CALL_OF.set(
        _IN_CALL_OF.get() | {serial}
    )  # only in this call's own context copy
    try:
        value = handler(*args)
        if inspect.isawaitable(value):  # a plain function that returned a coroutine
            value = asyncio.run(_await(value))
        reply: dict[str, Any] = {"t": "reply", "cid": cid, "v": _wire.Enc(value)}
    except Exception as exc:  # noqa: BLE001 - the guest sees the failure, the host keeps running
        reply = _error_reply(cid, exc, redact=redact)
    try:
        return _frame(reply)
    except _wire.WireError as exc:  # the value cannot cross: the guest's TypeError
        return _frame(_error_reply(cid, exc, redact=redact))


async def _call_guarded(
    handler: Callable[..., Any], args: list[Any], serial: int
) -> Any:
    """An asynchronous host function, run with the re-entrancy mark set in *its* context."""
    token = _IN_CALL_OF.set(_IN_CALL_OF.get() | {serial})
    try:
        return await handler(*args)
    finally:
        _IN_CALL_OF.reset(token)


# ---------------------------------------------------------------------------
# the runtime
# ---------------------------------------------------------------------------


class AsyncIsolatedRuntime:
    """An `IsolatedRuntime` for asyncio: the same supervised, sandboxed worker, driven from the
    caller's event loop without a thread per runtime.

    Every argument means exactly what it means for `IsolatedRuntime`, with the same defaults
    (1 GiB memory ceiling, 60 s hard deadline, OS sandbox, `--jitless`, `redact_host_errors`).
    One addition:

    Args:
        handler_executor: Where synchronous host functions run (default: a shared pool of
            `DEFAULT_HANDLER_THREADS` threads). Pass your own to size it, or to keep one tenant's
            slow tools from occupying threads another tenant needs.

    Use it as `async with AsyncIsolatedRuntime(config) as rt:` or
    `rt = await AsyncIsolatedRuntime.create(config)`. Constructing the object starts nothing; the
    worker is started (off the loop) by `create` / `async with`, and is bound to that event loop.

    Every command is a coroutine: `eval` and `eval_module` resolve promises (they are
    `IsolatedRuntime.eval_async` / `eval_module_async`; the `*_async` names are aliases).
    Cancelling a command that has been sent kills the worker and closes the runtime: V8 cannot be
    interrupted from outside mid-command, and a worker left running a command nobody is waiting for
    would answer the next command with the previous one's frames. Pass `timeout=` for a limit that
    keeps the runtime alive.
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        max_memory: int | None = _DEFAULT,
        request_timeout: float | int | None = _DEFAULT,
        timeout_grace: float | int = 2.0,
        max_host_calls: int | None = None,
        max_host_wait: float | int | None = _DEFAULT,
        max_inflight_host_calls: int | None = _DEFAULT,
        write_stall_timeout: float | int | None = _DEFAULT,
        redact_host_errors: bool = True,
        sandbox: str = "auto",
        empty_root: bool = True,
        jitless: bool = True,
        v8_flags: Sequence[str] = (),
        strict_eval: bool = False,
        clock: datetime | float | int | None = None,
        random_seed: int | None = None,
        python: str | None = None,
        prewarm: bool = True,
        handler_executor: Executor | None = None,
    ) -> None:
        # Validation is `IsolatedRuntime.__init__`'s, line for line; nothing here blocks.
        clock_ms = _clock_ms(clock)
        if random_seed is not None and (
            isinstance(random_seed, bool)
            or not isinstance(random_seed, int)
            or not 0 <= random_seed < 2**31
        ):
            raise ValueError("random_seed must be an integer in [0, 2**31)")
        worker_flags = _worker_v8_flags(
            jitless=jitless,
            random_seed=random_seed,
            v8_flags=v8_flags,
            strict_eval=strict_eval,
        )
        if max_memory is _DEFAULT:
            max_memory = DEFAULT_MAX_MEMORY
        max_memory = _limit_int("max_memory", max_memory, minimum=1)
        if sandbox not in ("auto", "require", "off"):
            raise ValueError("sandbox must be 'auto', 'require' or 'off'")
        if os.name != "posix":
            raise NotImplementedError(
                "AsyncIsolatedRuntime currently supports POSIX only"
            )
        config = config or RuntimeConfig()
        for attr in _UNSUPPORTED_CONFIG:
            if getattr(config, attr, None) is not None:
                raise ValueError(
                    f"RuntimeConfig.{attr} is not supported by AsyncIsolatedRuntime yet"
                )

        _check_wire_limits(config, max_memory)
        self._config = {k: getattr(config, k) for k in _CONFIG_KEYS}
        if max_memory is not None and self._config["max_buffer_bytes"] is None:
            # See IsolatedRuntime: a catchable RangeError instead of an RSS kill.
            self._config["max_buffer_bytes"] = max(1, max_memory // 4)
        self._soft_timeout = _limit_seconds("RuntimeConfig.timeout", config.timeout)
        self._max_memory = max_memory
        self._host_calls = 0
        self._request_timeout: float | None | Any
        for attr, value in _session_options(
            request_timeout=request_timeout,
            timeout_grace=timeout_grace,
            max_host_calls=max_host_calls,
            max_host_wait=max_host_wait,
            max_inflight_host_calls=max_inflight_host_calls,
            write_stall_timeout=write_stall_timeout,
            redact_host_errors=redact_host_errors,
        ).items():
            setattr(self, attr, value)
        self._python = python
        self._prewarm = bool(prewarm)
        self._handler_executor = handler_executor
        self._options: dict[str, Any] = {
            "sandbox": sandbox,
            "empty_root": empty_root,
            "v8_flags": worker_flags,
            "max_memory": max_memory,
        }
        if clock_ms is not None:
            self._options["clock_ms"] = clock_ms
        #: The OS layers in force, as the worker reported them ("seatbelt", "landlock+seccomp", ...).
        self.sandbox = "none"
        #: Bonus layers that also took effect, e.g. ["emptyroot"].
        self.sandbox_extras: list[str] = []
        self.v8_flags: list[str] = []

        self._handlers: dict[int, tuple[Callable[..., Any], bool]] = {}
        self._token_to_hid: dict[int, int] = {}
        self._revoked_hids: dict[int, None] = {}
        # Ids of `load_wasm` instances whose module object was dropped without `unload()`: sent
        # along with the next wasm command, so the worker forgets them (see `_wasm.track_drop`).
        self._wasm_dropped: list[int] = []
        self._hids = itertools.count(1)
        self._cmd_ids = itertools.count(1)
        self._serial = next(_SERIALS)
        if config.on_console is not None:
            console_hid = next(self._hids)
            self._handlers[console_hid] = (_checked_console(config.on_console), False)
            self._options["console_hid"] = console_hid

        self._owner_pid = os.getpid()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sup: _Supervisor | None = None
        self._lock: asyncio.Lock | None = None
        self._send_lock = asyncio.Lock()
        self._proc: Any = None
        self._stderr: Any = None
        self._rproto = _FrameReader()
        self._wproto = _Writer()
        self._rtransport: asyncio.ReadTransport | None = None
        self._wtransport: asyncio.WriteTransport | None = None
        self._finalizer: weakref.finalize | None = None
        self._starting = False
        self._closed = False
        self._killed = False
        self._verdict: _Verdict | None = None
        self._waiters: set[asyncio.Future[None]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._streak = 0
        # Supervision state (read by the supervisor on the loop thread).
        self._cmd: _Pump | None = None
        self._gen = 0
        self._async_inflight = 0
        self._idle_since = time.monotonic()
        self._idle_cpu_base: float | None = None
        self._last_cpu: float | None = None
        self._last_idle_sample = 0.0

    @property
    def strict_eval(self) -> bool:
        """As `IsolatedRuntime.strict_eval`."""
        return bool(_strict_eval_setting(self._options["v8_flags"]))

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def create(
        cls, config: RuntimeConfig | None = None, **options: Any
    ) -> AsyncIsolatedRuntime:
        """Construct and start a runtime without blocking the event loop."""
        rt = cls(config, **options)
        await rt._start()
        return rt

    async def __aenter__(self) -> AsyncIsolatedRuntime:
        if self._proc is None:
            await self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _start(self) -> None:
        if self._starting or self._closed:
            raise RuntimeError("this AsyncIsolatedRuntime has already been started")
        self._starting = True
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._lock = asyncio.Lock()
        self._sup = _supervisor_for(loop)
        try:
            async with self._sup.start_slots:
                await self._spawn_and_handshake(loop)
        except BaseException:
            self._closed = True
            raise
        if self._prewarm and self._python is None:
            _refill_spare()
        self._idle_since = time.monotonic()
        self._last_idle_sample = self._idle_since
        self._sup.add(self)

    async def _spawn_and_handshake(self, loop: asyncio.AbstractEventLoop) -> None:
        # The blocking part, fork/exec (or taking the spare), happens on the io pool.
        spawn = loop.run_in_executor(_pool("io"), _spawn, self._python, self._prewarm)
        try:
            self._proc, self._stderr = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # The worker may still arrive; it belongs to nobody, so it dies.
            sup = self._sup
            spawn.add_done_callback(lambda f: _discard_spawned(f, sup))
            self._closed = True
            raise
        except OSError as exc:
            self._closed = True
            raise WorkerCrashed(f"worker failed to start: {_clean(str(exc))}") from None
        _LIVE.add(self)
        try:
            self._rtransport, _ = await loop.connect_read_pipe(
                lambda: self._rproto, self._proc.stdout
            )
            self._wtransport, _ = await loop.connect_write_pipe(
                lambda: self._wproto, self._proc.stdin
            )
            self._finalizer = weakref.finalize(
                self,
                _finalize_worker,
                self._proc,
                self._stderr,
                loop,
                (self._rtransport, self._wtransport),
            )
            await self._handshake()
            await self._check_termination_authority()
            await self._check_limits_can_be_enforced()
        except BaseException:
            self._kill()
            self._close_stderr()
            raise

    async def _handshake(self) -> None:
        try:
            await self._send_frame(
                _frame(
                    {
                        "t": "init",
                        "config": _wire.Enc(self._config),
                        "options": self._options,
                    }
                )
            )
            async with _compat.timeout(_HANDSHAKE_SECONDS):
                payload = await self._next_frame()
            if payload is None:
                await self._reap(0.5)
                raise WorkerCrashed(
                    await self._describe_death("worker exited during startup")
                )
            message = _wire.loads(payload)
            if message["t"] == "error":
                text = message.get("msg")
                raise WorkerCrashed(
                    "worker failed to start: "
                    + (_clean(text) if isinstance(text, str) else "unknown error")
                )
            if message["t"] != "ready":
                raise _wire.WireError("expected ready")
            applied = message.get("sandbox")
            if not isinstance(applied, str) or len(applied) > 64:
                raise _wire.WireError("bad ready frame")
            extras = message.get("extras", [])
            if (
                not isinstance(extras, list)
                or len(extras) > 8
                or not all(isinstance(x, str) and len(x) <= 32 for x in extras)
            ):
                raise _wire.WireError("bad ready frame")
            self.sandbox = applied
            self.sandbox_extras = list(extras)
            missing = (
                _sandbox.missing_layers(applied) if applied != "off" else frozenset()
            )
            if missing and self._options["sandbox"] == "auto":
                warnings.warn(
                    f"AsyncIsolatedRuntime is running with a degraded OS sandbox ({applied!r}; "
                    f"missing {sorted(missing)}). Untrusted code has less containment than "
                    "intended; pass sandbox='require' to refuse instead.",
                    RuntimeWarning,
                    stacklevel=4,
                )
            self.v8_flags = list(self._options["v8_flags"])
        except TimeoutError:
            self._kill()
            raise WorkerCrashed(
                f"worker did not become ready within {_HANDSHAKE_SECONDS:g}s"
            ) from None
        except (_wire.WireError, OSError) as exc:
            self._kill()
            raise WorkerCrashed(f"worker failed to start: {_clean(str(exc))}") from None
        except WorkerCrashed:
            self._kill()
            raise

    async def _check_termination_authority(self) -> None:
        try:
            os.kill(self._proc.pid, 0)
        except PermissionError:
            # Startup already holds a start slot; close() would acquire it again. No guest
            # command has run, so the trusted worker can exit through the close protocol.
            try:
                self._write(_CLOSE_FRAME)
            except OSError:
                pass
            await self._wait_exit(_CLOSE_GRACE_SECONDS)
            raise WorkerCrashed(
                "worker failed to start: supervisor termination authority is unavailable"
            ) from None

    async def _check_limits_can_be_enforced(self) -> None:
        """As `IsolatedRuntime._check_limits_can_be_enforced`: a limit that cannot be measured
        is a limit that is not there, so say so, and under `sandbox="require"` refuse to start."""
        assert self._sup is not None
        sample = await self._sup.sample(self._proc.pid)
        rss, cpu, threads = sample if sample is not None else (None, None, None)
        missing = []
        if self._max_memory is not None and rss is None:
            missing.append("max_memory")
        if cpu is None:
            missing.append("the CPU cap")
        if threads is None:
            missing.append("the thread cap")
        self._idle_cpu_base = self._last_cpu = cpu
        if not missing:
            return
        what = " and ".join(missing)
        if self._options["sandbox"] == "require":
            self._kill()
            raise WorkerCrashed(
                f"{what} cannot be enforced on this system (the worker's resource usage "
                "cannot be read) and sandbox='require' demands every protection"
            )
        warnings.warn(
            f"{what} cannot be enforced on this system: the worker's resource usage cannot be "
            "read, so those limits will never fire.",
            RuntimeWarning,
            stacklevel=4,
        )

    def _apply_session(self, options: dict[str, Any]) -> None:
        """Install `_session_options(...)` on a runtime nobody has used yet (a pool checkout)."""
        for attr, value in options.items():
            setattr(self, attr, value)

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Ask the worker to exit; kill it if it has not within a second. Idempotent, and never
        blocks the loop. A command still running is ended with `WorkerCrashed`."""
        if os.getpid() != self._owner_pid:
            self._drop_inherited()
            return
        if self._proc is None:
            self._closed = True
            return
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "an AsyncIsolatedRuntime must be closed on the event loop it was started on"
            )
        try:
            if not self._closed:
                self._closed = True
                if not self._killed:
                    # A worker shutting down (V8 teardown, process exit) costs CPU like one
                    # starting; the same slots keep a mass close from starving the loop. Not when
                    # a command is running: that worker may take the whole grace second.
                    assert self._sup is not None
                    slot = (
                        self._sup.start_slots
                        if self._cmd is None
                        else contextlib.nullcontext()
                    )
                    async with slot:
                        try:
                            self._write(_CLOSE_FRAME)
                        except OSError:
                            pass
                        await self._wait_exit(_CLOSE_GRACE_SECONDS)
        finally:
            # Also on cancellation: a close() that was interrupted still must not leave a worker.
            self._kill(
                (WorkerCrashed, "the runtime was closed while a command was running")
            )
            self._close_stderr()
        await self._reap(5.0)

    def _close_stderr(self) -> None:
        if self._stderr is not None:
            try:
                self._stderr.close()
            except (OSError, ValueError):
                pass

    async def _wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        delay = 0.001
        while self._proc.poll() is None:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.02)
        return True

    async def _reap(self, timeout: float) -> None:
        if self._proc is None:
            return
        await self._wait_exit(timeout)
        if self._proc.poll() is not None:
            with _ZOMBIES_LOCK:
                _ZOMBIES.discard(self._proc)

    def _kill(self, verdict: _Verdict | None = None) -> None:
        """SIGKILL the worker now (no waiting: it is reaped later, off the loop's critical path),
        record why, and wake whatever is waiting on it. Safe to call any number of times."""
        if os.getpid() != self._owner_pid:
            self._drop_inherited()
            return
        self._closed = True
        if verdict is not None and self._verdict is None:
            self._verdict = verdict
        if self._proc is not None and not self._killed:
            self._killed = True
            _signal_group(self._proc)
            _bury(self._proc)
            if self._sup is not None:
                self._sup.ensure_running()  # someone has to reap it
            # `abort`, not `close`, for stdin: whatever is still buffered for a worker that is
            # gone (or that stopped reading) must not be waited for.
            for transport, end in (
                (self._rtransport, "close"),
                (self._wtransport, "abort"),
            ):
                if transport is None or transport.is_closing():
                    continue
                try:
                    getattr(transport, end)()
                except RuntimeError:  # the loop is closed
                    pass
            if self._loop is None or self._loop.is_closed():
                # No loop to close the transports' pipes for us: close them here.
                for stream in (self._proc.stdin, self._proc.stdout):
                    try:
                        if stream is not None:
                            stream.close()
                    except (OSError, ValueError):
                        pass
            _LIVE.discard(self)
        self._rproto.wake()
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.set_result(None)

    def _drop_inherited(self) -> None:
        """In a fork()ed child: let go of this process's copies of the worker's pipes and stderr
        file, without signalling, waiting on or talking to a worker that belongs to the parent."""
        self._closed = True
        if self._proc is not None:
            for stream in (self._proc.stdin, self._proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        self._close_stderr()

    async def _describe_death(self, prefix: str) -> str:
        code = self._proc.poll()
        if code is None:
            await self._wait_exit(1.0)
            code = self._proc.poll()
        if code == _sandbox.MEMORY_EXIT_CODE:
            return (
                f"{prefix}: worker went over max_memory={self._max_memory} and exited"
            )
        if code is not None and code < 0:
            try:
                prefix += f" (killed by {signal.Signals(-code).name})"
            except ValueError:
                prefix += f" (killed by signal {-code})"
        elif code:
            prefix += f" (exit code {code})"
        loop = asyncio.get_running_loop()
        tail = await loop.run_in_executor(_pool("io"), _stderr_tail, self._stderr)
        last = tail.splitlines()[-1] if tail else ""
        if not last or _NATIVE_FRAME.search(last):
            return prefix
        return f"{prefix}: {_clean(last)}"

    # -- supervision (called by the supervisor, on the loop) ---------------

    def _verdict_error(self) -> Exception:
        assert self._verdict is not None
        cls, text = self._verdict
        return cls(text)

    def _check_clock(self) -> None:
        """The two wall-clock limits, which need no sample: pure arithmetic on the loop."""
        pump = self._cmd
        if pump is None or self._closed:
            return
        if pump.expired():
            self._kill(
                (
                    RuntimeTimeout,
                    f"worker exceeded its {pump.hard:g}s hard deadline and was killed",
                )
            )
        elif pump.waited_too_long():
            self._kill(
                (
                    RuntimeTimeout,
                    f"host callbacks kept the guest waiting for more than "
                    f"{pump.max_host_wait:g}s in one command (max_host_wait); worker killed",
                )
            )

    def _apply_sample(self, sample: _Sample | None, gen: int) -> None:
        """Memory ceiling, thread cap, per-command CPU cap and idle CPU, from one sample."""
        if self._closed or sample is None:
            return
        rss, cpu, threads = sample
        if cpu is not None:
            self._last_cpu = cpu
        if threads is not None and threads > _MAX_WORKER_THREADS:
            self._kill(
                (
                    WorkerCrashed,
                    f"worker started {threads} threads (limit {_MAX_WORKER_THREADS}); killed",
                )
            )
            return
        if self._max_memory is not None and rss is not None and rss > self._max_memory:
            self._kill(
                (
                    WorkerCrashed,
                    f"worker used {rss} bytes, over max_memory={self._max_memory}; killed",
                )
            )
            return
        # CPU readings are only comparable with the baseline of the period they were taken in.
        if cpu is None or gen != self._gen:
            return
        pump = self._cmd
        if pump is not None:
            if (
                pump.cpu_cap is not None
                and pump.cpu_start is not None
                and cpu - pump.cpu_start > pump.cpu_cap
            ):
                self._kill(
                    (
                        RuntimeTimeout,
                        f"worker used more than {pump.cpu_cap:g}s of CPU in one command "
                        "and was killed",
                    )
                )
            return
        base = self._idle_cpu_base
        if base is None:
            return
        # A small flat allowance plus 1% of a core that grows with idle time (see IsolatedRuntime).
        allowed = _IDLE_CPU_LIMIT_SECONDS + 0.01 * (time.monotonic() - self._idle_since)
        if cpu - base > allowed:
            self._kill((WorkerCrashed, "worker burned CPU while idle; killed"))

    async def _final_check(self) -> None:
        """The check `IsolatedRuntime` makes as each command finishes: a spike that ended between
        two samples is still caught here."""
        assert self._sup is not None
        sample = await self._sup.sample(self._proc.pid)
        if self._verdict is not None:
            raise self._verdict_error()
        if sample is None:
            return
        self._apply_sample(sample, -1)  # memory and threads only
        if self._verdict is not None:
            raise self._verdict_error()
        self._idle_cpu_base = sample[1]

    # -- transport ---------------------------------------------------------

    def _write(self, frame: bytes) -> None:
        transport = self._wtransport
        if self._killed or transport is None or transport.is_closing():
            raise BrokenPipeError(errno.EPIPE, "the connection is closed")
        transport.write(frame)

    async def _drain(self) -> None:
        if not self._wproto.paused:
            return
        remaining = self._wtransport.get_write_buffer_size()
        while self._wproto.paused:
            try:
                async with _compat.timeout(self._stall):
                    await self._wproto.drain()
            except TimeoutError:
                current = self._wtransport.get_write_buffer_size()
                if current < remaining:
                    # The peer is still reading. Measure a stall from observed progress,
                    # rather than timing out a large reply that is steadily draining.
                    remaining = current
                    continue
                raise _wire.StalledWrite(
                    errno.EAGAIN, f"the peer stopped reading for {self._stall:g}s"
                ) from None

    async def _send_frame(self, frame: bytes) -> None:
        # Wait before entering the transport buffer, so a burst of replies cannot all
        # queue ahead of drain(). Its backlog is at most one frame above the high watermark.
        async with self._send_lock:
            self._write(frame)
            await self._drain()

    async def _encode(self, message: dict[str, Any], big: bool) -> bytes:
        if big:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(_pool("codec"), _frame, message)
        return _frame(message)

    async def _decode(self, payload: bytes) -> dict[str, Any]:
        if len(payload) > _OFFLOAD_BYTES:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _pool("codec"), _wire.loads_decoded, payload
            )
        return _wire.loads_decoded(payload)

    async def _next_frame(self) -> bytes | None:
        proto = self._rproto
        while True:
            if self._verdict is not None:
                raise self._verdict_error()
            if proto.frames:
                self._streak += 1
                if self._streak >= _YIELD_EVERY:
                    self._streak = 0
                    await asyncio.sleep(0)
                    continue
                return proto.pop()
            if proto.error is not None:
                raise _wire.WireError(proto.error)
            if proto.eof:
                if proto.partial():
                    raise _wire.WireError("peer closed mid-frame")
                return None
            if self._killed:
                raise WorkerCrashed("runtime is closed")
            self._streak = 0
            await proto.wait()

    async def _await_or_death(self, fut: asyncio.Future[Any]) -> Any:
        """Wait for `fut`, unless the worker is killed first (a deadline, the memory ceiling,
        `close()`): then raise why, and leave `fut` to finish on its own."""
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()

        def relay(f: asyncio.Future[Any]) -> None:
            if not f.cancelled():
                f.exception()  # retrieved: no "never retrieved" noise if nobody reads it
            if not waiter.done():
                waiter.set_result(None)

        fut.add_done_callback(relay)
        self._waiters.add(waiter)
        try:
            await waiter
        finally:
            self._waiters.discard(waiter)
        if self._verdict is not None:
            raise self._verdict_error()
        if self._killed:
            raise WorkerCrashed("runtime is closed")
        return fut.result()

    # -- requests ----------------------------------------------------------

    def _hard_timeout(self, soft: float | None) -> float | None:
        if self._request_timeout is not _DEFAULT:
            return self._request_timeout  # a number, or None: the caller opted out
        return DEFAULT_REQUEST_TIMEOUT if soft is None else soft + self._grace

    def _refuse_reentry(self) -> None:
        if self._serial in _IN_CALL_OF.get():
            raise RuntimeError(
                "an AsyncIsolatedRuntime cannot be re-entered from its own host functions"
            )

    def _check_usable(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "this AsyncIsolatedRuntime belongs to the process that created it, "
                "not to a fork() of it"
            )
        if self._loop is None:
            if self._closed:
                raise WorkerCrashed("runtime is closed")
            raise RuntimeError(
                "AsyncIsolatedRuntime is not started: use `async with` or "
                "`await AsyncIsolatedRuntime.create(...)`"
            )
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "an AsyncIsolatedRuntime belongs to the event loop it was started on"
            )

    async def _request(
        self, message: dict[str, Any], *, soft_timeout: float | None = None
    ) -> Any:
        self._refuse_reentry()
        self._check_usable()
        assert self._lock is not None
        hard = self._hard_timeout(soft_timeout)
        # Bounded, as in IsolatedRuntime: a host function that hands work to another thread which
        # then calls back into this runtime would otherwise wait on its own command forever.
        try:
            if self._lock.locked():
                async with _compat.timeout(
                    None if hard is None else hard + self._grace
                ):
                    await self._lock.acquire()
            else:
                await (
                    self._lock.acquire()
                )  # free: taken without suspending, no timer needed
        except TimeoutError:
            raise RuntimeTimeout(
                "timed out waiting for another command on this AsyncIsolatedRuntime to finish "
                "(a host function that waits on another task or thread which calls back into "
                "the same runtime would deadlock)"
            ) from None
        try:
            if self._closed:
                raise WorkerCrashed("runtime is closed")
            message["id"] = cmd_id = next(self._cmd_ids)
            try:
                frame = await self._encode(message, _big_message(message))
            except _wire.WireError as exc:
                raise TypeError(str(exc)) from None
            if self._closed:  # killed while the message was being encoded
                raise WorkerCrashed("runtime is closed")
            pump = _Pump(
                hard,
                None,
                max_host_wait=self._max_host_wait,
                cpu_cap=None if hard is None else hard * _CPU_CAP_FACTOR,
            )
            self._begin(pump)
            try:
                try:
                    await self._send_frame(frame)
                except _wire.StalledWrite:
                    self._kill()
                    raise WorkerCrashed(
                        f"worker stopped reading its input for {self._stall:g}s; killed"
                    ) from None
                except OSError:
                    if self._verdict is not None:
                        raise self._verdict_error() from None
                    self._kill()
                    raise WorkerCrashed(
                        await self._describe_death("worker is gone")
                    ) from None
                return await self._pump(cmd_id, pump)
            except asyncio.CancelledError:
                # The worker is mid-command and nothing can interrupt V8 from here; a worker left
                # to finish would hand its answer to the next command. Kill it.
                self._kill(
                    (WorkerCrashed, "a command was cancelled; the worker was killed")
                )
                raise
            finally:
                self._end()
        finally:
            self._lock.release()

    def _begin(self, pump: _Pump) -> None:
        assert self._sup is not None
        self._cmd = pump
        self._gen += 1
        # The CPU baseline: the latest reading (the previous command's final check, or an idle
        # sample at most `_IDLE_CHECK_SECONDS` old), as in IsolatedRuntime, which costs no thread
        # hop. It can only be *older*, so the
        # command is charged for at most that much extra (idle, and itself capped) CPU: stricter,
        # never looser.
        if self._last_cpu is not None:
            pump.cpu_start = self._last_cpu
            return
        start = self._sup.sample(self._proc.pid)

        def baseline(f: asyncio.Future[_Sample | None]) -> None:
            result = f.result()
            if result is not None and self._cmd is pump:
                pump.cpu_start = result[1]

        start.add_done_callback(baseline)

    def _end(self) -> None:
        self._cmd = None
        self._gen += 1
        self._idle_since = time.monotonic()
        self._last_idle_sample = self._idle_since

    async def _pump(self, cmd_id: int, pump: _Pump) -> Any:
        remote: Exception | None = None
        try:
            while True:
                payload = await self._next_frame()
                if payload is None:
                    if self._verdict is not None:
                        raise self._verdict_error()
                    await self._wait_exit(
                        0.2
                    )  # let an exit code (78: max_memory) arrive
                    self._kill()
                    raise WorkerCrashed(
                        await self._describe_death("worker process died")
                    )
                message = await self._decode(payload)
                kind = message["t"]
                if kind == "call":
                    await self._on_call(message, pump)
                elif kind in ("result", "error") and message.get("id") == cmd_id:
                    await self._final_check()
                    if kind == "result":
                        return message.get("v")  # already decoded by `loads_decoded`
                    # A guest's own JavaScriptError is an answer, not a fault.
                    remote = IsolatedRuntime._remote_error(message)  # noqa: SLF001
                    break
                else:
                    raise _wire.WireError(f"unexpected {_clean(str(kind), 32)!r} frame")
        except (WorkerCrashed, RuntimeTimeout, asyncio.CancelledError):
            raise  # verdicts already acted on; cancellation is handled by `_request`
        except _HostCallBudgetExceeded as exc:
            self._kill()
            raise WorkerCrashed(str(exc)) from None
        except _wire.WireError as exc:
            self._kill()
            raise WorkerCrashed(f"worker broke protocol: {_clean(str(exc))}") from None
        except OSError:
            self._kill()
            raise WorkerCrashed(await self._describe_death("lost the worker")) from None
        except Exception as exc:  # noqa: BLE001
            # It came from reading what the worker sent: the worker is at fault, and is killed.
            self._kill()
            raise WorkerCrashed(
                f"worker sent a malformed frame ({type(exc).__name__})"
            ) from None
        except BaseException:
            self._kill()
            raise
        assert remote is not None
        raise remote

    # -- host callbacks ----------------------------------------------------

    def _error(self, cid: int, exc: BaseException) -> dict[str, Any]:
        return _error_reply(cid, exc, redact=self._redact)

    async def _on_call(self, message: dict[str, Any], pump: _Pump) -> None:
        cid, hid, args = message.get("cid"), message.get("hid"), message.get("args")
        entry = self._handlers.get(hid) if isinstance(hid, int) else None
        if entry is None and hid in self._revoked_hids:
            # In flight when the capability was revoked: the guest lost a race with the host.
            # An error for this call, not the end of the session.
            entry = (_revoked_handler, False)
        if (
            not isinstance(cid, int)
            or isinstance(cid, bool)
            or entry is None
            or not isinstance(args, list)
        ):
            raise _wire.WireError("call for an unknown host function")
        self._host_calls += 1
        if self._max_host_calls is not None and self._host_calls > self._max_host_calls:
            raise _HostCallBudgetExceeded(
                f"guest made more than max_host_calls={self._max_host_calls} host calls"
            )
        handler, is_async = entry
        # Console output is the guest's own work, not a tool call: it pauses the deadline only
        # within the command's console allowance (see `_Pump`). It is synchronous (the worker waits
        # for it), so it is never one of the calls in flight and the in-flight cap does not refuse
        # it; `max_host_calls` still counts it (above).
        console = hid == self._options.get("console_hid")
        if (
            not console
            and self._max_inflight is not None
            and (
                pump.outstanding >= self._max_inflight
                or self._async_inflight >= self._max_inflight
            )
        ):
            await self._send_reply(
                {
                    "t": "reply",
                    "cid": cid,
                    "err": f"more than {self._max_inflight} host calls in flight",
                    "etype": "RuntimeError",
                },
                None,
            )
            return
        if console:
            pump.begin_console()
        else:
            pump.begin_call()
        loop = asyncio.get_running_loop()
        if is_async and not console:
            self._async_inflight += 1
            # `create_task` copies the current context, which is the caller's: the handler sees
            # the caller's contextvars.
            task = loop.create_task(self._async_call(handler, args, cid, pump))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        # A synchronous handler runs on a thread, in a copy of the caller's context (as
        # `asyncio.to_thread` does). The worker is blocked on this call meanwhile, so one at a time.
        context = contextvars.copy_context()
        fut = loop.run_in_executor(
            self._handler_executor or _pool("handlers"),
            context.run,
            _sync_call,
            handler,
            args,
            cid,
            self._redact,
            self._serial,
        )
        try:
            frame = await self._await_or_death(fut)
            await self._send_reply(frame, None if console else pump)
        finally:
            if console:
                pump.end_console()

    async def _async_call(
        self, handler: Callable[..., Any], args: list[Any], cid: int, pump: _Pump
    ) -> None:
        big = False
        try:
            try:
                value = await _call_guarded(handler, args, self._serial)
                reply: dict[str, Any] = {
                    "t": "reply",
                    "cid": cid,
                    "v": _wire.Enc(value),
                }
                big = _big_value(value)
            except BaseException as exc:  # noqa: BLE001 - as IsolatedRuntime: an error reply
                reply = self._error(cid, exc)
            await self._send_reply(reply, pump, big=big)
        finally:
            self._async_inflight -= 1

    async def _send_reply(
        self, reply: dict[str, Any] | bytes, pump: _Pump | None, *, big: bool = False
    ) -> None:
        try:
            if self._closed:
                return  # nobody to answer
            async with self._send_lock:
                if isinstance(reply, bytes):
                    frame = reply
                else:
                    try:
                        frame = await self._encode(reply, big)
                    except _wire.WireError as exc:
                        frame = _frame(self._error(reply["cid"], exc))
                self._write(frame)
                await self._drain()
        except _wire.StalledWrite:
            # The worker stopped reading what we send: kill it; the pump reports it.
            self._kill(
                (
                    WorkerCrashed,
                    f"worker stopped reading its input for {self._stall:g}s; killed",
                )
            )
        except OSError:
            pass  # worker already gone; the pump will report it
        finally:
            if pump is not None:
                pump.end_call()

    def _register_token(self, token: int, hid: int) -> None:
        """A duplicate token is proof of a lying worker (see IsolatedRuntime): the session ends."""
        if token in self._token_to_hid:
            self._handlers.pop(hid, None)
            self._kill()
            raise WorkerCrashed("worker reused a capability token")
        self._token_to_hid[token] = hid

    # -- public API --------------------------------------------------------

    async def eval(
        self, code: str, *, timeout: float | int | timedelta | None = None
    ) -> Any:
        """Evaluate JavaScript in the worker, awaiting a promise result. Host functions (sync or
        async) run while it waits, with the caller's contextvars."""
        soft = _limit_seconds("timeout", timeout)
        if soft is None:
            soft = self._soft_timeout
        return await self._request(
            {"t": "eval_async", "code": code, "timeout": soft}, soft_timeout=soft
        )

    eval_async = eval

    async def eval_module(
        self, specifier: str, *, timeout: float | int | timedelta | None = None
    ) -> Any:
        """Evaluate a module (awaiting top-level await) and return its namespace as a dict."""
        soft = _limit_seconds("timeout", timeout)
        if soft is None:
            soft = self._soft_timeout
        return await self._request(
            {"t": "eval_module_async", "specifier": specifier, "timeout": soft},
            soft_timeout=soft,
        )

    eval_module_async = eval_module

    async def bind_function(self, name: str, handler: Callable[..., Any]) -> int:
        """Expose a host function as a global; returns its capability token.

        A coroutine function is awaited on this loop and may run concurrently with others; a plain
        function runs on the handler thread pool, so it may block without stalling the loop."""
        from ._tools import ToolBridge

        ToolBridge._check_name(name, what="function name")  # noqa: SLF001
        hid = next(self._hids)
        is_async = inspect.iscoroutinefunction(handler)
        self._handlers[hid] = (handler, is_async)
        try:
            token = await self._request(
                {"t": "bind_function", "name": name, "hid": hid, "async": is_async}
            )
        except BaseException:
            self._handlers.pop(hid, None)
            raise
        if not _is_token(token):
            self._handlers.pop(hid, None)
            self._kill()
            raise WorkerCrashed("worker returned a malformed capability token")
        self._register_token(token, hid)
        return token

    async def bind_object(self, name: str, obj: Mapping[str, Any]) -> dict[str, int]:
        """Expose a mapping as a global object; callables become host functions."""
        from ._tools import ToolBridge

        ToolBridge._check_name(name, what="object name")  # noqa: SLF001
        for key in obj:
            ToolBridge._check_name(key, what="property name")  # noqa: SLF001
        entries: dict[str, Any] = {}
        hids: dict[str, int] = {}
        for key, value in obj.items():
            if callable(value):
                hid = next(self._hids)
                is_async = inspect.iscoroutinefunction(value)
                self._handlers[hid] = (value, is_async)
                hids[key] = hid
                entries[key] = {"hid": hid, "async": is_async}
            else:
                entries[key] = {"v": _wire.Enc(value)}
        try:
            tokens = await self._request(
                {"t": "bind_object", "name": name, "entries": entries}
            )
        except BaseException:
            for hid in hids.values():
                self._handlers.pop(hid, None)
            raise
        if (
            not isinstance(tokens, dict)
            or set(tokens) != set(hids)
            or not all(isinstance(k, str) and _is_token(v) for k, v in tokens.items())
        ):
            for hid in hids.values():
                self._handlers.pop(hid, None)
            self._kill()
            raise WorkerCrashed("worker returned malformed capability tokens")
        for key, token in tokens.items():
            self._register_token(token, hids[key])
        return tokens

    async def revoke_op(self, op_id: int) -> bool:
        """Revoke a capability. The host handler is dropped as well as the worker's token."""
        # First and unconditionally, before anything is awaited: what the worker answers (or
        # whether it answers) must not decide whether a revoked capability can still be called.
        hid = self._token_to_hid.pop(op_id, None)
        if hid is not None:
            self._handlers.pop(hid, None)
            self._revoked_hids[hid] = None
            if len(self._revoked_hids) > _REVOKED_MEMORY:
                self._revoked_hids.pop(next(iter(self._revoked_hids)))
        return bool(await self._request({"t": "revoke", "token": op_id}))

    async def add_static_module(self, name: str, source: str) -> None:
        await self._request({"t": "add_module", "name": name, "source": source})

    async def load_wasm(
        self,
        module: Any,
        /,
        *,
        max_bytes: int = _wasm.MAX_WASM_BYTES,
        timeout: float | int | timedelta | None = None,
    ) -> _wasm.AsyncWasmModule:
        """`IsolatedRuntime.load_wasm` for asyncio: returns an `AsyncWasmModule`.

        Needs ``jitless=False``; a path is read by this process (in a thread, so the loop does not
        block), never by the worker."""
        if _wasm.flags_disable_wasm(self._options["v8_flags"]):
            raise RuntimeError(_wasm.JITLESS_MESSAGE)
        if isinstance(module, (bytes, bytearray, memoryview)):
            data = _wasm.read_module(module, max_bytes)
        else:
            data = await asyncio.to_thread(_wasm.read_module, module, max_bytes)
        signatures = _wasm.parse_signatures(data)
        soft = _limit_seconds("timeout", timeout)
        if soft is None:
            soft = self._soft_timeout
        wid = await self._request(
            {
                "t": "wasm_load",
                "bytes": _wire.Enc(data),
                "timeout": soft,
                "drop": _wasm.drain(self._wasm_dropped),
            },
            soft_timeout=soft,
        )
        if not _is_token(wid):
            self._kill()
            raise WorkerCrashed("worker returned a malformed module id")

        async def call(
            name: str, values: list[Any], wide: list[bool], call_timeout: Any
        ) -> Any:
            soft = _limit_seconds("timeout", call_timeout)
            if soft is None:
                soft = self._soft_timeout
            message = {
                "t": "wasm_call",
                "wid": wid,
                "name": name,
                "args": _wire.Enc(values),
                "wide": wide,
                "timeout": soft,
                "drop": _wasm.drain(self._wasm_dropped),
            }
            return await self._request(message, soft_timeout=soft)

        async def unload() -> None:
            if not self._closed:
                await self._request({"t": "wasm_unload", "wid": wid})

        return _wasm.track_drop(
            _wasm.AsyncWasmModule(signatures, call, unload), self._wasm_dropped, wid
        )

    async def set_module_resolver(
        self, resolver: Callable[[str, str], str | None]
    ) -> None:
        """Resolve import specifiers with a host function: `(specifier, referrer) -> str | None`."""
        hid = next(self._hids)
        self._handlers[hid] = (_checked_specifiers(resolver, 2), False)
        try:
            await self._request({"t": "set_module_resolver", "hid": hid})
        except BaseException:
            self._handlers.pop(hid, None)
            raise

    async def set_module_loader(self, loader: Callable[[str], Any]) -> None:
        """Supply module source with a host function: `(specifier) -> str`, sync or async."""
        hid = next(self._hids)
        self._handlers[hid] = (
            _checked_specifiers(loader, 1),
            inspect.iscoroutinefunction(loader),
        )
        try:
            await self._request({"t": "set_module_loader", "hid": hid})
        except BaseException:
            self._handlers.pop(hid, None)
            raise


_CLOSE_FRAME = _frame({"t": "close"})


def _discard_spawned(
    fut: asyncio.Future[tuple[Any, Any]], sup: _Supervisor | None
) -> None:
    if fut.cancelled() or fut.exception() is not None:
        return
    proc, stderr = fut.result()
    _signal_group(proc)
    _bury(proc)
    if sup is not None:
        sup.ensure_running()
    try:
        stderr.close()
    except (OSError, ValueError):
        pass


def _kill_all_at_exit() -> None:
    for runtime in list(_LIVE):
        try:
            runtime._kill()  # noqa: SLF001
        except Exception:  # noqa: BLE001, S110
            pass
    _reap_zombies(block=1.0)


atexit.register(_kill_all_at_exit)


def _forget_parents_workers() -> None:
    """After `fork()`, the child holds the parent's runtimes as inherited descriptors. They are the
    parent's: no finalizer or atexit hook here may kill them, no supervisor may sample them, and
    the parent's thread pools do not exist in the child (their threads did not survive the fork)."""
    global _POOLS_LOCK, _ZOMBIES_LOCK, _REFILLING  # noqa: PLW0603
    _POOLS.clear()
    _POOLS_LOCK = threading.Lock()
    _ZOMBIES_LOCK = threading.Lock()
    _ZOMBIES.clear()
    _SUPERVISORS.clear()
    _REFILLING = False
    for runtime in list(_LIVE):
        if runtime._finalizer is not None:  # noqa: SLF001
            runtime._finalizer.detach()  # noqa: SLF001
    _LIVE.clear()


os.register_at_fork(after_in_child=_forget_parents_workers)
