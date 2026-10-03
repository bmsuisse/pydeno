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

import base64
import binascii
import errno
import json
import os
import re
import select
import struct
import threading
import time
from datetime import datetime
from typing import Any

from ._pydeno import JsUndefined, undefined

try:  # the native codec; an older extension lacks it and the Python code below is used instead
    from ._pydeno import WireNativeError as _NativeError
    from ._pydeno import _wire_decode_values as _native_decode
    from ._pydeno import _wire_dumps as _native_dumps
    from ._pydeno import _wire_encode_value as _native_encode
    from ._pydeno import _wire_loads_decoded as _native_loads
except ImportError:  # pragma: no cover - only with a stale compiled extension
    _native_decode = _native_encode = _native_dumps = _native_loads = _NativeError = (
        None
    )

# Same order of magnitude as Monty's 256 MiB frame cap, scaled down because
# pydeno's value limits (`max_serialization_bytes`) are smaller.
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 128
MAX_NODES = 2_000_000

# A JavaScript global variable name, as accepted in `strip_globals`.
GLOBAL_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,63}")

_SAFE_INT = 2**53
_HEADER = struct.Struct("<I")
_FLOAT_TAGS = {
    "nan": float("nan"),
    "inf": float("inf"),
    "-inf": float("-inf"),
    "-0": -0.0,
}


class WireError(Exception):
    """The peer sent something outside the protocol (or we cannot encode a value)."""


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------


