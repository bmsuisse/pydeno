"""`Runtime.load_wasm`: a trusted WebAssembly module loaded by the host (issue #37).

The modules are assembled here, byte by byte, so the tests need no toolchain. `ADD` is written out
literally; the others go through `module()`, which only adds the section headers and lengths.
The isolated runtimes are covered in `test_isolated_wasm.py`.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from pydeno import JavaScriptError, Runtime, RuntimeConfig, RuntimeTimeout
from pydeno._wasm import MAX_WASM_BYTES, WasmModule

# (module (func (export "add") (param i32 i32) (result i32) local.get 0 local.get 1 i32.add))
ADD = bytes(
    [
        *(0x00, 0x61, 0x73, 0x6D, 0x01, 0x00, 0x00, 0x00),  # "\0asm", version 1
        *(
            0x01,
            0x07,
            0x01,
            0x60,
            0x02,
            0x7F,
            0x7F,
            0x01,
            0x7F,
        ),  # type 0: (i32, i32) -> i32
        *(0x03, 0x02, 0x01, 0x00),  # function 0 has type 0
        *(
            0x07,
            0x07,
            0x01,
            0x03,
            0x61,
            0x64,
            0x64,
            0x00,
            0x00,
        ),  # export "add" = function 0
        *(0x0A, 0x09, 0x01, 0x07, 0x00, 0x20, 0x00, 0x20, 0x01, 0x6A, 0x0B),  # its body
    ]
)

I32, I64, F32, F64 = 0x7F, 0x7E, 0x7D, 0x7C


def _leb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _vec(items: list[bytes]) -> bytes:
    return _leb(len(items)) + b"".join(items)


def _section(section_id: int, payload: bytes) -> bytes:
    return bytes([section_id]) + _leb(len(payload)) + payload


def _name(text: str) -> bytes:
    raw = text.encode()
    return _leb(len(raw)) + raw


def module(
    functions: list[tuple[str, list[int], list[int], bytes]],
    *,
    memory_pages: int | None = None,
    imports: bytes = b"",
    locals_: bytes = b"\x00",
) -> bytes:
    """A module of exported functions `(name, params, results, code)`; `code` excludes the final
    `end`. Every function gets the same locals declaration."""
    types = [
        b"\x60"
        + _vec([bytes([p]) for p in params])
        + _vec([bytes([r]) for r in results])
        for _, params, results, _ in functions
    ]
    out = b"\x00asm\x01\x00\x00\x00" + _section(1, _vec(types))
    if imports:
        out += _section(2, imports)
    out += _section(3, _vec([_leb(i) for i in range(len(functions))]))
    if memory_pages is not None:
        out += _section(5, _vec([b"\x00" + _leb(memory_pages)]))
    exports = [
        _name(name) + b"\x00" + _leb(i) for i, (name, *_rest) in enumerate(functions)
    ]
    out += _section(7, _vec(exports))
    bodies = []
    for *_rest, code in functions:
        body = locals_ + code + b"\x0b"
        bodies.append(_leb(len(body)) + body)
    return out + _section(10, _vec(bodies))


ADD64 = module([("add64", [I64, I64], [I64], b"\x20\x00\x20\x01\x7c")])
ADDF64 = module([("addf", [F64, F64], [F64], b"\x20\x00\x20\x01\xa0")])
SPIN = module([("spin", [], [], b"\x03\x40\x0c\x00\x0b")])  # loop br 0 end
TRAP = module([("boom", [], [], b"\x00")])  # unreachable
# (import "env" "f" (func)) -- refused: nothing host-side is wired into WebAssembly.
WITH_IMPORT = module(
    [("add", [I32, I32], [I32], b"\x20\x00\x20\x01\x6a")],
    imports=_vec([_name("env") + _name("f") + b"\x00\x00"]),
)


@pytest.fixture
def rt():
    with Runtime(RuntimeConfig(timeout=5.0)) as runtime:
        yield runtime


def test_the_hand_assembled_add_round_trips(rt: Runtime) -> None:
    wasm = rt.load_wasm(ADD)
    assert isinstance(wasm, WasmModule)
    assert wasm.call("add", 2, 3) == 5
    assert wasm.exports["add"](40, 2) == 42
    assert set(wasm.exports) == {"add"}
    assert wasm.signatures == {"add": (("i32", "i32"), ("i32",))}
    # i32 arithmetic wraps, as WebAssembly defines it.
    assert wasm.call("add", 2**31 - 1, 1) == -(2**31)


def test_i64_parameters_take_python_ints_of_any_size(rt: Runtime) -> None:
    wasm = rt.load_wasm(ADD64)
    assert wasm.call("add64", 1, 2) == 3  # small ints still cross as BigInt
    assert wasm.call("add64", 2**53, 1) == 2**53 + 1
    # Exact past 2**53, where the ordinary int conversion would round to a double.
    assert wasm.call("add64", 2**53 + 1, 0) == 2**53 + 1
    assert wasm.call("add64", 2**63 - 1, 0) == 2**63 - 1
    assert wasm.call("add64", 2**64 - 1, 0) == -1  # the unsigned spelling of -1
    assert wasm.call("add64", 2**63 - 1, 1) == -(2**63)


def test_floats(rt: Runtime) -> None:
    wasm = rt.load_wasm(ADDF64)
    assert wasm.call("addf", 0.5, 2) == 2.5
    # An integral f64 result is still a float (the result conversion alone would give an int).
    result = wasm.call("addf", 1.0, 1)
    assert result == 2.0 and isinstance(result, float)
    # An int past the float range is a ValueError, like an out-of-range integer.
    with pytest.raises(ValueError, match="f64"):
        wasm.call("addf", 10**400, 1)


def test_a_path_is_read_by_the_host(rt: Runtime, tmp_path: Path) -> None:
    path = tmp_path / "add.wasm"
    path.write_bytes(ADD)
    assert rt.load_wasm(path).call("add", 1, 1) == 2
    assert rt.load_wasm(str(path)).call("add", 1, 2) == 3
    assert rt.load_wasm(bytearray(ADD)).call("add", 2, 2) == 4
    assert rt.load_wasm(memoryview(ADD)).call("add", 2, 3) == 5


def test_size_is_capped(rt: Runtime, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        rt.load_wasm(ADD, max_bytes=len(ADD) - 1)
    assert rt.load_wasm(ADD, max_bytes=len(ADD)).call("add", 1, 1) == 2
    with pytest.raises(ValueError, match="max_bytes"):
        rt.load_wasm(ADD, max_bytes=MAX_WASM_BYTES + 1)
    with pytest.raises(ValueError, match="max_bytes"):
        rt.load_wasm(ADD, max_bytes=0)
    with pytest.raises(TypeError):
        rt.load_wasm(ADD, max_bytes=True)  # type: ignore[arg-type]
    big = tmp_path / "big.wasm"
    big.write_bytes(ADD + b"\x00" * MAX_WASM_BYTES)
    with pytest.raises(ValueError, match="max_bytes"):
        rt.load_wasm(big)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not wasm at all",
        b"\x00asm\x02\x00\x00\x00",  # unknown version
        ADD[:-1],  # truncated body
        ADD[:12],  # truncated section
        b"\x00asm\x01\x00\x00\x00\x01\xff\xff\xff\xff\x0f",  # section longer than the module
        b"\x00asm\x01\x00\x00\x00\x01\x05\xff\xff\xff\xff\xff",  # LEB128 past 32 bits
    ],
    ids=[
        "empty",
        "text",
        "version",
        "truncated-body",
        "truncated-section",
        "long-section",
        "leb",
    ],
)
def test_invalid_bytes_give_a_clean_error(rt: Runtime, data: bytes) -> None:
    with pytest.raises((ValueError, JavaScriptError)):
        rt.load_wasm(data)
    assert rt.eval("1 + 1") == 2


def test_mutated_modules_fail_cleanly_or_load(rt: Runtime) -> None:
    rng = random.Random(37)
    for _ in range(300):
        data = bytearray(ADD)
        for _ in range(rng.randint(1, 4)):
            data[rng.randrange(8, len(data))] = rng.randrange(256)
        try:
            rt.load_wasm(bytes(data))
        except (ValueError, JavaScriptError):
            pass
    assert rt.eval("1 + 1") == 2


def test_modules_with_imports_are_refused(rt: Runtime) -> None:
    with pytest.raises(ValueError, match="imports"):
        rt.load_wasm(WITH_IMPORT)


def test_not_bytes_is_a_type_error(rt: Runtime) -> None:
    with pytest.raises(TypeError):
        rt.load_wasm(12)  # type: ignore[arg-type]


def test_arguments_are_checked_against_the_signature(rt: Runtime) -> None:
    wasm = rt.load_wasm(ADD)
    with pytest.raises(TypeError, match="2 arguments"):
        wasm.call("add", 1)
    with pytest.raises(TypeError):
        wasm.call("add", True, 1)
    with pytest.raises(TypeError):
        wasm.call("add", "1", 1)
    with pytest.raises(TypeError):
        wasm.call("add", 1.5, 1)
    with pytest.raises(ValueError, match="i32"):
        wasm.call("add", 2**32, 1)
    with pytest.raises(KeyError):
        wasm.call("nope")
    wasm64 = rt.load_wasm(ADD64)
    with pytest.raises(ValueError, match="i64"):
        wasm64.call("add64", 2**64, 1)


def test_a_trap_is_a_javascript_error_and_the_runtime_survives(rt: Runtime) -> None:
    wasm = rt.load_wasm(TRAP)
    with pytest.raises(JavaScriptError, match="unreachable"):
        wasm.call("boom")
    assert rt.eval("1 + 1") == 2


def test_an_endless_loop_is_stopped_by_the_timeout(rt: Runtime) -> None:
    wasm = rt.load_wasm(SPIN)
    with pytest.raises(RuntimeTimeout):
        wasm.call("spin", timeout=0.5)
    assert rt.eval("1 + 1") == 2


def test_unload(rt: Runtime) -> None:
    wasm = rt.load_wasm(ADD)
    wasm.unload()
    wasm.unload()  # idempotent
    with pytest.raises(RuntimeError, match="unloaded"):
        wasm.call("add", 1, 2)
    with rt.load_wasm(ADD) as scoped:
        assert scoped.call("add", 1, 2) == 3
    with pytest.raises(RuntimeError, match="unloaded"):
        scoped.call("add", 1, 2)


def test_a_guest_that_replaced_the_webassembly_api_changes_nothing(rt: Runtime) -> None:
    rt.eval(
        """
        globalThis.seen = [];
        WebAssembly.Module = function (bytes) { seen.push(bytes); throw new Error('guest'); };
        WebAssembly.Instance = function () { throw new Error('guest'); };
        WebAssembly.Module.imports = () => [];
        Object.defineProperty(WebAssembly.Instance.prototype, 'exports',
          { get() { return { add: () => 666 }; } });
        Object.defineProperty(Object.prototype, 'add', { get() { return () => 667; } });
        Array.prototype[0] = 'guest';
        0
        """
    )
    wasm = rt.load_wasm(ADD)
    assert wasm.call("add", 2, 3) == 5
    assert rt.eval("seen.length") == 0


def test_the_loader_global_is_fixed_and_hidden(rt: Runtime) -> None:
    assert rt.eval("typeof __pydeno_wasm_load") == "function"
    rt.eval("__pydeno_wasm_load = () => 'hijacked'; 0")
    assert rt.eval("delete globalThis.__pydeno_wasm_load") is False
    assert rt.eval("Object.keys(globalThis).includes('__pydeno_wasm_load')") is False
    assert rt.eval("Object.isFrozen(__pydeno_wasm_load)") is True
    assert rt.load_wasm(ADD).call("add", 1, 2) == 3


@pytest.mark.parametrize(
    "value", [10**100, -(10**100), 2**128 - 2**104 + 1, 1e100, -1e100]
)
def test_f32_arguments_outside_the_finite_range_raise(
    rt: Runtime, value: int | float
) -> None:
    wasm = rt.load_wasm(module([("identity", [F32], [F32], b"\x20\x00")]))
    with pytest.raises(ValueError, match="f32"):
        wasm.call("identity", value)


def test_f32_boundaries_and_explicit_nonfinite_arguments(rt: Runtime) -> None:
    wasm = rt.load_wasm(module([("identity", [F32], [F32], b"\x20\x00")]))
    maximum = 2**128 - 2**104
    for value in (maximum, -maximum, float(maximum), -float(maximum), 0.5, 1e-50):
        result = wasm.call("identity", value)
        assert result == (0.0 if value == 1e-50 else value)
    for value in (math.inf, -math.inf):
        assert wasm.call("identity", value) == value
    assert math.isnan(wasm.call("identity", math.nan))
