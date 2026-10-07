"""Worker process for `IsolatedRuntime` (started by `_isolated._start_worker`).

Hosts one ordinary `Runtime` and drives it from commands on stdin. Host
functions bound by the parent become stubs here that send a `call` frame and
wait for the parent's `reply`, so the handlers themselves never leave the
parent. Guest code that crashes, wedges or exhausts this process is the
parent's problem to observe and kill, not ours to survive.

Protocol: see `pydeno/_wire.py`; the command set is in
`docs/superpowers/specs/2026-10-02-isolated-runtime-design.md`.
"""

from __future__ import annotations

# ruff: noqa: E402 - the Seatbelt precompile below has to start before the other imports

# Everything this process will ever import is imported here, before the OS sandbox
# goes up: afterwards the filesystem is gone, so a lazy import (including the ones
# the Rust core does on first use of a feature) would fail. The extra imports below
# are exactly those lazy ones.
from . import _sandbox

# First, so the Seatbelt profile compiles on another thread while the imports below run.
_sandbox.precompile_seatbelt()

import sys

# Never `ssl`: this process makes no network connections (the sandbox forbids them), and asyncio
# imports `ssl` only if it can (`try: import ssl / except ImportError: ssl = None`), so marking it
# unimportable skips loading OpenSSL, about 2-3 ms of start-up. After the sandbox is up the import
# would fail anyway, for want of a filesystem.
sys.modules.setdefault("ssl", None)  # type: ignore[arg-type]

import asyncio
import base64  # noqa: F401
import builtins
import binascii  # noqa: F401
import concurrent.futures
import contextvars  # noqa: F401
import datetime
import inspect  # noqa: F401
import itertools
import json  # noqa: F401
import os
import queue
import re
import threading
import time
import weakref  # noqa: F401
from collections.abc import Callable

from . import _awaitable  # noqa: F401
from . import _wasm
from . import _wire
from ._pydeno import (
    JavaScriptError,
    JsUndefined,
    Runtime,
    RuntimeConfig,
    RuntimeForceKilled,
    RuntimeTerminated,
    RuntimeTimeout,
    _set_terse_guest_errors,
    _set_v8_flags,
)

# `typing.TYPE_CHECKING` without importing `typing`: the worker imports this module and does not
# otherwise need `typing` (about 2 ms of its start-up on 3.14; older asyncio imports it anyway).
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

PROTOCOL_VERSION = 1
_MEMORY_POLL_SECONDS = 0.02

# A host function that raises reaches guest JS as an error whose `name` is the Python class name
# and whose `message` is `str(exc)`. The exception lives in the parent, so the worker rebuilds
# one with the same name and message. The name is text from the other side of a boundary: it must
# be a plain identifier, and the number of distinct classes is capped so it cannot be used to make
# this process allocate without bound.
_EXC_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_EXC_CLASSES = 256
_exc_classes: dict[str, type[Exception]] = {}


# What is kept of an oversized `console.*` call (see `_console_stub`): a prefix of the text of its
# string arguments, at most this many characters in all. Far under the 16 MiB frame, far over any
# sensible `max_output_bytes` default (64 KiB), and it is the parent's cap that applies after that.
_CONSOLE_PREFIX_CHARS = 1 << 20


def _console_prefix(args: list[Any]) -> list[Any]:
    """`args` cut to something one frame can carry: string arguments are kept up to a shared
    budget; anything else (an object or array the parent would format as JSON) is replaced by a
    short placeholder, since its size is unknown here."""
    left = _CONSOLE_PREFIX_CHARS
    out: list[Any] = []
    for arg in args:
        if isinstance(arg, str):
            part = arg[:left]
            left -= len(part)
        elif (
            isinstance(arg, (bool, float))
            or arg is None
            or (isinstance(arg, int) and abs(arg) < 2**63)
        ):
            part = arg
        else:
            part = "[value omitted: too large]"
        out.append(part)
    return out


class _VerbatimMessage:
    """`str(exc)` returns the message exactly. Without this, rebuilding `KeyError('k')` from
    its already-formatted text `"'k'"` would quote it a second time."""

    _pydeno_message: str

    def __str__(self) -> str:
        return self._pydeno_message


