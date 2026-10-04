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
import datetime  # noqa: F401
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
from . import _wire
from ._pydeno import (
    JavaScriptError,
    Runtime,
    RuntimeConfig,
    RuntimeForceKilled,
    RuntimeTerminated,
    RuntimeTimeout,
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


class _Worker:
    def __init__(self, in_fd: int, out_fd: int) -> None:
        self._reader = _wire.FrameReader(in_fd)
        self._writer = _wire.FrameWriter(out_fd)
        self._commands: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._pending: dict[int, concurrent.futures.Future[Any]] = {}
        self._pending_lock = threading.Lock()
        self._call_ids = itertools.count(1)
        self._runtime: Runtime | None = None

    # -- transport ---------------------------------------------------------

    def _read_loop(self) -> None:
        """Demultiplex the parent's frames: replies wake stubs, the rest queue up."""
        try:
            while True:
                payload = self._reader.read()
                if payload is None:
                    break
                message = _wire.loads(payload)
                if message["t"] == "reply":
                    try:
                        self._resolve(message)
                    except Exception:  # noqa: BLE001
                        # One bad reply must never end this thread: it is the only reader, so
                        # every later reply, and so every later command, would wait forever.
                        continue
                else:
                    self._commands.put(message)
        except (_wire.WireError, OSError):
            pass
        # The parent is gone or broke protocol: nothing here is worth keeping alive.
        os._exit(0)

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
        try:
            self._writer.send(
                {
                    "t": "call",
                    "cid": cid,
                    "hid": hid,
                    "args": [_wire.Enc(a) for a in args],
                }
            )
        except _wire.WireError as exc:
            with self._pending_lock:
                self._pending.pop(cid, None)
            raise TypeError(str(exc)) from None
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
        cannot cross the boundary must never break the guest, so every failure is dropped."""

        def console(level: str, args: list[Any]) -> None:
            try:
                self._call_host(hid, (level, args)).result()
            except Exception:  # noqa: BLE001, S110
                pass

        return console

    # -- commands ----------------------------------------------------------

    def _rt(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("worker is not initialised")
        return self._runtime

    def _handle(self, message: dict[str, Any]) -> Any:
        kind = message["t"]
        if kind == "eval":
            return self._rt().eval(message["code"])
        if kind == "eval_async":
            # `eval_async` binds to the running loop when *called*, so it must be
            # called from inside the coroutine, not handed to asyncio.run.
            async def run() -> Any:
                return await self._rt().eval_async(
                    message["code"], timeout=message.get("timeout")
                )

            return asyncio.run(run())
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

            async def run_module() -> Any:
                return await self._rt().eval_module_async(
                    message["specifier"], timeout=message.get("timeout")
                )

            return asyncio.run(run_module())
        raise _wire.WireError(f"unknown command {kind!r}")

    def _run_command(self, message: dict[str, Any]) -> None:
        cmd_id = message.get("id")
        try:
            result = self._handle(message)
            reply: dict[str, Any] = {
                "t": "result",
                "id": cmd_id,
                "v": _wire.Enc(result),
            }
        except _wire.WireError as exc:
            reply = {"t": "error", "id": cmd_id, "kind": "TypeError", "msg": str(exc)}
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
                {"t": "error", "id": cmd_id, "kind": "TypeError", "msg": str(exc)}
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
        applied = (
            "none"
            if mode == "off"
            else _sandbox.apply(
                empty_root=bool(options.get("empty_root", True)),
                # A jitless V8 never maps memory executable, so refuse it: an exploit then has to
                # work without injecting code.
                allow_exec="--jitless" not in flags,
            )
        )
        if applied != "none" and not _sandbox.missing_layers(applied):
            # Ask the kernel rather than trust the filter lists: if the platform's full sandbox
            # claims to be on and a forbidden operation still works, no guest code may run in
            # this process. (A degraded one, say a kernel without Landlock, is expected to leak.)
            breaches = _sandbox.attest()
            if breaches:
                raise RuntimeError(
                    f"sandbox self-test failed: the worker could still {breaches} "
                    f"(applied: {applied}){_sandbox.seatbelt_note()}"
                )
        if mode == "require":
            # "require" means every layer this platform has, not "at least one": a kernel that
            # lacks Landlock must not be allowed to pass for a fully sandboxed one.
            missing = _sandbox.missing_layers(applied)
            if missing:
                raise RuntimeError(
                    f"an OS sandbox is required but {sorted(missing)} could not be applied "
                    f"here (applied: {applied}){_sandbox.seatbelt_note()}"
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
        if isinstance(max_memory, int) and max_memory > 0:
            threading.Thread(
                target=_watch_memory,
                args=(max_memory, read_rss),
                name="pydeno-worker-memory",
                daemon=True,
            ).start()

        kwargs = {k: config[k] for k in _CONFIG_KEYS if config.get(k) is not None}
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
        self._runtime = Runtime(RuntimeConfig(**kwargs))
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
        while True:
            message = self._commands.get()
            if message is None or message["t"] == "close":
                break
            self._run_command(message)
        try:
            self._rt().close()
        except BaseException:  # noqa: BLE001, S110
            pass
        os._exit(0)


def _watch_memory(limit: int, read_rss: Callable[[], int | None]) -> None:
    """Exit with a dedicated code the moment this process goes over its budget.

    Sampled every 20ms from inside, so it reacts faster than the parent's poll and
    keeps working if the parent is slow; the parent's check remains as a backstop.
    """
    while True:
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
