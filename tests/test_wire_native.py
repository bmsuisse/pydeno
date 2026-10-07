"""The Rust wire codec must be indistinguishable from the Python reference, errors included.

The codec sits on the trust boundary: a compromised worker controls every byte the parent decodes.
So "faster" is only acceptable if it accepts and rejects exactly the same things, with the same
budgets (nodes, depth, hash-collision flood). Each test feeds both implementations the same input
and compares the outcome.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import wire_reference as ref
from wire_reference import native_encode
from pydeno import _wire, undefined

NODES = _wire.MAX_NODES
DEPTH = _wire.MAX_DEPTH


def _outcome(fn, *args) -> tuple[str, Any]:
    try:
        return "ok", fn(*args)
    except _wire.WireError as exc:
        return "error", str(exc)


def _same(a: Any, b: Any) -> bool:
    """Equality that treats NaN as equal to NaN and tells -0.0 from 0.0 and 1 from True."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return (math.isnan(a) and math.isnan(b)) or (
            a == b and math.copysign(1, a) == math.copysign(1, b)
        )
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        # pairwise, not by lookup: a NaN key is not found again by a distinct NaN object
        return len(a) == len(b) and all(
            _same(k1, k2) and _same(v1, v2)
            for (k1, v1), (k2, v2) in zip(a.items(), b.items(), strict=True)
        )
    return a == b


def _decode_both(nodes: list[Any]) -> None:
    py = _outcome(ref.py_decode_values, nodes)
    native = _outcome(_wire.decode_values, nodes)
    assert py[0] == native[0], (nodes, py, native)
    if py[0] == "error":
        assert py[1] == native[1]
    else:
        assert _same(py[1], native[1]), (py, native)


def _encode_both(value: Any) -> None:
    py = _outcome(ref.py_encode_value, value)
    native = _outcome(native_encode, value)
    assert py[0] == native[0], (value, py, native)
    if py[0] == "error":
        assert py[1] == native[1]
    else:
        assert _same(py[1], native[1]), (py, native)


# --- values the guest and host really exchange ------------------------------------------------

LEAVES = [
    None,
    True,
    False,
    0,
    1,
    -1,
    2**53,
    -(2**53),
    2**53 + 1,
    -(2**53) - 1,
    2**200,
    -(2**200),
    0.0,
    -0.0,
    1.5,
    -2.5,
    float("inf"),
    float("-inf"),
    float("nan"),
    1e308,
    5e-324,
    "",
    "x",
    "héllo",
    "日本語😀",
    "$",
    "u",
    b"",
    b"\x00\xff",
    bytearray(b"ab"),
    memoryview(b"xyz"),
    undefined,
    dt.datetime(2024, 3, 5, 12, 30, 1, 123456),
    dt.datetime(2024, 3, 5, tzinfo=dt.timezone.utc),
]


@pytest.mark.parametrize("leaf", LEAVES, ids=repr)
def test_every_kind_of_leaf_encodes_the_same(leaf: Any) -> None:
    _encode_both(leaf)
    _encode_both([leaf, {"k": leaf}])


@pytest.mark.parametrize("leaf", LEAVES, ids=repr)
def test_every_kind_of_leaf_round_trips_the_same(leaf: Any) -> None:
    encoded = ref.py_encode_value(leaf)
    _decode_both([encoded])
    out = _wire.decode_values([native_encode(leaf)])[0]
    if isinstance(leaf, (bytearray, memoryview)):
        assert out == bytes(leaf)
    else:
        assert _same(out, leaf)


def test_collections_and_their_special_cases_encode_the_same() -> None:
    for value in (
        [],
        {},
        set(),
        frozenset({1}),
        (1, 2),
        [[], {}],
        {"a": {"b": [1, 2, {"c": None}]}},
        {1, 2, 3},
        {"$": 1},
        {1: "a", 2: "b"},
        {(1, 2): "t"},
        {"a": 1, 2: "mixed"},
        {"$": {"$": 1}},
        [{"$": "int", "v": "5"}],
        {"a": float("nan")},
    ):
        _encode_both(value)


def test_unsupported_types_fail_with_the_same_message() -> None:
    class Custom: ...

    for value in (Custom(), object(), lambda: 1, 1j, range(3), type, _wire):
        _encode_both(value)