def _remote_exception(name: object, message: str) -> Exception:
    if not isinstance(name, str) or not _EXC_NAME.match(name):
        name = "RuntimeError"
    cls = _exc_classes.get(name)
    if cls is None and len(_exc_classes) >= _MAX_EXC_CLASSES:
        name = "RuntimeError"
        cls = _exc_classes.get(name)
    if cls is None:
        base = getattr(builtins, name, None)
        if not (isinstance(base, type) and issubclass(base, Exception)):
            base = Exception
        cls = type(name, (_VerbatimMessage, base), {})
        _exc_classes[name] = cls
    try:
        exc = cls(message)
    except Exception:  # noqa: BLE001 - a builtin whose constructor wants other arguments
        exc = type(name, (_VerbatimMessage, Exception), {})(message)
    exc._pydeno_message = message  # noqa: SLF001
    return exc


# Freezes the guest's clock. `Date` is replaced by a function that never reads the real
# clock, and the original is not reachable afterwards: `Date.prototype.constructor` is
# repointed, the replacement's own prototype is `Function.prototype` (not the original, whose
# `now` would still tick), and the one other thing in V8 that reads "now" implicitly,
# `Intl.DateTimeFormat#format()` with no argument, is wrapped too.
# Globals a guest has no use for and that only widen the attack surface: shared memory and atomics
# are what a high-resolution timer is built from, and weak references and finalizers make garbage
# collection observable.
_STRIP_GLOBALS_JS = (
    "for (const n of ['SharedArrayBuffer', 'Atomics', 'WeakRef', 'FinalizationRegistry'])"
    " { try { delete globalThis[n]; } catch (_) {} }\n"
)

# Pins `Error.prepareStackTrace` so guest code can neither install one (and read every `CallSite`,
# `ext:` frame file names included) nor remove this one. The pinned formatter renders the stack
# as V8 does, minus frames of pydeno's own `ext:` scripts, which would otherwise name the bridge
# (`callSync (ext:pydeno/python_bridge.js:300:31)`). Runs after the host's own bootstrap, which
# may set the hook itself; it must also stay the last thing that touches the property, because the
# bridge's bind code redefines it temporarily and a non-configurable property would refuse that.
_PIN_STACK_FORMAT_JS = """
;(() => {
  const toStr = Error.prototype.toString;
  const call = Function.prototype.call;
  const str = String;
  const format = function prepareStackTrace(error, sites) {
    let out;
    try { out = call.call(toStr, error); } catch (_) { out = 'Error'; }
    for (let i = 0; i < sites.length; i++) {
      const site = sites[i];
      let file;
      try { file = site.getFileName(); } catch (_) { file = undefined; }
      if (typeof file === 'string' && file.startsWith('ext:')) continue;
      out += '\\n    at ' + str(site);
    }
    return out;
  };
  Object.defineProperty(Error, 'prepareStackTrace',
    { value: format, writable: false, configurable: false, enumerable: false });
})();
"""

_FROZEN_CLOCK_JS = """
(() => {
  const Native = Date;
  const frozen = %(ms)d;
  const Patched = function Date(...args) {
    if (!new.target) return new Native(frozen).toString();
    // No arguments means "now"; construct with the frozen instant, keeping `new.target` so
    // that `class Stamp extends Date` still gets its own prototype.
    if (args.length === 0) args = [frozen];
    return Reflect.construct(Native, args, new.target === Patched ? Native : new.target);
  };
  Object.defineProperty(Patched, 'prototype', { value: Native.prototype, writable: false });
  Object.defineProperty(Patched, 'now', { value: () => frozen, writable: true, configurable: true });
  Object.defineProperty(Patched, 'UTC', { value: Native.UTC, writable: true, configurable: true });
  Object.defineProperty(Patched, 'parse', { value: Native.parse, writable: true, configurable: true });
  Object.defineProperty(Native.prototype, 'constructor',
    { value: Patched, writable: true, configurable: true });
  Object.defineProperty(globalThis, 'Date', { value: Patched, writable: true, configurable: true });

  // `Temporal.Now` is a second wall clock, with nanosecond resolution, that `Date` never sees.
  if (typeof Temporal !== 'undefined' && Temporal.Now) {
    const T = Temporal;
    const instant = () => T.Instant.fromEpochMilliseconds(frozen);
    const zoned = (tz = 'UTC') => instant().toZonedDateTimeISO(tz);
    const now = {
      instant,
      timeZoneId: () => 'UTC',
      zonedDateTimeISO: zoned,
      plainDateTimeISO: (tz) => zoned(tz).toPlainDateTime(),
      plainDateISO: (tz) => zoned(tz).toPlainDate(),
      plainTimeISO: (tz) => zoned(tz).toPlainTime(),
    };
    Object.defineProperty(T, 'Now',
      { value: Object.freeze(now), writable: false, configurable: false });
  }

  if (typeof Intl !== 'undefined' && Intl.DateTimeFormat) {
    const proto = Intl.DateTimeFormat.prototype;
    const format = Object.getOwnPropertyDescriptor(proto, 'format');
    Object.defineProperty(proto, 'format', {
      configurable: true,
      get() { const f = format.get.call(this); return (d) => f(d === undefined ? frozen : d); },
    });
    const parts = proto.formatToParts;
    Object.defineProperty(proto, 'formatToParts', {
      configurable: true, writable: true,
      value(d) { return parts.call(this, d === undefined ? frozen : d); },
    });
  }
})();
"""


