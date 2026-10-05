"""`load_wasm` on `IsolatedRuntime` and `AsyncIsolatedRuntime` (issue #37).

WebAssembly needs `jitless=False`; with the default (jitless) worker `load_wasm` is refused in the
parent and nothing about that worker changes. The modules come from `test_wasm.py`, which
assembles them byte by byte.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from pydeno import JavaScriptError, RuntimeConfig, RuntimeTimeout, WorkerCrashed
from pydeno._aio import AsyncIsolatedRuntime
from pydeno._isolated import IsolatedRuntime
from pydeno._wasm import AsyncWasmModule, WasmModule

from test_wasm import ADD, ADD64, I32, SPIN, TRAP, WITH_IMPORT, module

MIB = 1024 * 1024

# One page of memory and `fill(pages)`: grow by `pages`, then write every byte of them
# (memory.grow; memory.fill 0 .. pages * 64 KiB with 0xAB), so the pages are really committed.
FILL = module(
    [
        (
            "fill",
            [I32],
            [],
            b"\x20\x00\x40\x00\x1a"  # local.get 0; memory.grow; drop
            b"\x41\x00\x41\xab\x01"  # i32.const 0; i32.const 0xAB
            b"\x20\x00\x41\x10\x74"  # local.get 0; i32.const 16; i32.shl
            b"\xfc\x0b\x00",  # memory.fill
        )
    ],
    memory_pages=1,
)


def _jit(**kwargs: object) -> IsolatedRuntime:
    return IsolatedRuntime(RuntimeConfig(timeout=5.0), jitless=False, **kwargs)  # type: ignore[arg-type]


def test_the_default_jitless_worker_refuses_and_is_unchanged() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
        with pytest.raises(RuntimeError, match="jitless=False") as info:
            rt.load_wasm(ADD)
        assert "attack surface" in str(info.value)
        # No round trip happened and the guest's surface is what it was.
        assert rt.eval("typeof WebAssembly") == "undefined"
        assert rt.eval("typeof __pydeno_wasm_load") == "undefined"
        assert rt.eval("1 + 1") == 2


def test_round_trip_with_jitless_false(tmp_path: Path) -> None:
    with _jit() as rt:
        wasm = rt.load_wasm(ADD)
        assert isinstance(wasm, WasmModule)
        assert wasm.call("add", 2, 3) == 5
        assert wasm.exports["add"](40, 2) == 42
        assert wasm.signatures == {"add": (("i32", "i32"), ("i32",))}
        assert rt.load_wasm(ADD64).call("add64", 2**62, 2**62) == -(2**63)
        path = tmp_path / "add.wasm"
        path.write_bytes(ADD)
        assert rt.load_wasm(path).call("add", 1, 1) == 2
        wasm.unload()
        with pytest.raises(RuntimeError, match="unloaded"):
            wasm.call("add", 1, 2)
        # The guest never sees the instance; it can only call the (fixed) loader itself.
        assert rt.eval("typeof add") == "undefined"


def test_invalid_bytes_and_limits_leave_the_worker_usable() -> None:
    with _jit() as rt:
        for data in (
            b"",
            b"junk",
            ADD[:-1],
            b"\x00asm\x01\x00\x00\x00\x01\xff\xff\xff\xff\x0f",
        ):
            with pytest.raises((ValueError, JavaScriptError)):
                rt.load_wasm(data)
        with pytest.raises(ValueError, match="imports"):
            rt.load_wasm(WITH_IMPORT)
        with pytest.raises(ValueError, match="max_bytes"):
            rt.load_wasm(ADD, max_bytes=8)
        rng = random.Random(370)
        for _ in range(60):
            data = bytearray(ADD)
            data[rng.randrange(8, len(data))] = rng.randrange(256)
            try:
                rt.load_wasm(bytes(data))
            except (ValueError, JavaScriptError):
                pass
        with pytest.raises(JavaScriptError, match="unreachable"):
            rt.load_wasm(TRAP).call("boom")
        assert not rt.is_closed()
        assert rt.eval("1 + 1") == 2


def test_an_endless_loop_hits_the_timeout() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=1.0), jitless=False) as rt:
        wasm = rt.load_wasm(SPIN)
        with pytest.raises(RuntimeTimeout):
            wasm.call("spin")


def test_linear_memory_counts_against_max_memory() -> None:
    # `max_buffer_bytes` does not see WebAssembly memory; the worker's RSS limit does.
    with _jit(max_memory=256 * MIB) as rt:
        wasm = rt.load_wasm(FILL)
        assert wasm.call("fill", 16) is None  # 1 MiB is fine
        with pytest.raises(WorkerCrashed):
            wasm.call("fill", 8192)  # 512 MiB is not
        assert rt.is_closed()


async def test_async_refuses_when_jitless() -> None:
    async with await AsyncIsolatedRuntime.create(RuntimeConfig(timeout=5.0)) as rt:
        with pytest.raises(RuntimeError, match="jitless=False"):
            await rt.load_wasm(ADD)
        assert await rt.eval("1 + 1") == 2


async def test_async_round_trip() -> None:
    async with await AsyncIsolatedRuntime.create(
        RuntimeConfig(timeout=5.0), jitless=False
    ) as rt:
        wasm = await rt.load_wasm(ADD)
        assert isinstance(wasm, AsyncWasmModule)
        assert await wasm.call("add", 2, 3) == 5
        assert await wasm.exports["add"](40, 2) == 42
        with pytest.raises(TypeError):
            await wasm.call("add", "x", 1)
        with pytest.raises((ValueError, JavaScriptError)):
            await rt.load_wasm(b"\x00asm\x01\x00\x00\x00\x01")
        async with await rt.load_wasm(ADD64) as wasm64:
            assert await wasm64.call("add64", 1, 2) == 3
        with pytest.raises(RuntimeError, match="unloaded"):
            await wasm64.call("add64", 1, 2)
        await wasm.unload()
        assert await rt.eval("1 + 1") == 2