def test_nesting_depth_limits_agree_at_the_boundary() -> None:
    def nest(n: int) -> Any:
        v: Any = 1
        for _ in range(n):
            v = [v]
        return v

    for n in (DEPTH - 1, DEPTH, DEPTH + 1, DEPTH + 2):
        _encode_both(nest(n))
        _decode_both([nest(n)])


def test_the_node_budget_is_shared_and_agrees_at_the_boundary() -> None:
    for count in (NODES - 5, NODES - 1, NODES, NODES + 1):
        _decode_both([[0] * (count - 1)])
    big = [0] * 1_000_000
    _decode_both([big])
    _decode_both([big, big, big])


# --- hostile input: the same verdicts, with the same messages --------------------------------

HOSTILE: list[Any] = [
    {"$": "int", "v": "12345678901234567890"},
    {"$": "int", "v": "1_000"},
    {"$": "int", "v": " 7 "},
    {"$": "int", "v": "٣"},
    {"$": "int", "v": "x"},
    {"$": "int", "v": 5},
    {"$": "int", "v": "9" * 5000},
    {"$": "int", "v": ""},
    {"$": "int", "v": "-0"},
    {"$": "int", "v": "+5"},
    {"$": "f", "v": "nan"},
    {"$": "f", "v": "inf"},
    {"$": "f", "v": "-inf"},
    {"$": "f", "v": "-0"},
    {"$": "f", "v": "0"},
    {"$": "f", "v": []},
    {"$": "f", "v": None},
    {"$": "f", "v": "NaN"},
    {"$": "b", "v": "aGVsbG8="},
    {"$": "b", "v": "aGVsbG8"},
    {"$": "b", "v": "!!"},
    {"$": "b", "v": 5},
    {"$": "b", "v": "aGVs bG8="},
    {"$": "b", "v": "é"},
    {"$": "b", "v": ""},
    {"$": "dt", "v": "2024-03-05T12:30:00"},
    {"$": "dt", "v": "2024-03-05"},
    {"$": "dt", "v": "nope"},
    {"$": "dt", "v": "x" * 100},
    {"$": "dt", "v": 5},
    {"$": "dt", "v": "2024-03-05T12:30:00+01:00"},
    {"$": "set", "v": [1, 2, 3]},
    {"$": "set", "v": []},
    {"$": "set", "v": 5},
    {"$": "set", "v": [[1]]},
    {"$": "set", "v": [{"a": 1}]},
    {"$": "set", "v": [1, 1, 1]},
    {"$": "d", "v": [[1, 2], ["a", "b"]]},
    {"$": "d", "v": [[1]]},
    {"$": "d", "v": [[1, 2, 3]]},
    {"$": "d", "v": 5},
    {"$": "d", "v": [5]},
    {"$": "d", "v": [[[1], 2]]},
    {"$": "d", "v": [[1, 2], [1, 3]]},
    {"$": "u"},
    {"$": "u", "v": 1},
    {"$": "u", "x": 1},
    {"$": "x" * 1000, "v": 1},
    {"$": "zzz", "v": 1},
    {"$": 5, "v": 1},
    {"$": None},
    {"$": ["int"], "v": 1},
    {"$": {}, "v": 1},
    {"$": "int"},
    {"$": "int", "v": 1, "w": 2},
    {"$": "\ud800", "v": 1},
    {"$": "é" * 40, "v": 1},
    2**53 + 1,
    -(2**53) - 1,
    2**64,
    10**30,
    (1, 2),
    {1, 2},
    b"raw",
    object,
    1 + 2j,
    {"a": [1, {"$": "int", "v": "7"}, {"$": "set", "v": [1, 2]}]},
    {"nested": {"$": "zzz", "v": 1}},
]


@pytest.mark.parametrize("node", HOSTILE, ids=lambda n: repr(n)[:50])
def test_hostile_nodes_get_the_same_verdict_and_message(node: Any) -> None:
    _decode_both([node])
    _decode_both([1, node, 2])


def test_hash_collision_floods_are_refused_alike() -> None:
    flood = [(2**61 - 1) * k for k in range(1, 400)]
    for k in (5, 16, 17, 40, 399):
        members = [{"$": "int", "v": str(v)} for v in flood[:k]]
        _decode_both([{"$": "set", "v": members}])
        _decode_both([{"$": "d", "v": [[m, 1] for m in members]}])
    start = time.monotonic()
    assert (
        _outcome(
            _wire.decode_values,
            [{"$": "set", "v": [{"$": "int", "v": str(v)} for v in flood]}],
        )[0]
        == "error"
    )
    assert time.monotonic() - start < 1.0


