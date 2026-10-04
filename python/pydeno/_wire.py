"""Wire format between an `IsolatedRuntime` parent and its worker process.

Modelled on pydantic/monty's `monty-proto` (MIT): length-prefixed frames with a
hard cap, and a decoder that treats the peer as untrusted. Everything the
parent reads from a worker goes through `loads`, which enforces a frame size,
a nesting depth, a node count and a strict tag vocabulary, and which never
unpickles or evaluates anything.

Values cross as JSON with a few tagged objects for what JSON cannot express:

    {"$": "int",  "v": "12345678901234567890"}   integers beyond 2**53
    {"$": "f",    "v": "nan" | "inf" | "-inf" | "-0"}
    {"$": "b",    "v": "<base64>"}               bytes
    {"$": "set",  "v": [...]}
    {"$": "dt",   "v": "<isoformat>"}
    {"$": "d",    "v": [[key, value], ...]}      dicts JSON cannot hold as objects
    {"$": "u"}                                   JS `undefined`
"""

from __future__ import annotations

import errno
import json
import os
import select
import struct
import threading
import time

from ._pydeno import WireNativeError as _NativeError
from ._pydeno import _wire_decode_values as _native_decode
from ._pydeno import _wire_dumps as _native_dumps
from ._pydeno import _wire_loads_decoded as _native_loads

# `typing.TYPE_CHECKING` without importing `typing`: the worker imports this module and does not
# otherwise need `typing` (about 2 ms of its start-up on 3.14; older asyncio imports it anyway).
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

# Same order of magnitude as Monty's 256 MiB frame cap, scaled down because
# pydeno's value limits (`max_serialization_bytes`) are smaller.
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 128
MAX_NODES = 2_000_000

_HEADER = struct.Struct("<I")


class WireError(Exception):
    """The peer sent something outside the protocol (or we cannot encode a value)."""


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------


def decode_values(nodes: list[Any]) -> list[Any]:
    """Decode several values (a call's arguments) under ONE shared budget, so a peer cannot
    multiply the node limit by the number of arguments."""
    try:
        return _native_decode(list(nodes), MAX_NODES, MAX_DEPTH)
    except _NativeError as exc:
        raise WireError(str(exc)) from None


def decode_value(node: Any) -> Any:
    """JSON structure from the peer -> Python value, validating every node."""
    return decode_values([node])[0]


# ---------------------------------------------------------------------------
# messages and frames
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise WireError(f"non-finite JSON constant {name}")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


