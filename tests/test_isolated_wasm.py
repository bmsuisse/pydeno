"""`load_wasm` on `IsolatedRuntime` and `AsyncIsolatedRuntime` (issue #37).

WebAssembly needs `jitless=False`; with the default (jitless) worker `load_wasm` is refused in the
parent and nothing about that worker changes. The modules come from `test_wasm.py`, which
assembles them byte by byte.
"""

from __future__ import annotations

import gc
import random
from pathlib import Path
from typing import Any

import pytest

from pydeno import JavaScriptError, RuntimeConfig, RuntimeTimeout, WorkerCrashed
from pydeno import _wasm, _wire
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
    """`max_buffer_bytes` does not see WebAssembly memory; the worker's RSS limit does.

    The RSS limit is sampled: while a command runs, and once more as its result arrives. A fill
    that finishes quickly is normally caught by that last reading, but under load the reading can
    lag the allocation (the kernel may also hold some of the pages compressed for a while), and
    the call returns. What the design promises is that a worker over `max_memory` is killed by the
    next command at the latest, so that is what is pinned: the memory keeps growing for a few
    calls, and if none of them was killed the next command must be.
    """
    with _jit(max_memory=256 * MIB) as rt:
        wasm = rt.load_wasm(FILL)
        assert wasm.call("fill", 16) is None  # 1 MiB is fine
        with pytest.raises(WorkerCrashed):
            for step in (1, 2, 3):
                wasm.call(
                    "fill", 8192 * step
                )  # 512 MiB, then 1 GiB, then 1.5 GiB touched
            rt.eval("1 + 1")  # the next command at the latest
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


# A guest that defines its own loader global, which can only happen where V8 has no WebAssembly
# (with it, the bridge's global is fixed in place before any guest code).
PLANT = """
globalThis.stolen = null;
globalThis.__pydeno_wasm_load = function (bytes) {
  globalThis.stolen = bytes.length;
  return function () { return 666; };
};
0
"""


def _no_round_trip(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("load_wasm reached the worker")


@pytest.mark.parametrize(
    "flags",
    [[], ["--lite-mode"], ["--lite_mode"], ["--no-jitless", "--jitless"]],
    ids=["jitless", "lite-mode", "lite_mode", "last-wins"],
)
def test_flags_without_webassembly_are_refused_in_the_parent(flags: list[str]) -> None:
    jitless = not flags  # the default worker, or jitless=False plus flags that imply it
    with IsolatedRuntime(
        RuntimeConfig(timeout=5.0), jitless=jitless, v8_flags=flags
    ) as rt:
        rt.eval(PLANT)
        rt._request = _no_round_trip  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="jitless=False"):
            rt.load_wasm(ADD)
        del rt._request
        assert rt.eval("stolen") is None


def test_the_worker_never_calls_a_guest_planted_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even if the parent's flag check missed a flag that removes WebAssembly, the worker uses only
    # the loader it took before any guest code, and has none here.
    monkeypatch.setattr(_wasm, "flags_disable_wasm", lambda flags: False)
    with IsolatedRuntime(
        RuntimeConfig(timeout=5.0), jitless=False, v8_flags=["--lite-mode"]
    ) as rt:
        assert rt.eval("typeof WebAssembly") == "undefined"
        rt.eval(PLANT)
        with pytest.raises(RuntimeError, match="jitless=False"):
            rt.load_wasm(ADD)
        assert rt.eval("stolen") is None


async def test_async_never_calls_a_guest_planted_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with await AsyncIsolatedRuntime.create(
        RuntimeConfig(timeout=5.0), jitless=False, v8_flags=["--lite-mode"]
    ) as rt:
        await rt.eval(PLANT)
        with pytest.raises(RuntimeError, match="jitless=False"):
            await rt.load_wasm(ADD)  # refused in the parent
        monkeypatch.setattr(_wasm, "flags_disable_wasm", lambda flags: False)
        with pytest.raises(RuntimeError, match="jitless=False"):
            await rt.load_wasm(ADD)  # and by the worker
        assert await rt.eval("stolen") is None