def test_the_honest_collisions_python_allows_still_decode() -> None:
    assert hash(-1) == hash(-2)
    _decode_both([{"$": "set", "v": [-1, -2]}])


# --- property: arbitrary JSON-ish trees, tagged or not ----------------------------------------

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**60), max_value=2**60),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=12),
)
_tag_values = st.one_of(
    _scalars, st.sampled_from(["nan", "inf", "-inf", "-0", "aGk=", "2024-01-02", "5"])
)
_tags = st.sampled_from(["u", "int", "f", "b", "dt", "set", "d", "zz", "$", ""])
_tagged = st.builds(lambda t, v: {"$": t, "v": v}, _tags, _tag_values)


def _trees(inner: st.SearchStrategy) -> st.SearchStrategy:
    return st.one_of(
        st.lists(inner, max_size=5),
        st.dictionaries(st.text(max_size=4), inner, max_size=4),
        st.builds(
            lambda t, items: {"$": t, "v": items},
            st.sampled_from(["set", "d"]),
            st.lists(inner, max_size=5),
        ),
        st.builds(lambda a, b: {"$": "d", "v": [[a, b]]}, inner, inner),
    )


NODE = st.recursive(st.one_of(_scalars, _tagged), _trees, max_leaves=25)


@settings(max_examples=600, deadline=None, suppress_health_check=list(HealthCheck))
@given(NODE)
def test_arbitrary_trees_decode_the_same(node: Any) -> None:
    _decode_both([node])


_py_values = st.recursive(
    st.one_of(
        _scalars, st.binary(max_size=6), st.just(undefined), st.just(float("nan"))
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.tuples(inner, inner),
        st.dictionaries(st.text(max_size=3), inner, max_size=3),
        st.sets(st.integers(-5, 5), max_size=4),
        st.dictionaries(st.integers(-3, 3), inner, max_size=3),
    ),
    max_leaves=20,
)


@settings(max_examples=600, deadline=None, suppress_health_check=list(HealthCheck))
@given(_py_values)
def test_arbitrary_python_values_encode_and_round_trip_the_same(value: Any) -> None:
    _encode_both(value)
    decoded = _wire.decode_values([native_encode(value)])[0]
    assert _same(decoded, ref.py_decode_values([ref.py_encode_value(value)])[0])


# --- the fused JSON layer: parse + decode, encode + write ---------------------------------------
#
# `dumps` and `loads_decoded` have Python reference versions (`py_dumps`, `py_loads_decoded`). The
# bytes they produce may differ (float and escape spelling), so outputs are compared after parsing;
# what must agree is the *meaning*: the same values out, and the same accept/reject verdict.


def _message(kind: str, **fields: Any) -> dict[str, Any]:
    return {"t": kind, **fields}


def _semantic(data: bytes) -> Any:
    return json.loads(data)


@pytest.mark.parametrize("leaf", LEAVES, ids=repr)
def test_native_dumps_means_the_same_as_the_reference(leaf: Any) -> None:
    msg = _message("result", id=7, v=_wire.Enc(leaf))
    assert _same(_semantic(_wire.dumps(msg)), _semantic(ref.py_dumps(msg)))
    call = _message("call", cid=1, hid=2, args=[_wire.Enc(leaf), _wire.Enc([leaf])])
    assert _same(_semantic(_wire.dumps(call)), _semantic(ref.py_dumps(call)))


@pytest.mark.parametrize("leaf", LEAVES, ids=repr)
def test_a_native_frame_decodes_to_the_value_that_went_in(leaf: Any) -> None:
    out = _wire.loads_decoded(_wire.dumps(_message("result", id=1, v=_wire.Enc(leaf))))[
        "v"
    ]
    if isinstance(leaf, (bytearray, memoryview)):
        assert out == bytes(leaf)
    else:
        assert _same(out, leaf)


def test_dumps_handles_the_plain_parts_of_a_message() -> None:
    msg = {
        "t": "x",
        "i": 5,
        "neg": -9,
        "big": 2**70,
        "f": 1.5,
        "tiny": 5e-324,
        "huge": 1.7e308,
        "none": None,
        "yes": True,
        "no": False,
        "s": "plain",
        "uni": "héllo 日本語 😀",
        "ctrl": 'a\x00b\x1fc\td\ne"f\\g',
        "nested": {"a": [1, [2, {"b": None}]], "t": (1, 2)},
    }
    assert _semantic(_wire.dumps(msg)) == _semantic(ref.py_dumps(msg))


