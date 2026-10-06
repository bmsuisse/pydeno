"""`call_bytes`: byte buffers in and out of a trusted WebAssembly module (issue #37).

Convention under test: the module exports `memory`, `alloc(len) -> ptr`, `dealloc(ptr, len)` and a
function `(in_ptr, in_len, out_ptr, out_cap) -> i32` (bytes written, negative on error). The host
copies the input in, bounds the output by `max_result_bytes`, copies it out and frees both blocks.
"""

from __future__ import annotations

import pytest

from pydeno import JavaScriptError, Runtime, RuntimeConfig, RuntimeTimeout
from pydeno._aio import AsyncIsolatedRuntime
from pydeno._isolated import IsolatedRuntime
from pydeno._wasm import MAX_BUFFER_BYTES

from test_wasm import I32, _leb, _name, _section, _vec

# Bump allocator over a mutable global (starts at 1024); `dealloc` frees nothing but counts calls in
# global 1, so the tests can see that both blocks are handed back.
ALLOC = b"\x23\x00\x23\x00\x20\x00\x6a\x24\x00"  # ptr = heap; heap += n; (leaves ptr)
DEALLOC = b"\x23\x01\x41\x01\x6a\x24\x01"  # frees += 1
# xor55: out[i] = in[i] ^ 0x55 for i < in_len; -1 when in_len > out_cap; returns in_len.
XOR55 = (
    b"\x20\x01\x20\x03\x4b\x04\x40\x41\x7f\x0f\x0b"
    b"\x02\x40\x03\x40"
    b"\x20\x04\x20\x01\x4f\x0d\x01"
    b"\x20\x02\x20\x04\x6a"
    b"\x20\x00\x20\x04\x6a\x2d\x00\x00\x41\xd5\x00\x73\x3a\x00\x00"
    b"\x20\x04\x41\x01\x6a\x21\x04"
    b"\x0c\x00\x0b\x0b"
    b"\x20\x01"
)
SPIN = b"\x03\x40\x0c\x00\x0b\x41\x00"


def _sleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if (value == 0 and not byte & 0x40) or (value == -1 and byte & 0x40):
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def const(value: int) -> bytes:
    return b"\x41" + _sleb(value)


def buffer_module(
    extra: list[tuple[str, bytes]], *, alloc: bytes | None = ALLOC, memory: bool = True
) -> bytes:
    """(40 pages of memory.) Functions: alloc, dealloc, `freed() -> i32`, then `extra` (each the buffer signature)."""
    funcs: list[tuple[str, list[int], list[int], bytes]] = []
    if alloc is not None:
        funcs.append(("alloc", [I32], [I32], alloc))
        funcs.append(("dealloc", [I32, I32], [], DEALLOC))
    funcs.append(("freed", [], [I32], b"\x23\x01"))
    funcs += [(n, [I32] * 4, [I32], code) for n, code in extra]
    types = [
        b"\x60" + _vec([bytes([p]) for p in ps]) + _vec([bytes([r]) for r in rs])
        for _, ps, rs, _ in funcs
    ]
    out = b"\x00asm\x01\x00\x00\x00" + _section(1, _vec(types))
    out += _section(3, _vec([_leb(i) for i in range(len(funcs))]))
    out += _section(5, _vec([b"\x00\x28"]))  # 40 pages, 2.5 MiB
    out += _section(
        6,
        _vec([b"\x7f\x01" + const(1024) + b"\x0b", b"\x7f\x01" + const(0) + b"\x0b"]),
    )
    exports = [_name(n) + b"\x00" + _leb(i) for i, (n, *_r) in enumerate(funcs)]
    if memory:
        exports.append(_name("memory") + b"\x02\x00")
    out += _section(7, _vec(exports))
    bodies = []
    for *_r, code in funcs:
        body = b"\x01\x01\x7f" + code + b"\x0b"
        bodies.append(_leb(len(body)) + body)
    return out + _section(10, _vec(bodies))


MOD = buffer_module(
    [
        ("xor55", XOR55),
        ("fail", const(-7)),
        ("overclaim", const(100_000)),  # claims more than the cap it was given
        ("spin", SPIN),
    ]
)


SMALL = 4096  # the default 1 MiB result block does not fit a bump allocator's few pages twice


@pytest.fixture
def rt():
    with Runtime(RuntimeConfig(timeout=5.0)) as runtime:
        yield runtime


