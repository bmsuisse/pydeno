"""The tool host: the child process behind `ToolProcess` (experimental).

It imports the tool functions it is asked for (`'module:function'`) and serves calls over the
framed wire (`_wire`). It is NOT the JavaScript worker and never sees guest code, only the
arguments of a call the parent forwards. Without `sandbox` it is crash isolation and resource
limits, not confinement: the tool host runs with the authority of the user that started it.

Frames from the parent:
    {"t": "init", "path": [...], "tools": [...], "sandbox": null|"auto"|"require",
     "max_memory": int|null, "max_result_bytes": int, "parent": pid}
    {"t": "call", "cid": n, "tool": "module:function", "args": [...]}

Frames to the parent:
    {"t": "ready", "sandbox": "<layers applied>"}
    {"t": "result", "cid": n, "v": value}
    {"t": "error", "cid": n, "etype": "<class name>", "msg": "<str(exc)>"}
    {"t": "toolarge", "cid": n, "size": bytes}
    {"t": "error", "cid": 0, ...}  -- start-up failed; the process exits
"""

from __future__ import annotations

import asyncio
import base64  # noqa: F401 - the native wire codec imports these on first use, which must not
import binascii  # noqa: F401   happen after the sandbox has closed the filesystem
import concurrent.futures
import concurrent.futures.thread
import contextvars  # noqa: F401
import datetime
import importlib
import inspect
import json  # noqa: F401
import os
import queue  # noqa: F401
import resource
import sys
import threading
import time
import weakref  # noqa: F401
from typing import Any

from . import _sandbox, _wire

# Most synchronous tool calls running at once (each on a thread of its own). More wait their turn;
# asynchronous tools run on one event loop and are not limited by it.
MAX_SYNC_THREADS = 32
# What a thread costs a memory ceiling that counts address space (`RLIMIT_DATA`): its stack.
_THREAD_STACK = 1 << 20
# Room in the kernel ceiling above `max_memory` for what the interpreter and its threads map; the
# parent's resident-memory poll is the exact limit.
_KERNEL_SLACK = 96 << 20
_MAX_MESSAGE = 64 * 1024


def resolve(spec: str) -> Any:
    """The object `'module:attr.path'` names. Raises what the import raises."""
    module_name, _, attr = spec.partition(":")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise TypeError(f"{spec!r} is not callable")
    return obj