def test_lone_surrogates_survive_both_ways() -> None:
    """JavaScript strings may hold them; Python's json round-trips them; so must we."""
    for text in ("\ud800", "a\udc00b", "\ud83d", "😀", "x\ud800\ud800y", "􏿿"):
        sent = _message("result", id=1, v=_wire.Enc(text))
        out = _wire.loads_decoded(_wire.dumps(sent))["v"]
        assert out == text, (text, out)
        assert out == ref.py_loads_decoded(ref.py_dumps(sent))["v"]
        # and in a plain (non-Enc) field and as a dict key
        msg = {"t": "result", "id": 1, "v": _wire.Enc({text: [text]}), "extra": text}
        assert _wire.loads_decoded(_wire.dumps(msg)) == ref.py_loads_decoded(
            ref.py_dumps(msg)
        )


def test_unencodable_values_fail_as_wire_errors_with_the_same_text() -> None:
    class Custom: ...

    for bad in (Custom(), object(), lambda: 1, 1j, range(2)):
        n = _outcome(_wire.dumps, _message("result", id=1, v=_wire.Enc(bad)))
        p = _outcome(ref.py_dumps, _message("result", id=1, v=_wire.Enc(bad)))
        assert n[0] == p[0] == "error"
        assert n[1] == p[1]


def test_dumps_refuses_oversized_messages_and_nan_in_plain_fields() -> None:
    with pytest.raises(_wire.WireError, match="frame cap"):
        _wire.dumps(
            _message("result", id=1, v=_wire.Enc("x" * (_wire.MAX_FRAME_BYTES + 1)))
        )
    with pytest.raises(_wire.WireError):
        _wire.dumps({"t": "x", "n": float("nan")})


def _frame_both(raw: bytes) -> None:
    py = _outcome(ref.py_loads_decoded, raw)
    native = _outcome(_wire.loads_decoded, raw)
    assert py[0] == native[0], (raw[:200], py, native)
    if py[0] == "ok":
        assert _same(py[1], native[1]), (raw[:200], py, native)
    elif not py[1].startswith(("frame ", "non-finite")):
        # decode-level verdicts carry the same message; parse-level ones are worded differently
        assert py[1] == native[1], (raw[:200], py, native)


def _frames(value_json: str) -> list[bytes]:
    frames = [
        f'{{"t":"result","id":1,"v":{value_json}}}',
        f'{{"t":"call","cid":1,"hid":2,"args":[{value_json},{value_json}]}}',
        f' {{ "t" : "result" , "v" : {value_json} , "id" : 1 }} ',
    ]
    return [f.encode() for f in frames]


RAW_VALUES = [
    "null",
    "true",
    "false",
    "0",
    "-0",
    "1",
    "-1",
    "9007199254740992",
    "9007199254740993",
    "-9007199254740993",
    "1" + "0" * 30,
    "-" + "9" * 400,
    "0.5",
    "-0.0",
    "1e5",
    "1E5",
    "1e-5",
    "1.5e+300",
    "1e400",
    "-1e400",
    "5e-324",
    "1e-400",
    '""',
    '"abc"',
    '"a\\nb"',
    '"\\u00e9"',
    '"\\ud83d\\ude00"',
    '"\\ud800"',
    '"\\udc00"',
    '"\\ud800x"',
    '"a\\ud800\\ud800"',
    '"\\/"',
    '"\\u0000"',
    '"日本語"',
    "[]",
    "{}",
    "[1,2,3]",
    '{"a":1}',
    '{"a":1,"a":2}',
    '{"a":{"b":1,"b":1}}',
    "1e999999",
    "-1e999999",
    "[1e999999]",
    '{"$":"f","v":"inf"}',
    '{"$":"int","v":"5"}',
    '{"$":"u"}',
    '{"$":"f","v":"nan"}',
    '{"$":"b","v":"aGk="}',
    '{"$":"dt","v":"2024-01-02T03:04:05"}',
    '{"$":"set","v":[1,2]}',
    '{"$":"d","v":[[1,2],["a","b"]]}',
    '{"$":"zz","v":1}',
    '{"$":5}',
    '{"$":"int","v":"x"}',
    '{"$":"set","v":5}',
    '{"a":{"$":"int","v":"7"}}',
    '[{"$":"u"},{"$":"u"}]',
    '{"$":"set","v":[[1]]}',
    # not JSON at all
    "",
    " ",
    "nul",
    "tru",
    "01",
    "1.",
    ".5",
    "+1",
    "1e",
    "--1",
    "NaN",
    "Infinity",
    "-Infinity",
    "[1,]",
    "[,1]",
    '{"a":1,}',
    "{a:1}",
    "{'a':1}",
    '"unterminated',
    '"bad\\q"',
    '"\\u12"',
    '"\\u12zz"',
    '"\x01"',
    "[1 2]",
    '{"a" 1}',
    '{"a":}',
    "]",
    "}",
    "[",
    "{",
    "[[]",
    '{"a":[}',
]