def test_round_trip_is_a_copy(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    data = bytearray(b"hello wasm")
    out = wasm.call_bytes("xor55", data, max_result_bytes=SMALL)
    assert isinstance(out, bytes)
    assert out == bytes(b ^ 0x55 for b in b"hello wasm")
    data[0] = 0  # the host's buffer is its own: nothing is shared with the module
    assert wasm.call_bytes("xor55", out, max_result_bytes=SMALL) == b"hello wasm"
    assert wasm.call_bytes("xor55", b"", max_result_bytes=SMALL) == b""
    assert (
        wasm.call_bytes("xor55", memoryview(b"\x00\xff"), max_result_bytes=SMALL)
        == b"\x55\xaa"
    )
    # Binary-safe: every byte value.
    allb = bytes(range(256)) * 8
    assert wasm.call_bytes("xor55", allb, max_result_bytes=SMALL) == bytes(
        b ^ 0x55 for b in allb
    )


def test_both_blocks_are_freed_each_call(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    assert wasm.call("freed") == 0
    wasm.call_bytes("xor55", b"abc", max_result_bytes=SMALL)
    wasm.call_bytes("xor55", b"abc", max_result_bytes=SMALL)
    assert wasm.call("freed") == 4
    with pytest.raises(JavaScriptError):
        wasm.call_bytes(
            "fail", b"abc", max_result_bytes=SMALL
        )  # blocks are freed on an error too
    assert wasm.call("freed") == 6


def test_result_is_bounded_by_max_result_bytes(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    # xor55 refuses (code -1) when the input does not fit the output block it was given.
    with pytest.raises(JavaScriptError, match="code -1"):
        wasm.call_bytes("xor55", b"x" * 10, max_result_bytes=9)
    assert wasm.call_bytes("xor55", b"x" * 10, max_result_bytes=10) == b"\x2d" * 10
    # A module that claims to have written more than the cap is refused, not truncated or trusted.
    with pytest.raises(JavaScriptError, match="max_result_bytes"):
        wasm.call_bytes("overclaim", b"", max_result_bytes=10)


def test_module_error_code(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    with pytest.raises(JavaScriptError, match="code -7"):
        wasm.call_bytes("fail", b"", max_result_bytes=SMALL)


def test_limits_are_checked_by_the_host(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    with pytest.raises(ValueError, match="max_input_bytes"):
        wasm.call_bytes("xor55", b"x" * 11, max_input_bytes=10)
    with pytest.raises(ValueError, match="max_input_bytes"):
        wasm.call_bytes("xor55", b"", max_input_bytes=MAX_BUFFER_BYTES + 1)
    with pytest.raises(ValueError, match="max_result_bytes"):
        wasm.call_bytes("xor55", b"", max_result_bytes=MAX_BUFFER_BYTES + 1)
    with pytest.raises(ValueError, match="max_result_bytes"):
        wasm.call_bytes("xor55", b"", max_result_bytes=-1)
    with pytest.raises(TypeError, match="max_result_bytes"):
        wasm.call_bytes("xor55", b"", max_result_bytes=True)  # type: ignore[arg-type]
    big = b"\x01" * (MAX_BUFFER_BYTES + 1)
    with pytest.raises(ValueError, match="max_input_bytes"):
        wasm.call_bytes("xor55", big, max_input_bytes=MAX_BUFFER_BYTES)


def test_wrong_kinds_are_refused(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    with pytest.raises(TypeError, match="bytes"):
        wasm.call_bytes("xor55", "text")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bytes"):
        wasm.call_bytes("xor55", [1, 2])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="byte-buffer function"):
        wasm.call_bytes("freed", b"")  # wrong signature
    with pytest.raises(KeyError):
        wasm.call_bytes("nope", b"")
    # The numeric path still works for the same function.
    assert wasm.call("freed") == 0


def test_module_without_the_convention_is_refused(rt: Runtime) -> None:
    no_alloc = rt.load_wasm(buffer_module([("xor55", XOR55)], alloc=None))
    with pytest.raises(TypeError, match="alloc"):
        no_alloc.call_bytes("xor55", b"x")
    no_memory = rt.load_wasm(buffer_module([("xor55", XOR55)], memory=False))
    with pytest.raises(JavaScriptError, match="memory"):
        no_memory.call_bytes("xor55", b"x")


def test_a_wild_alloc_pointer_is_refused(rt: Runtime) -> None:
    wild = rt.load_wasm(buffer_module([("xor55", XOR55)], alloc=const(0x7FFFFFF0)))
    with pytest.raises(JavaScriptError, match="outside the memory"):
        wild.call_bytes("xor55", b"x")
    # Just past the end of the the 40 pages of memory.
    edge = rt.load_wasm(buffer_module([("xor55", XOR55)], alloc=const(40 * 65536 - 1)))
    with pytest.raises(JavaScriptError, match="outside the memory"):
        edge.call_bytes("xor55", b"xy")


def test_unloaded_and_timeout(rt: Runtime) -> None:
    wasm = rt.load_wasm(MOD)
    with pytest.raises(RuntimeTimeout):
        wasm.call_bytes("spin", b"x", timeout=0.2, max_result_bytes=SMALL)
    assert (
        wasm.call_bytes("xor55", b"a", max_result_bytes=SMALL) == b"\x34"
    )  # the runtime survives
    wasm.unload()
    with pytest.raises(RuntimeError, match="unloaded"):
        wasm.call_bytes("xor55", b"a")


def test_isolated_runtime() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=5.0), jitless=False) as rt:
        wasm = rt.load_wasm(MOD)
        assert (
            wasm.call_bytes("xor55", b"abc", max_result_bytes=SMALL) == b"\x34\x37\x36"
        )
        big = bytes(range(256)) * 4096  # 1 MiB crosses the wire both ways
        assert wasm.call_bytes(
            "xor55", big, max_input_bytes=len(big), max_result_bytes=len(big)
        ) == bytes(b ^ 0x55 for b in big)
        with pytest.raises(JavaScriptError, match="code -7"):
            wasm.call_bytes("fail", b"", max_result_bytes=SMALL)
        with pytest.raises(ValueError, match="max_input_bytes"):
            wasm.call_bytes("xor55", b"x" * 11, max_input_bytes=10)
        assert wasm.call("freed") >= 2


@pytest.mark.asyncio
async def test_async_isolated_runtime() -> None:
    async with await AsyncIsolatedRuntime.create(
        RuntimeConfig(timeout=5.0), jitless=False
    ) as rt:
        wasm = await rt.load_wasm(MOD)
        assert (
            await wasm.call_bytes("xor55", b"abc", max_result_bytes=SMALL)
            == b"\x34\x37\x36"
        )
        with pytest.raises(JavaScriptError, match="code -7"):
            await wasm.call_bytes("fail", b"", max_result_bytes=SMALL)
        with pytest.raises(ValueError, match="max_result_bytes"):
            await wasm.call_bytes("xor55", b"", max_result_bytes=-1)