def py_encode_value(value: Any, _depth: int = 0) -> Any:
    """Reference implementation of `encode_value` (also the fallback)."""
    if _depth > MAX_DEPTH:
        raise WireError("value nested too deeply to cross the isolation boundary")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if -_SAFE_INT <= value <= _SAFE_INT:
            return value
        return {"$": "int", "v": str(value)}
    if isinstance(value, float):
        if value != value:
            return {"$": "f", "v": "nan"}
        if value in (float("inf"), float("-inf")):
            return {"$": "f", "v": "inf" if value > 0 else "-inf"}
        if value == 0.0 and str(value).startswith("-"):
            return {"$": "f", "v": "-0"}
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$": "b", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, JsUndefined):
        return {"$": "u"}
    if isinstance(value, datetime):
        return {"$": "dt", "v": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [py_encode_value(item, _depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return {"$": "set", "v": [py_encode_value(item, _depth + 1) for item in value]}
    if isinstance(value, dict):
        if all(isinstance(k, str) and k != "$" for k in value):
            return {k: py_encode_value(v, _depth + 1) for k, v in value.items()}
        return {
            "$": "d",
            "v": [
                [py_encode_value(k, _depth + 1), py_encode_value(v, _depth + 1)]
                for k, v in value.items()
            ],
        }
    raise WireError(f"{type(value).__name__} cannot cross the isolation boundary")


class _Budget:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = MAX_NODES

    def spend(self) -> None:
        self.nodes -= 1
        if self.nodes < 0:
            raise WireError("value has too many nodes")


# How many distinct values may share one hash inside a set or a non-string-keyed dict. A hostile
# peer can pick integers that all hash alike (multiples of 2**61 - 1 do), which makes building
# the set quadratic: seconds for a few thousand values, hours for a frame's worth, on the very
# thread that is supposed to enforce the deadline. Honest data has at most a couple (-1 and -2).
_MAX_HASH_REPEATS = 16


def _reject_hash_flood(values: list[Any]) -> None:
    if len(values) <= _MAX_HASH_REPEATS:
        return
    seen: dict[int, int] = {}
    for value in values:
        h = hash(value)
        count = seen.get(h, 0) + 1
        if count > _MAX_HASH_REPEATS:
            raise WireError("too many values in a collection share a hash")
        seen[h] = count


def py_decode_value(node: Any, _budget: _Budget | None = None, _depth: int = 0) -> Any:
    """Reference implementation of `decode_value` (also the fallback)."""
    budget = _budget or _Budget()
    budget.spend()
    if _depth > MAX_DEPTH:
        raise WireError("value nested too deeply")
    if node is None or isinstance(node, (bool, str)):
        return node
    if isinstance(node, int):
        # A plain JSON number past 2**53 is never what `encode_value` sends (it tags those), and
        # accepting it would let a peer smuggle in integers chosen to collide in a hash table.
        if not -_SAFE_INT <= node <= _SAFE_INT:
            raise WireError("integer outside the safe range must be tagged")
        return node
    if isinstance(node, float):
        return node
    if isinstance(node, list):
        return [py_decode_value(item, budget, _depth + 1) for item in node]
    if not isinstance(node, dict):
        raise WireError("unexpected JSON node")
    if "$" not in node:
        return {k: py_decode_value(v, budget, _depth + 1) for k, v in node.items()}

    tag = node["$"]
    if not isinstance(tag, str):
        raise WireError("malformed tagged value")
    if tag == "u" and len(node) == 1:
        return undefined
    if len(node) != 2 or "v" not in node:
        raise WireError("malformed tagged value")
    payload = node["v"]
    if tag == "int":
        if not isinstance(payload, str) or len(payload) > 4096:
            raise WireError("bad int payload")
        try:
            return int(payload)
        except ValueError:
            raise WireError("bad int payload") from None
    if tag == "f":
        if not isinstance(payload, str) or payload not in _FLOAT_TAGS:
            raise WireError("bad float payload")
        return _FLOAT_TAGS[payload]
    if tag == "b":
        if not isinstance(payload, str):
            raise WireError("bad bytes payload")
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise WireError("bad bytes payload") from None
    if tag == "dt":
        if not isinstance(payload, str) or len(payload) > 64:
            raise WireError("bad datetime payload")
        try:
            return datetime.fromisoformat(payload)
        except ValueError:
            raise WireError("bad datetime payload") from None
    if tag == "set":
        if not isinstance(payload, list):
            raise WireError("bad set payload")
        members = [py_decode_value(item, budget, _depth + 1) for item in payload]
        try:
            _reject_hash_flood(members)
            return set(members)
        except TypeError:
            raise WireError("unhashable set member") from None
    if tag == "d":
        if not isinstance(payload, list):
            raise WireError("bad dict payload")
        keys: list[Any] = []
        values: list[Any] = []
        for pair in payload:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise WireError("bad dict entry")
            keys.append(py_decode_value(pair[0], budget, _depth + 1))
            values.append(py_decode_value(pair[1], budget, _depth + 1))
        try:
            _reject_hash_flood(keys)
            return dict(zip(keys, values, strict=True))
        except TypeError:
            raise WireError("unhashable dict key") from None
    raise WireError(f"unknown tag {tag[:32]!r}")


def py_decode_values(nodes: list[Any]) -> list[Any]:
    """Reference implementation of `decode_values` (also the fallback)."""
    budget = _Budget()
    return [py_decode_value(node, budget) for node in nodes]


def encode_value(value: Any, _depth: int = 0) -> Any:
    """Python value -> JSON-able structure. Raises `WireError` for unsupported types."""
    if _native_encode is None or _depth:
        return py_encode_value(value, _depth)
    try:
        return _native_encode(value, MAX_DEPTH)
    except _NativeError as exc:
        raise WireError(str(exc)) from None


def decode_values(nodes: list[Any]) -> list[Any]:
    """Decode several values (a call's arguments) under ONE shared budget, so a peer cannot
    multiply the node limit by the number of arguments."""
    if _native_decode is None:
        return py_decode_values(nodes)
    try:
        return _native_decode(list(nodes), MAX_NODES, MAX_DEPTH)
    except _NativeError as exc:
        raise WireError(str(exc)) from None


def decode_value(node: Any, _budget: _Budget | None = None, _depth: int = 0) -> Any:
    """JSON structure from the peer -> Python value, validating every node."""
    if _native_decode is None or _budget is not None or _depth:
        return py_decode_value(node, _budget, _depth)
    return decode_values([node])[0]


# ---------------------------------------------------------------------------
# messages and frames
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise WireError(f"non-finite JSON constant {name}")


class Enc:
    """Marks a value inside a message that must be written in its wire (tagged) form.

    `dumps` encodes it in the same pass that serialises the message, so a large result is walked
    once, not twice."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def _enc_default(obj: Any) -> Any:
    if isinstance(obj, Enc):
        return py_encode_value(obj.value)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def py_dumps(message: dict[str, Any]) -> bytes:
    """Reference implementation of `dumps` (also the fallback)."""
    data = json.dumps(
        message, separators=(",", ":"), allow_nan=False, default=_enc_default
    ).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise WireError(
            f"message of {len(data)} bytes exceeds the {MAX_FRAME_BYTES} byte frame cap"
        )
    return data


def dumps(message: dict[str, Any]) -> bytes:
    if _native_dumps is None:
        return py_dumps(message)
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
        message = json.loads(data, parse_constant=_reject_constant)
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


def py_loads_decoded(data: bytes) -> dict[str, Any]:
    """Reference implementation of `loads_decoded` (also the fallback)."""
    message = loads(data)
    single, multi = DECODE_SPEC.get(message["t"], ((), ()))
    for key in single:
        if key in message:
            message[key] = py_decode_value(message[key])
    for key in multi:
        if isinstance(message.get(key), list):
            message[key] = py_decode_values(message[key])
    return message


def loads_decoded(data: bytes) -> dict[str, Any]:
    """`loads`, and the value fields of `DECODE_SPEC` frames decoded in the same pass.

    Never raises anything but `WireError`."""
    if _native_loads is None:
        return py_loads_decoded(data)
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
            view = memoryview(frame)
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

    def invalidate(self) -> None:
        """Make every later `read` fail with EBADF rather than read a reused descriptor."""
        self._fd = -1

    def _fill(self, deadline: float | None) -> bool:
        """Read more bytes. Returns False on EOF; raises `TimeoutError` at the deadline."""
        fd = self._fd
        if fd < 0:
            raise OSError(errno.EBADF, "the connection is closed")
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _wait(fd, write=False, timeout=remaining):
                raise TimeoutError
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def read(self, deadline: float | None = None) -> bytes | None:
        """Next frame payload, or None on a clean EOF between frames."""
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