class _Host:
    def __init__(self, in_fd: int, out_fd: int) -> None:
        self._reader = _wire.FrameReader(in_fd)
        self._writer = _wire.FrameWriter(out_fd)
        self._tools: dict[str, Any] = {}
        self._import_lock = threading.Lock()
        self._max_result = 1 << 20
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    # -- start-up -----------------------------------------------------------

    def _init(self, message: dict[str, Any]) -> str:
        path = message.get("path")
        if isinstance(path, list):
            keep = [p for p in path if isinstance(p, str)]
            sys.path[:] = keep + [p for p in sys.path if p not in keep]
        result = message.get("max_result_bytes")
        if isinstance(result, int) and not isinstance(result, bool) and result > 0:
            self._max_result = result
        mode = message.get("sandbox")
        max_memory = message.get("max_memory")
        if isinstance(max_memory, int) and not isinstance(max_memory, bool):
            self._limit_data(max_memory)
        applied = "none"
        if mode is not None:
            if mode not in ("auto", "require"):
                raise ValueError(f"unknown sandbox mode {mode!r}")
            tools = message.get("tools")
            # Imported before the filesystem closes: afterwards nothing can be imported.
            for spec in tools if isinstance(tools, list) else []:
                self._tools[spec] = resolve(spec)
            _warm_up_codec()
            read_rss = _sandbox.rss_reader()  # noqa: F841 - opened before the sandbox
            hardened = _sandbox.harden_process()
            applied = _sandbox.apply(empty_root=True, allow_exec=True)
            missing = _sandbox.missing_layers(applied) if applied != "none" else None
            if mode == "require":
                if applied == "none" or missing:
                    raise RuntimeError(
                        f"an OS sandbox is required but {sorted(missing or ['all'])} could not "
                        f"be applied here (applied: {applied}){_sandbox.layer_notes()}"
                    )
                if (
                    sys.platform.startswith("linux")
                    and hardened.get("uid_before") == 0
                    and hardened.get("uid_after") == 0
                ):
                    raise RuntimeError(
                        "an OS sandbox is required but this tool host runs as root and could "
                        "not drop its privileges"
                    )
        threading.stack_size(_THREAD_STACK)
        return applied

    @staticmethod
    def _limit_data(max_memory: int) -> None:
        if not sys.platform.startswith("linux") or max_memory <= 0:
            return
        limit = (
            _sandbox._data_bytes_now()  # noqa: SLF001
            + max_memory
            + _KERNEL_SLACK
            + MAX_SYNC_THREADS * _THREAD_STACK
        )
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_DATA)
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
        except (ValueError, OSError):
            pass

    # -- serving ------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._writer.send(message)
        except OSError:
            os._exit(0)  # the parent is gone

    def _send_error(self, cid: int, exc: BaseException) -> None:
        text = str(exc)
        if len(text) > _MAX_MESSAGE:
            text = text[:_MAX_MESSAGE] + "..."
        # The same name the guest would have seen from a tool run in the parent.
        etype = "TypeError" if isinstance(exc, _wire.WireError) else type(exc).__name__
        self._send({"t": "error", "cid": cid, "etype": etype, "msg": text})

    def _send_value(self, cid: int, value: Any) -> None:
        try:
            payload = _wire.dumps({"t": "result", "cid": cid, "v": _wire.Enc(value)})
        except _wire.WireError as exc:
            self._send_error(cid, exc)
            return
        if len(payload) > self._max_result:
            self._send({"t": "toolarge", "cid": cid, "size": len(payload)})
            return
        try:
            self._writer.send_encoded(payload)
        except OSError:
            os._exit(0)

    def _lookup(self, spec: str) -> Any:
        fn = self._tools.get(spec)
        if fn is None:
            with self._import_lock:
                fn = self._tools.get(spec)
                if fn is None:
                    fn = self._tools[spec] = resolve(spec)
        return fn

    def _run_sync(self, cid: int, fn: Any, args: list[Any]) -> None:
        try:
            value = fn(*args)
            if inspect.isawaitable(value):
                assert self._loop is not None
                value = asyncio.run_coroutine_threadsafe(
                    _await(value), self._loop
                ).result()
        except BaseException as exc:  # noqa: BLE001 - whatever the tool raised is its answer
            self._send_error(cid, exc)
            return
        self._send_value(cid, value)

    def _on_loop_done(self, cid: int, future: concurrent.futures.Future[Any]) -> None:
        try:
            value = future.result()
        except BaseException as exc:  # noqa: BLE001
            self._send_error(cid, exc)
            return
        self._send_value(cid, value)

    def _dispatch(self, message: dict[str, Any]) -> None:
        cid, spec, args = message.get("cid"), message.get("tool"), message.get("args")
        if (
            not isinstance(cid, int)
            or isinstance(cid, bool)
            or not isinstance(spec, str)
            or not isinstance(args, list)
        ):
            os._exit(2)  # a parent that breaks protocol is not one to serve
        try:
            fn = self._lookup(spec)
        except BaseException as exc:  # noqa: BLE001 - an import error is the call's error
            self._send_error(cid, exc)
            return
        assert self._pool is not None and self._loop is not None
        if inspect.iscoroutinefunction(fn):
            future = asyncio.run_coroutine_threadsafe(fn(*args), self._loop)
            future.add_done_callback(lambda f: self._on_loop_done(cid, f))
        else:
            self._pool.submit(self._run_sync, cid, fn, args)

    def run(self) -> None:
        try:
            payload = self._reader.read()
            first = _wire.loads(payload) if payload is not None else None
        except (_wire.WireError, OSError):
            os._exit(2)
        if first is None or first.get("t") != "init":
            os._exit(2)
        try:
            applied = self._init(first)
        except BaseException as exc:  # noqa: BLE001
            self._send_error(0, exc)
            os._exit(3)
        # Threads only now: Landlock and the user-namespace layer need a single-threaded process.
        loop = self._loop = asyncio.new_event_loop()
        threading.Thread(
            target=loop.run_forever, name="pydeno-tool-loop", daemon=True
        ).start()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_SYNC_THREADS, thread_name_prefix="pydeno-tool"
        )
        parent = first.get("parent")
        if isinstance(parent, int) and not isinstance(parent, bool):
            threading.Thread(
                target=_watch_parent,
                args=(parent,),
                name="pydeno-tool-watch",
                daemon=True,
            ).start()
        self._send({"t": "ready", "sandbox": applied})
        while True:
            try:
                payload = self._reader.read()
                message = _wire.loads_decoded(payload) if payload is not None else None
            except (_wire.WireError, OSError):
                os._exit(2)
            if message is None:
                os._exit(0)  # the parent closed our stdin: it is gone or done with us
            if message.get("t") == "call":
                self._dispatch(message)


def _warm_up_codec() -> None:
    """Encode and decode one value of every kind, so the codec's lazy imports happen now."""
    sample = {
        "t": "result",
        "cid": 1,
        "v": _wire.Enc(
            [
                b"x",
                {1, 2},
                datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc),
                2**70,
                float("nan"),
                {1: 3},
            ]
        ),
    }
    _wire.loads_decoded(_wire.dumps(sample))


async def _await(awaitable: Any) -> Any:
    return await awaitable


def _watch_parent(parent: int) -> None:
    """A parent that dies leaves stdin at EOF, which the main loop sees; this is the second
    witness, for a stdin that something else still holds open."""
    while True:
        if os.getppid() != parent:
            os._exit(0)
        time.sleep(0.5)


def main() -> None:
    # fd 0/1 become private duplicates; the real ones are pointed away so a tool's prints (or a
    # C library's) can neither corrupt the protocol nor block on it.
    in_fd, out_fd = os.dup(0), os.dup(1)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _Host(in_fd, out_fd).run()


if __name__ == "__main__":
    main()