def _encode_error_text(exc: Exception) -> str:
    """The guest-visible text of a value that could not be encoded. A `WireError` is pydeno's own
    wording; a `ValueError` is CPython's int-to-str digit limit, which names the host language
    and an interpreter setting, so the guest gets a plain BigInt message instead."""
    if isinstance(exc, _wire.WireError):
        return str(exc)
    return "BigInt value is too large to transfer"


_PLAIN_LEAVES = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    bytearray,
    memoryview,
    datetime.datetime,
    JsUndefined,
)
_DROPPED = object()


def _scrub(value: Any, mode: str, depth: int) -> Any:
    """`value` with every leaf the wire cannot carry (a `JsFunction`, a stream handle, ...)
    dropped or replaced by its type name in brackets, for `on_unserializable`. Containers are
    rebuilt, so the guest's own structure is never mutated; past the wire's depth limit the value
    is returned as it is, and the encoder reports the depth the way it always did."""
    if isinstance(value, _PLAIN_LEAVES):
        return value
    if depth > _wire.MAX_DEPTH:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            kept = _scrub(item, mode, depth + 1)
            if kept is not _DROPPED:
                out[key] = kept
        return out
    if isinstance(value, (list, tuple)):
        items = [_scrub(item, mode, depth + 1) for item in value]
        return [None if item is _DROPPED else item for item in items]
    if isinstance(value, (set, frozenset)):
        kept = [_scrub(item, mode, depth + 1) for item in value]
        return {item for item in kept if item is not _DROPPED and _hashable(item)}
    return _DROPPED if mode == "drop" else f"[{type(value).__name__}]"


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


# Exceptions the parent may re-raise by name. Anything else becomes RuntimeError.
_ERROR_KINDS = {
    cls.__name__: cls
    for cls in (
        JavaScriptError,
        RuntimeTimeout,
        RuntimeTerminated,
        RuntimeForceKilled,
        RuntimeError,
        ValueError,
        TypeError,
    )
}

_CONFIG_KEYS = (
    "max_heap_size",
    "initial_heap_size",
    "max_buffer_bytes",
    "timeout",
    "bootstrap",
    "enable_console",
    "max_serialization_depth",
    "max_serialization_bytes",
    "force_kill_grace",
)


class _CommandLoop(asyncio.SelectorEventLoop):
    """Between commands this loop looks closed to other threads, as the per-command loop did once
    `asyncio.run` closed it: a late `call_soon_threadsafe` (a host call's start or completion
    owed to an earlier command) is refused instead of queued for the next one."""

    def __init__(self) -> None:
        self._submission_lock = threading.Lock()
        self._idle = True
        super().__init__()
        # `asyncio.all_tasks` is weak. A task that only its own cycle references (a coroutine
        # parked on an Event nobody else holds) can be collected while pending, and its cleanup
        # then runs outside any loop. Holding each task until it is done keeps it reachable here.
        self.live_tasks: set[asyncio.Future[Any]] = set()

        def task_factory(
            loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any
        ) -> Any:
            task = asyncio.Task(coro, loop=loop, **kwargs)
            self.live_tasks.add(task)
            task.add_done_callback(self.live_tasks.discard)
            return task

        self.set_task_factory(task_factory)

    def pending_tasks(self) -> set[asyncio.Future[Any]]:
        return {t for t in self.live_tasks if not t.done()} | asyncio.all_tasks(self)

    @property
    def idle(self) -> bool:
        return self._idle

    @idle.setter
    def idle(self, value: bool) -> None:
        # A submission accepted before the idle transition must be queued before settlement.
        with self._submission_lock:
            self._idle = value

    def is_closed(self) -> bool:
        return self.idle or super().is_closed()

    def call_soon_threadsafe(
        self, callback: Any, *args: Any, context: Any = None
    ) -> Any:
        with self._submission_lock:
            if self._idle:
                raise RuntimeError("Event loop is closed")
            return super().call_soon_threadsafe(callback, *args, context=context)


