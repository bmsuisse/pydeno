"""The pure-Python reference implementation of the wire codec.

The shipped codec is native (`src/runtime/wire.rs`, `wire_json.rs`). This is the readable
specification it is tested against (`tests/test_wire_native.py`): same results, same error text,
same budgets. It is not shipped; it exists so the Rust cannot drift unnoticed.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from pydeno._pydeno import JsUndefined, undefined
from pydeno._wire import (
    DECODE_SPEC,
    MAX_DEPTH,
    MAX_FRAME_BYTES,
    MAX_NODES,
    Enc,
    WireError,
    loads,
)

_SAFE_INT = 2**53
_FLOAT_TAGS = {
    "nan": float("nan"),
    "inf": float("inf"),
    "-inf": float("-inf"),
    "-0": -0.0,
}


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


def native_encode(value: Any) -> Any:
    """What the shipped (native) codec writes for `value`, read back as JSON.

    The native encoder has no separate entry point: it is fused with writing the frame (`dumps`),
    so this goes through a one-field message."""
    from pydeno import _wire

    return json.loads(_wire.dumps({"t": "x", "v": Enc(value)}))["v"]