def test_a_dropped_module_is_forgotten_by_the_worker() -> None:
    with _jit() as rt:
        wasm = rt.load_wasm(ADD)
        assert wasm.call("add", 1, 2) == 3
        del wasm
        gc.collect()
        assert len(rt._wasm_dropped) == 1
        (wid,) = rt._wasm_dropped
        keep = rt.load_wasm(ADD)  # carries the drop
        assert rt._wasm_dropped == [] and keep.call("add", 2, 2) == 4
        _assert_forgotten(rt, wid)


def _assert_forgotten(rt: IsolatedRuntime, wid: int) -> None:
    with pytest.raises(RuntimeError, match="unloaded"):
        rt._request(
            {
                "t": "wasm_call",
                "wid": wid,
                "name": "add",
                "args": _wire.Enc([1, 2]),
                "wide": [False, False],
            }
        )


class _Interleaved(list[int]):
    """A drop queue on which, once, a finalizer appends and another thread drains right after the
    first read: the worst interleaving of two concurrent drains, made deterministic."""

    def __init__(self, items: list[int]) -> None:
        super().__init__(items)
        self.other: list[int] | None = None

    def _interleave(self) -> None:
        if self.other is None:
            self.other = []
            self.append(99)
            self.other.extend(_wasm.drain(self))

    def __getitem__(self, key: Any) -> Any:
        value = super().__getitem__(key)
        self._interleave()
        return value

    def pop(self, index: Any = -1) -> int:
        value = super().pop(index)
        self._interleave()
        return value


def test_concurrent_drains_take_each_id_once() -> None:
    dropped = _Interleaved([1, 2, 3])
    mine = _wasm.drain(dropped)
    assert dropped.other is not None
    assert sorted(mine + dropped.other + list(dropped)) == [1, 2, 3, 99]


def test_draining_requeues_the_ids_when_the_request_fails() -> None:
    dropped = [1, 2]
    with pytest.raises(OSError), _wasm.draining(dropped) as drop:
        assert drop == [1, 2] and dropped == []
        dropped.append(3)  # a finalizer meanwhile
        raise OSError("not sent")
    assert sorted(dropped) == [1, 2, 3]
    with _wasm.draining(dropped) as drop:
        pass
    assert sorted(drop) == [1, 2, 3] and dropped == []


def test_a_failed_request_keeps_the_dropped_ids_for_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _jit() as rt:
        gone = rt.load_wasm(ADD)
        keep = rt.load_wasm(ADD)
        del gone
        gc.collect()
        (wid,) = rt._wasm_dropped

        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("failed before sending")

        monkeypatch.setattr(rt, "_request", fail)
        with pytest.raises(RuntimeError, match="before sending"):
            keep.call("add", 1, 1)
        with pytest.raises(RuntimeError, match="before sending"):
            rt.load_wasm(ADD)
        assert rt._wasm_dropped == [wid]  # not lost with the failed requests
        monkeypatch.undo()
        assert keep.call("add", 2, 2) == 4  # carries the drop
        assert rt._wasm_dropped == []
        _assert_forgotten(rt, wid)


async def test_async_a_failed_request_keeps_the_dropped_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with await AsyncIsolatedRuntime.create(
        RuntimeConfig(timeout=5.0), jitless=False
    ) as rt:
        gone = await rt.load_wasm(ADD)
        keep = await rt.load_wasm(ADD)
        del gone
        gc.collect()
        (wid,) = rt._wasm_dropped

        async def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("failed before sending")

        monkeypatch.setattr(rt, "_request", fail)
        with pytest.raises(RuntimeError, match="before sending"):
            await keep.call("add", 1, 1)
        assert rt._wasm_dropped == [wid]
        monkeypatch.undo()
        assert await keep.call("add", 2, 2) == 4
        assert rt._wasm_dropped == []