class _Worker:
    def __init__(self, in_fd: int, out_fd: int) -> None:
        # Read before anything runs, in the process that was just started: a host that is PID 1
        # (a container's main process) is a legitimate parent, so "orphaned" means the parent
        # changed, not that it is 1.
        self._parent = os.getppid()
        self._reader = _wire.FrameReader(in_fd)
        self._writer = _wire.FrameWriter(out_fd)
        # Who reads the parent's frames. Between commands the main thread does, itself: a command
        # then reaches the runtime without a hop through another thread. While a command runs the
        # main thread is inside the runtime, so when the guest is waiting for host calls the
        # helper thread (`_read_loop`) reads instead: the replies, and anything else, which it
        # queues for afterwards. Exactly one of them reads at a time (`_helper_reading` and
        # `_in_command`, both under `_io`). A command with no host call in flight has nothing to
        # read, and the helper sleeps through it; a parent that dies meanwhile is caught by
        # `_watch` instead.
        self._io = threading.Condition(
            threading.Lock()
        )  # never re-entered: no RLock needed
        self._in_command = False
        self._helper_reading = False
        self._queued: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._pending: dict[int, concurrent.futures.Future[Any]] = {}
        self._pending_lock = threading.Lock()
        self._call_ids = itertools.count(1)
        self._runtime: Runtime | None = None
        # One event loop for every async command, driven by the main thread for the length of
        # the command and idle in between, instead of an `asyncio.run` (a new loop, its selector
        # and self-pipe, and their teardown) per command. Created here, before the OS sandbox,
        # like every import; it starts no thread. `_settle` leaves it empty after each command,
        # and between commands it refuses work from other threads (`_CommandLoop`).
        self._loop = _CommandLoop()
        # `load_wasm` instances, by id: the bridge's function handles (the parent holds only ids).
        self._wasm: dict[int, Any] = {}
        self._wasm_ids = itertools.count(1)
        # The bridge's loader, taken once before any guest code (see `_init`), never looked up by
        # name later: where V8 has no WebAssembly a guest could plant a global of that name.
        self._wasm_loader: Any = None

    # -- transport ---------------------------------------------------------

    def _read_frame(self) -> dict[str, Any] | None:
        """One frame from the parent, a reply already delivered (None), or the process ends:
        a parent that is gone or broke protocol leaves nothing here worth keeping alive."""
        try:
            payload = self._reader.read()
            if payload is None:
                os._exit(0)
            message = _wire.loads(payload)
        except (_wire.WireError, OSError):
            os._exit(0)
        if message["t"] != "reply":
            return message
        try:
            self._resolve(message)
        except Exception:  # noqa: BLE001, S110
            # One bad reply must never stop the reading: every later reply, and so every later
            # command, would wait forever.
            pass
        return None

    def _read_loop(self) -> None:
        """The helper's half of the reading (see `__init__`): while a command runs and waits for
        the host. Every call it waits for gets a reply (or the parent kills us), so the read it
        starts always ends."""
        io = self._io
        while True:
            with io:
                while not (self._in_command and self._pending):
                    io.wait()
                self._helper_reading = True
            try:
                message = self._read_frame()
                if message is not None:
                    self._queued.put(message)
            finally:
                with io:
                    self._helper_reading = False
                    io.notify_all()

    def _next_command(self) -> dict[str, Any]:
        """The main thread's half: a command the helper queued, else read one directly."""
        io = self._io
        while True:
            with io:
                while self._helper_reading and self._queued.empty():
                    io.wait()
                if not self._queued.empty():
                    return self._queued.get_nowait()
            message = self._read_frame()
            if message is not None:
                return message

    def _resolve(self, message: dict[str, Any]) -> None:
        with self._pending_lock:
            future = self._pending.pop(message.get("cid"), None)
        if future is None or future.done():
            # Cancelled: whoever was waiting (a loop that has since closed, a command that timed
            # out) no longer wants the answer. Setting a result on it would raise.
            return
        try:
            if "err" in message:
                future.set_exception(
                    _remote_exception(message.get("etype"), str(message["err"]))
                )
                return
            try:
                future.set_result(_wire.decode_value(message.get("v")))
            except _wire.WireError as exc:
                future.set_exception(RuntimeError(f"bad reply from host: {exc}"))
        except concurrent.futures.InvalidStateError:
            return  # cancelled between the check above and now

    # -- host calls --------------------------------------------------------

    def _call_host(
        self, hid: int, args: tuple[Any, ...]
    ) -> concurrent.futures.Future[Any]:
        cid = next(self._call_ids)
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        with self._pending_lock:
            self._pending[cid] = future
        # Inside a command this wakes the helper to read the reply (see `__init__`); outside
        # one the main thread is the reader and gets it.
        with self._io:
            self._io.notify_all()
        try:
            self._writer.send(
                {
                    "t": "call",
                    "cid": cid,
                    "hid": hid,
                    "args": [_wire.Enc(a) for a in args],
                }
            )
        except (_wire.WireError, ValueError) as exc:
            with self._pending_lock:
                self._pending.pop(cid, None)
            raise TypeError(_encode_error_text(exc)) from None
        return future

    def _stub(self, hid: int, is_async: bool) -> Any:
        if is_async:

            async def async_stub(*args: Any) -> Any:
                return await asyncio.wrap_future(self._call_host(hid, args))

            return async_stub

        def sync_stub(*args: Any) -> Any:
            return self._call_host(hid, args).result()

        return sync_stub

    def _console_stub(self, hid: int) -> Any:
        """`console.*` goes to the parent's callback. A broken callback or an argument that
        cannot cross the boundary must never break the guest, so every failure is dropped.

        One exception to dropping: a call too large to cross (over 10 MiB when the engine converts it,
        or over a 16 MiB frame when it is sent) arrives as a bounded prefix of its text, flagged as cut
        (`cut`, sent as a third argument), so the parent ends that stream with `[truncated]` and sets
        `truncated`. Before, the line vanished and nothing said so."""

        def console(level: str, args: list[Any], cut: bool = False) -> None:
            try:
                try:
                    future = self._call_host(
                        hid, (level, args, True) if cut else (level, args)
                    )
                except TypeError:
                    # Not sent: it does not fit a frame (a send that worked, whose callback then
                    # failed, must not be repeated: the callback would see the line twice).
                    future = self._call_host(hid, (level, _console_prefix(args), True))
                future.result()
            except Exception:  # noqa: BLE001, S110
                pass

        return console

    # -- commands ----------------------------------------------------------

    def _rt(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("worker is not initialised")
        return self._runtime

    def _run_async(self, make: Callable[[], Any]) -> Any:
        """Run one async command on the worker's loop. `make` is called from inside the
        coroutine: `eval_async` binds to the running loop when *called*."""

        async def run() -> Any:
            return await make()

        self._loop.idle = False
        try:
            return self._loop.run_until_complete(run())
        finally:
            # From here on other threads see a closed loop: nothing more can be queued for later.
            self._loop.idle = True
            self._settle()

    def _settle(self) -> None:
        """Leave nothing of a command on the loop for the next one, as `asyncio.run` closing
        its loop did: every task still pending (an async host call the guest started and never
        awaited) is cancelled and run until it has finished, and whatever callbacks the last pass
        queued (a finished host call's completion) run now, not at the start of the next
        command."""
        loop = self._loop
        while True:
            tasks = loop.pending_tasks()
            if tasks:
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.call_soon(loop.stop)
            loop.run_forever()
            # Cancellation handlers and done callbacks can create more tasks or queue another
            # callback. Rescan after each pass instead of carrying those into the next command.
            if not loop.pending_tasks() and not loop._ready:
                break
        # Keep timers alive while cancelled tasks finish (their cleanup may await sleep), then
        # discard delayed callbacks as closing the old per-command loop used to do.
        for handle in loop._scheduled:
            handle.cancel()
        loop._scheduled.clear()
        loop._timer_cancelled_count = 0

    def _handle(self, message: dict[str, Any]) -> Any:
        kind = message["t"]
        if kind == "eval":
            return self._rt().eval(message["code"])
        if kind == "eval_async":
            return self._run_async(
                lambda: self._rt().eval_async(
                    message["code"], timeout=message.get("timeout")
                )
            )
        if kind == "bind_function":
            return self._rt().bind_function(
                message["name"], self._stub(message["hid"], bool(message["async"]))
            )
        if kind == "bind_object":
            members: dict[str, Any] = {}
            for key, entry in message["entries"].items():
                if "hid" in entry:
                    members[key] = self._stub(entry["hid"], bool(entry["async"]))
                else:
                    members[key] = _wire.decode_value(entry["v"])
            return self._rt().bind_object(message["name"], members)
        if kind == "revoke":
            return self._rt().revoke_op(message["token"])
        if kind == "add_module":
            return self._rt().add_static_module(message["name"], message["source"])
        if kind == "set_module_resolver":
            # Called by V8 on the runtime thread, which simply waits for the parent's answer.
            return self._rt().set_module_resolver(self._stub(message["hid"], False))
        if kind == "set_module_loader":
            return self._rt().set_module_loader(self._stub(message["hid"], False))
        if kind == "eval_module":
            return self._rt().eval_module(message["specifier"])
        if kind == "eval_module_async":
            return self._run_async(
                lambda: self._rt().eval_module_async(
                    message["specifier"], timeout=message.get("timeout")
                )
            )
        if kind in ("wasm_load", "wasm_call"):
            # Instances whose `WasmModule` the parent dropped without unloading.
            for wid in message.get("drop", ()):
                self._wasm.pop(wid, None)
        if kind == "wasm_load":
            data = _wire.decode_value(message["bytes"])
            if not isinstance(data, bytes) or len(data) > _wasm.MAX_WASM_BYTES:
                raise _wire.WireError("wasm_load takes at most MAX_WASM_BYTES bytes")
            if self._wasm_loader is None:
                raise RuntimeError(_wasm.JITLESS_MESSAGE)
            handle = self._wasm_loader(data, timeout=message.get("timeout"))
            wid = next(self._wasm_ids)
            self._wasm[wid] = handle
            return wid
        if kind == "wasm_call":
            handle = self._wasm.get(message["wid"])
            if handle is None:
                raise RuntimeError("this WebAssembly module was unloaded")
            return handle(
                message["name"],
                _wire.decode_value(message["args"]),
                message["wide"],
                _wire.decode_value(message["buf"]),
                timeout=message.get("timeout"),
            )
        if kind == "wasm_unload":
            handle = self._wasm.pop(message["wid"], None)
            if handle is not None:
                handle(None, [], [])
            return None
        raise _wire.WireError(f"unknown command {kind!r}")

    def _run_command(self, message: dict[str, Any]) -> None:
        cmd_id = message.get("id")
        try:
            result = self._handle(message)
            mode = message.get("unser")
            if mode in ("drop", "stringify"):
                result = _scrub(result, mode, 0)
                if result is _DROPPED:
                    result = None
            reply: dict[str, Any] = {
                "t": "result",
                "id": cmd_id,
                "v": _wire.Enc(result),
            }
        except _wire.WireError as exc:
            reply = {
                "t": "error",
                "id": cmd_id,
                "kind": "TypeError",
                "msg": _encode_error_text(exc),
            }
        except BaseException as exc:  # noqa: BLE001 - every failure is reported, never raised here
            name = type(exc).__name__
            kind = name if name in _ERROR_KINDS else "RuntimeError"
            reply = {"t": "error", "id": cmd_id, "kind": kind, "msg": str(exc)}
        try:
            self._writer.send(reply)
        except (_wire.WireError, ValueError) as exc:
            # The result itself could not be encoded: a JS function handle, or a BigInt past
            # Python's int-to-str digit limit (a ValueError). Encoding fails before anything is
            # written, so an error reply is safe, and the guest must not be able to end the
            # session by returning one.
            self._writer.send(
                {
                    "t": "error",
                    "id": cmd_id,
                    "kind": "TypeError",
                    "msg": _encode_error_text(exc),
                }
            )

    def _init(self, message: dict[str, Any]) -> None:
        config = message.get("config", {})
        options = message.get("options", {})
        mode = options.get("sandbox", "auto")
        flags = options.get("v8_flags", [])
        if mode not in ("auto", "require", "off"):
            raise ValueError(f"unknown sandbox mode {mode!r}")
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            raise ValueError("v8_flags must be a list of strings")

        # Before any guest code: limit and module errors the guest can read must not carry this
        # host's config field names, docs paths or API hints.
        _set_terse_guest_errors(True)
        # Order matters. V8 flags freeze at the first isolate; the sandbox has to be up
        # before the isolate exists so every thread V8 and tokio spawn inherits it.
        if flags:
            unknown = _set_v8_flags(flags)
            if unknown:
                raise ValueError(f"V8 did not recognise: {unknown}")
        # Opened first, before anything that changes who we are or what we may open: after
        # dropping to `nobody` /proc/self is root-owned, and once Landlock is up no /proc
        # file can be opened at all. The descriptor keeps working through both.
        read_rss = _sandbox.rss_reader()
        hardened = _sandbox.harden_process()
        max_memory = options.get("max_memory")
        if (
            isinstance(max_memory, int)
            and not isinstance(max_memory, bool)
            and max_memory > 0
        ):
            # A kernel ceiling under the sampled one (`_sandbox.DATA_HEADROOM` explains the gap).
            _sandbox.limit_data(max_memory)
        empty_root = options.get("empty_root", "auto")
        if empty_root is True:
            empty_root = "auto"
        elif empty_root is False:
            empty_root = "off"
        if empty_root not in ("auto", "require", "off"):
            raise ValueError(f"unknown empty_root mode {empty_root!r}")
        applied = (
            "none"
            if mode == "off"
            else _sandbox.apply(
                empty_root=empty_root != "off",
                # A jitless V8 never maps memory executable, so refuse it: an exploit then has to
                # work without injecting code.
                allow_exec="--jitless" not in flags,
            )
        )
        if applied != "none" and not _sandbox.missing_layers(applied):
            # Ask the kernel rather than trust the filter lists: if the platform's full sandbox
            # claims to be on and a forbidden operation still works, no guest code may run in
            # this process. (A degraded one, say a kernel without Landlock, is expected to leak.)
            breaches = _sandbox.attest(parent=self._parent)
            if breaches:
                raise RuntimeError(
                    f"sandbox self-test failed: the worker could still {breaches} "
                    f"(applied: {applied}){_sandbox.layer_notes()}"
                )
        if empty_root == "require" and "emptyroot" not in _sandbox.EXTRAS:
            raise RuntimeError(
                "empty_root='require' but the empty-root layer could not be applied here "
                "(it needs unprivileged user namespaces and a single-threaded worker)"
                f"{_sandbox.layer_notes()}"
            )
        if mode == "require":
            # "require" means every layer this platform has, not "at least one": a kernel that
            # lacks Landlock must not be allowed to pass for a fully sandboxed one.
            missing = _sandbox.missing_layers(applied)
            if missing:
                raise RuntimeError(
                    f"an OS sandbox is required but {sorted(missing)} could not be applied "
                    f"here (applied: {applied}){_sandbox.layer_notes()}"
                )
            if (
                sys.platform.startswith("linux")
                and hardened.get("uid_before") == 0
                and hardened.get("uid_after") == 0
            ):
                raise RuntimeError(
                    "an OS sandbox is required but this worker runs as root and could not "
                    "drop its privileges"
                )
        max_memory = options.get("max_memory")
        threading.Thread(
            target=_watch,
            args=(
                max_memory if isinstance(max_memory, int) and max_memory > 0 else None,
                read_rss,
                self._parent,
            ),
            name="pydeno-worker-watch",
            daemon=True,
        ).start()

        kwargs = {k: config[k] for k in _CONFIG_KEYS if config.get(k) is not None}
        # Never let the engine echo console output into this process's own stdout/stderr: both
        # point at the parent's capture file, which sits under RLIMIT_FSIZE (1 MiB), and
        # deno_core's `op_print` unwraps the flush of a failed write, so one `console.log` of a
        # megabyte would abort the worker (SIGABRT) instead of raising. Nobody reads that echo
        # anyway: the parent sees console output through `on_console` (the stub below), and the
        # capture file only ever yields the last line of a crash. `enable_console=True` therefore
        # keeps its documented meaning for the callback (it still fires) and stops at this
        # process boundary.
        kwargs["enable_console"] = False
        console_hid = options.get("console_hid")
        if isinstance(console_hid, int):
            kwargs["on_console"] = self._console_stub(console_hid)
        # First of all, so everything after it (clock, the caller's bootstrap) sees the reduced
        # global scope.
        kwargs["bootstrap"] = _STRIP_GLOBALS_JS + str(kwargs.get("bootstrap") or "")
        clock_ms = options.get("clock_ms")
        if isinstance(clock_ms, int) and not isinstance(clock_ms, bool):
            # Runs before the caller's own bootstrap, which therefore sees the frozen clock.
            kwargs["bootstrap"] = (_FROZEN_CLOCK_JS % {"ms": clock_ms}) + str(
                kwargs.get("bootstrap") or ""
            )
        kwargs["bootstrap"] = str(kwargs.get("bootstrap") or "") + _PIN_STACK_FORMAT_JS
        self._runtime = Runtime(RuntimeConfig(**kwargs))
        if "--jitless" not in flags:
            # Before any guest code: only the host's own bootstrap has run.
            self._wasm_loader = _wasm.bridge_loader(self._runtime)
        self._writer.send(
            {
                "t": "ready",
                "version": PROTOCOL_VERSION,
                "sandbox": applied,
                "extras": list(_sandbox.EXTRAS) if mode != "off" else [],
                "v8_flags": flags,
            }
        )

    def run(self) -> None:
        # `init` is read here, on the main thread, before any other thread exists. The sandbox
        # applies in `_init`, and its user-namespace layer (`unshare(CLONE_NEWUSER)`) is refused
        # by the kernel in a multi-threaded process. The reader thread starts only afterwards;
        # whatever the parent sends in the meantime waits in the pipe.
        try:
            payload = self._reader.read()
            first = _wire.loads(payload) if payload is not None else None
        except (_wire.WireError, OSError):
            os._exit(2)
        if first is None or first.get("t") != "init":
            os._exit(2)
        try:
            self._init(first)
        except BaseException as exc:  # noqa: BLE001
            self._writer.send(
                {
                    "t": "error",
                    "id": 0,
                    "kind": "RuntimeError",
                    "msg": f"init failed: {exc}",
                }
            )
            os._exit(3)
        threading.Thread(
            target=self._read_loop, name="pydeno-worker-reader", daemon=True
        ).start()
        io = self._io
        while True:
            message = self._next_command()
            if message["t"] == "close":
                break
            with io:
                self._in_command = True
                # Replies still owed from before: the helper reads them now.
                if self._pending:
                    io.notify_all()
            try:
                self._run_command(message)
            finally:
                with io:
                    self._in_command = False
        try:
            self._rt().close()
        except BaseException:  # noqa: BLE001, S110
            pass
        os._exit(0)


def _watch(limit: int | None, read_rss: Callable[[], int | None], parent: int) -> None:
    """Exit the moment this process goes over its memory budget (with a dedicated code), or the
    moment its parent is gone.

    Memory is sampled every 20ms from inside, so this reacts faster than the parent's poll and
    keeps working if the parent is slow; the parent's check remains as a backstop. The parent
    check is what ends a guest's endless loop after a `kill -9` of the parent: nothing reads the
    pipe (and so sees it close) while a command runs with no host call in flight. An orphan is
    re-parented, so its parent pid changes.
    """
    while True:
        if os.getppid() != parent:
            os._exit(0)
        if limit is not None:
            rss = read_rss()
            if rss is not None and rss > limit:
                os._exit(_sandbox.MEMORY_EXIT_CODE)
        time.sleep(_MEMORY_POLL_SECONDS)


def main() -> None:
    # fd 0/1 become private duplicates; the real fds are pointed away so stray
    # prints (ours or V8's) can neither corrupt the protocol nor block on it.
    in_fd, out_fd = os.dup(0), os.dup(1)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _Worker(in_fd, out_fd).run()


if __name__ == "__main__":
    main()