@pytest.mark.parametrize("value", RAW_VALUES, ids=lambda v: v[:30] or "<empty>")
def test_raw_frames_get_the_same_verdict_and_value(value: str) -> None:
    for raw in _frames(value):
        _frame_both(raw)


def test_malformed_envelopes_and_encodings_get_the_same_verdict() -> None:
    for raw in (
        b"",
        b"{}",
        b"[]",
        b"1",
        b'"t"',
        b"null",
        b'{"t":1}',
        b'{"t":null}',
        b'{"t":["result"]}',
        b'{"t":"result"}',
        b'{"t":"call"}',
        b'{"t":"call","args":5}',
        b'{"t":"call","args":[]}',
        b'{"t":"unknown","v":{"$":"zz"}}',
        b'{"t":"error","kind":"X","msg":"m"}',
        b'{"t":"result","v":"\xff"}',
        b"\xff\xfe",
        b"{\x00}",
        b'{"t":"result","v":1} trailing',
        b'{"t":"result","v":1}{}',
    ):
        _frame_both(raw)


def test_the_native_parser_is_stricter_about_encodings_than_json_loads() -> None:
    """`json.loads` quietly accepts a UTF-8 BOM and UTF-16/32 frames. The protocol is plain UTF-8,
    so anything else is a peer doing something odd, and is refused."""
    for raw in (
        b"\xef\xbb\xbf" + b'{"t":"result","v":1}',
        '{"t":"result","v":"é"}'.encode("utf-16"),
        '{"t":"result","v":"é"}'.encode("utf-32"),
    ):
        assert _outcome(ref.py_loads_decoded, raw)[0] == "ok"
        assert _outcome(_wire.loads_decoded, raw)[0] == "error"


def test_only_the_specified_frame_types_have_their_values_decoded() -> None:
    # an `error` frame's fields are plain data: a tagged-looking value there is just a dict
    raw = b'{"t":"error","id":1,"v":{"$":"zz","v":1},"args":[{"$":"zz"}]}'
    assert _wire.loads_decoded(raw)["v"] == {"$": "zz", "v": 1}
    assert _wire.loads_decoded(raw) == ref.py_loads_decoded(raw)


def test_frame_limits_agree_at_the_boundaries() -> None:
    def nested(n: int) -> str:
        return "[" * n + "]" * n

    for n in (DEPTH - 1, DEPTH, DEPTH + 1, DEPTH + 2, DEPTH + 10, 500):
        for raw in _frames(nested(n)):
            _frame_both(raw)
    for count in (NODES - 70, NODES - 2, NODES, NODES + 2, NODES + 100):
        _frame_both(
            f'{{"t":"result","id":1,"v":[{",".join(["0"] * (count - 1))}]}}'.encode()
        )
    big = ",".join(["0"] * 900_000)
    _frame_both(
        f'{{"t":"call","cid":1,"hid":1,"args":[[{big}],[{big}],[{big}]]}}'.encode()
    )


def test_a_flood_frame_is_refused_before_it_is_built() -> None:
    raw = ('{"t":"result","id":1,"v":[' + ",".join(["[]"] * 3_000_000) + "]}").encode()
    start = time.monotonic()
    assert _outcome(_wire.loads_decoded, raw)[0] == "error"
    assert time.monotonic() - start < 1.5