class Enc:
    """Marks a value inside a message that must be written in its wire (tagged) form.

    `dumps` encodes it in the same pass that serialises the message, so a large result is walked
    once, not twice."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def dumps(message: dict[str, Any]) -> bytes:
    """Serialise a message; every `Enc` inside it is written in its wire form, in the same pass."""
    try:
        return _native_dumps(message, Enc, MAX_DEPTH, MAX_FRAME_BYTES)
    except _NativeError as exc:
        raise WireError(str(exc)) from None


def loads(data: bytes) -> dict[str, Any]:
    """Parse one frame into a message dict. Never raises anything but `WireError`."""
    # `json.loads` builds every container before anyone gets to look at it, so a 16 MiB frame of
    # `[],[],[],...` would cost hundreds of MiB. Counting openers is a cheap upper bound on the
    # containers that will exist, and rejects the frame before any of them is allocated.
    if data.count(b"[") + data.count(b"{") > MAX_NODES:
        raise WireError("frame has too many nested values")
    try:
        # What `json.loads(data, parse_constant=...)` does, minus building a new decoder for
        # every frame (that alone is more than half its cost on a small frame).
        message = _DECODER.decode(
            data.decode(json.detect_encoding(data), "surrogatepass")
        )
    except WireError:
        raise
    except (ValueError, RecursionError, MemoryError):
        raise WireError("frame is not valid JSON") from None
    if not isinstance(message, dict) or not isinstance(message.get("t"), str):
        raise WireError("frame is not a message")
    return message


# Frame type -> (keys holding ONE encoded value, keys holding a LIST of encoded values that share
# one budget). Only the frames the parent reads in its hot path: values elsewhere are decoded by
# the code that asks for them.
DECODE_SPEC: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "result": (("v",), ()),
    "call": ((), ("args",)),
}


def loads_decoded(data: bytes) -> dict[str, Any]:
    """`loads`, and the value fields of `DECODE_SPEC` frames decoded in the same pass.

    Never raises anything but `WireError`."""
    try:
        return _native_loads(data, DECODE_SPEC, MAX_NODES, MAX_DEPTH)
    except _NativeError as exc:
        raise WireError(str(exc)) from None


def _wait(fd: int, *, write: bool, timeout: float) -> bool:
    """Whether `fd` becomes ready within `timeout` seconds.

    `poll`, not `select`: `select` raises `ValueError` for any descriptor numbered 1024 or more,
    which a busy host process reaches easily, and that error would escape every handler here.
    """
    poller = select.poll()
    poller.register(fd, select.POLLOUT if write else select.POLLIN)
    return bool(poller.poll(max(0.0, timeout) * 1000.0))


class StalledWrite(OSError):
    """The peer stopped reading and the pipe stayed full past the stall limit."""


class FrameWriter:
    """Thread-safe length-prefixed writer over a file descriptor.

    With `stall_timeout` the descriptor is expected to be non-blocking, and a write that cannot
    finish because the peer is not reading raises `StalledWrite` once the pipe has been full for
    that long. A blocking write would otherwise hang the writing thread forever, and a peer that
    simply stops reading is the cheapest way for a compromised worker to freeze its parent.
    """

    def __init__(self, fd: int, stall_timeout: float | None = None) -> None:
        self._fd = fd
        self._stall = stall_timeout
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """Make every later `send` fail, instead of writing to a descriptor number that the
        process may by then have reused for something else."""
        with self._lock:
            self._fd = -1

    def send(self, message: dict[str, Any]) -> None:
        payload = dumps(message)
        frame = _HEADER.pack(len(payload)) + payload
        with self._lock:
            if self._fd < 0:
                raise BrokenPipeError(errno.EPIPE, "the connection is closed")
            try:
                written = os.write(self._fd, frame)
            except BlockingIOError:
                if self._stall is None:
                    raise
                written = 0
            if written == len(frame):
                return  # the usual case: the whole frame in one write
            view = memoryview(frame)[written:]
            blocked_until: float | None = None
            while view:
                try:
                    written = os.write(self._fd, view)
                except BlockingIOError:
                    if self._stall is None:
                        raise
                    now = time.monotonic()
                    if blocked_until is None:
                        blocked_until = now + self._stall
                    remaining = blocked_until - now
                    if remaining <= 0 or not _wait(
                        self._fd, write=True, timeout=remaining
                    ):
                        raise StalledWrite(
                            errno.EAGAIN,
                            f"the peer stopped reading for {self._stall:g}s",
                        ) from None
                    continue
                view = view[written:]


class FrameReader:
    """Length-prefixed reader over a file descriptor, with an optional deadline."""

    def __init__(self, fd: int, max_frame: int = MAX_FRAME_BYTES) -> None:
        self._fd = fd
        self._max = max_frame
        self._buf = bytearray()
        # Registered on first use (a reader without deadlines needs none), then kept.
        self._poller: Any = None

    def invalidate(self) -> None:
        """Make every later `read` fail with EBADF rather than read a reused descriptor."""
        self._fd = -1

    def _chunk(self, deadline: float | None) -> bytes:
        """One read of up to 64 KiB (empty at EOF); raises `TimeoutError` at the deadline."""
        fd = self._fd
        if fd < 0:
            raise OSError(errno.EBADF, "the connection is closed")
        if deadline is not None:
            remaining = deadline - time.monotonic()
            poller = self._poller
            if poller is None:
                # `poll`, not `select`: see `_wait`.
                poller = self._poller = select.poll()
                poller.register(fd, select.POLLIN)
            if remaining <= 0 or not poller.poll(remaining * 1000.0):
                raise TimeoutError
        return os.read(fd, 1 << 16)

    def _fill(self, deadline: float | None) -> bool:
        """Read more bytes. Returns False on EOF; raises `TimeoutError` at the deadline."""
        chunk = self._chunk(deadline)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def read(self, deadline: float | None = None) -> bytes | None:
        """Next frame payload, or None on a clean EOF between frames."""
        if not self._buf:
            # The common case: nothing buffered, and one read returns exactly one whole frame,
            # which is taken straight from the chunk without a trip through the buffer.
            chunk = self._chunk(deadline)
            if not chunk:
                return None
            if len(chunk) >= _HEADER.size:
                (length,) = _HEADER.unpack_from(chunk)
                if length > self._max:
                    raise WireError(
                        f"frame of {length} bytes exceeds the {self._max} byte cap"
                    )
                if len(chunk) == _HEADER.size + length:
                    return chunk[_HEADER.size :]
            self._buf += chunk
        while len(self._buf) < _HEADER.size:
            if not self._fill(deadline):
                if self._buf:
                    raise WireError("peer closed mid-header")
                return None
        (length,) = _HEADER.unpack_from(self._buf)
        if length > self._max:
            # Checked before buffering the payload: the peer picks `length`.
            raise WireError(f"frame of {length} bytes exceeds the {self._max} byte cap")
        end = _HEADER.size + length
        while len(self._buf) < end:
            if not self._fill(deadline):
                raise WireError("peer closed mid-frame")
        payload = bytes(self._buf[_HEADER.size : end])
        del self._buf[:end]
        return payload