def test_duplicate_keys_are_refused_everywhere() -> None:
    for value in ('{"a":1,"a":2}', '{"a":1,"b":2,"a":3}', '[{"x":{"k":1,"k":1}}]'):
        for raw in _frames(value):
            _frame_both(raw)
            with pytest.raises(_wire.WireError, match="duplicate object key"):
                _wire.loads_decoded(raw)
    for raw in (
        b'{"t":"result","t":"call","id":1,"v":1}',
        b'{"t":"result","id":1,"v":1,"t":"call"}',
        b'{"t":"result","id":1,"v":{"$":"int","v":"1","v":"2"}}',
        b'{"t":"result","id":1,"v":{"\\u0061":1,"a":2}}',
        b'{"t":"error","id":1,"extra":{"a":1,"a":2}}',
        b'{"t":"result","id":1,"v":{"\\ud800":1,"\\ud800":2}}',
        # more keys than the linear-scan cut-off
        b'{"t":"result","id":1,"v":{'
        + b",".join(b'"k%d":0' % i for i in range(40))
        + b',"k7":1}}',
    ):
        _frame_both(raw)
        with pytest.raises(_wire.WireError):
            _wire.loads_decoded(raw)
    # distinct keys that merely look alike are fine
    ok = b'{"t":"result","id":1,"v":{"a":1,"A":2,"a ":3,"\\ud800":4,"\\ud801":5}}'
    _frame_both(ok)
    assert len(_wire.loads_decoded(ok)["v"]) == 5


def test_out_of_range_number_literals_are_refused() -> None:
    for literal in ("1e999999", "-1e999999", "1e400", "1.8e308", "123456789e301"):
        raw = ('{"t":"result","id":1,"v":%s}' % literal).encode()
        _frame_both(raw)
        with pytest.raises(_wire.WireError, match="non-finite"):
            _wire.loads_decoded(raw)
        raw = ('{"t":"call","cid":1,"hid":2,"args":[[%s]]}' % literal).encode()
        with pytest.raises(_wire.WireError, match="non-finite"):
            _wire.loads_decoded(raw)
    # underflow and the largest finite double are still numbers, and the tagged form still works
    for literal in ("1e-400", "1.7976931348623157e308", "5e-324"):
        _frame_both(('{"t":"result","id":1,"v":%s}' % literal).encode())
    for tag, expected in (("inf", math.inf), ("-inf", -math.inf)):
        raw = ('{"t":"result","id":1,"v":{"$":"f","v":"%s"}}' % tag).encode()
        assert _wire.loads_decoded(raw)["v"] == expected
    assert math.isnan(
        _wire.loads_decoded(b'{"t":"result","id":1,"v":{"$":"f","v":"nan"}}')["v"]
    )


_json_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**62), max_value=2**62),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=10),
    st.text(
        alphabet=st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF), max_size=3
    ),
)


def _json_tree(inner: st.SearchStrategy) -> st.SearchStrategy:
    return st.one_of(
        st.lists(inner, max_size=5),
        st.dictionaries(st.text(max_size=4), inner, max_size=4),
        st.builds(lambda t, v: {"$": t, "v": v}, _tags, inner),
        st.builds(
            lambda t, items: {"$": t, "v": items},
            st.sampled_from(["set", "d"]),
            st.lists(inner, max_size=4),
        ),
    )


_JSON_NODE = st.recursive(_json_leaf, _json_tree, max_leaves=25)


@settings(max_examples=800, deadline=None, suppress_health_check=list(HealthCheck))
@given(_JSON_NODE, st.booleans())
def test_arbitrary_frames_get_the_same_verdict_and_value(
    node: Any, ascii_only: bool
) -> None:
    seps = (",", ":") if ascii_only else (", ", ": ")
    text = json.dumps(node, ensure_ascii=ascii_only, separators=seps)
    # a raw lone surrogate is not UTF-8; `json.loads` takes it anyway, the native parser refuses
    assume(ascii_only or not any(0xD800 <= ord(c) <= 0xDFFF for c in text))
    for raw in _frames(text):
        _frame_both(raw)


@settings(max_examples=500, deadline=None, suppress_health_check=list(HealthCheck))
@given(_py_values)
def test_arbitrary_python_values_round_trip_through_the_fused_layer(value: Any) -> None:
    sent = _message("result", id=1, v=_wire.Enc(value))
    native = _wire.loads_decoded(_wire.dumps(sent))["v"]
    reference = ref.py_loads_decoded(ref.py_dumps(sent))["v"]
    assert _same(native, reference)
    # and each side can read what the other wrote
    assert _same(ref.py_loads_decoded(_wire.dumps(sent))["v"], reference)
    assert _same(_wire.loads_decoded(ref.py_dumps(sent))["v"], reference)
