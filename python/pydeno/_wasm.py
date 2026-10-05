"""`load_wasm`: a trusted WebAssembly module, loaded by the host (issue #37).

The host reads the bytes (from memory or a path; the guest never touches a file), checks the size,
and parses just enough of the module to know its exported functions' signatures. The bridge
(`__pydeno_wasm_load` in `src/runtime/ops.rs`) compiles and instantiates it with WebAssembly
intrinsics captured before any guest code ran, and hands back a closure over the exports that only
the host holds. Calls are checked against the signatures here and run under the runtime's timeout;
results come back through the ordinary result conversion.

Only for **trusted** modules: WebAssembly needs V8's JIT (`jitless=False` on the isolated runtimes),
which is a larger attack surface, and a module's linear memory is outside `max_buffer_bytes`.

Imported by the isolation worker before its sandbox goes up, so only the standard library here.
"""

from __future__ import annotations

import os
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import MappingProxyType

TYPE_CHECKING = False
if TYPE_CHECKING:
    from datetime import timedelta
    from typing import Any

#: The most bytes `load_wasm` takes. Base64 on the isolation wire makes 8 MiB about 10.7 MiB, inside
#: one 16 MiB frame.
MAX_WASM_BYTES = 8 * 1024 * 1024

JITLESS_MESSAGE = (
    "load_wasm needs jitless=False: this runtime's V8 has no WebAssembly (--jitless, or a flag "
    "such as --lite-mode that implies it). Turning it off enables V8's JIT compiler and WebAssembly, a larger attack "
    "surface (the OS sandbox and the other limits still apply, but max_buffer_bytes does not "
    "bound WebAssembly memory). Load only trusted modules."
)

_MAGIC = b"\x00asm\x01\x00\x00\x00"
_VALUE_TYPES = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64"}
# V8's own limits on a function type (`kV8MaxWasmFunctionParams` / `...Returns`).
_MAX_PARAMS = 1000
_MAX_RESULTS = 1000
_INT_RANGES = {"i32": (-(2**31), 2**32), "i64": (-(2**63), 2**64)}


def read_module(module: Any, max_bytes: int) -> bytes:
    """The module's bytes, from memory or from a path the host reads, refused past `max_bytes`."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an int")
    if not 1 <= max_bytes <= MAX_WASM_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_WASM_BYTES}")
    if isinstance(module, (bytes, bytearray, memoryview)):
        view = memoryview(module).cast("B")
        if view.nbytes > max_bytes:
            raise ValueError(
                f"the module is {view.nbytes} bytes, over max_bytes ({max_bytes})"
            )
        data = view.tobytes()
    elif isinstance(module, (str, os.PathLike)):
        with open(module, "rb") as f:  # noqa: PTH123 - str or PathLike alike
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"the module file is over max_bytes ({max_bytes})")
    else:
        raise TypeError(
            "load_wasm takes the module as bytes, bytearray, memoryview or a path, "
            f"not {type(module).__name__}"
        )
    if data[:8] != _MAGIC:
        raise ValueError("not a WebAssembly binary module (version 1)")
    return data


class _Reader:
    """Bounded reads over one byte range; every overrun is a ValueError."""

    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data, self.pos, self.end = data, pos, end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise ValueError("truncated WebAssembly module")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u32(self) -> int:
        result = 0
        for shift in range(0, 35, 7):
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                if result >= 2**32:
                    raise ValueError("malformed WebAssembly module (integer too large)")
                return result
        raise ValueError("malformed WebAssembly module (integer too long)")

    def take(self, length: int) -> bytes:
        if length > self.end - self.pos:
            raise ValueError("truncated WebAssembly module")
        chunk = self.data[self.pos : self.pos + length]
        self.pos += length
        return chunk

    def count(self, bound: int) -> int:
        # A vector cannot hold more entries than it has bytes left, so a forged count is refused
        # before anything loops over it.
        n = self.u32()
        if n > bound or n > self.end - self.pos:
            raise ValueError("malformed WebAssembly module (vector too long)")
        return n


def parse_signatures(data: bytes) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Exported function name -> (parameter types, result types), read from the type, import,
    function and export sections. Refuses modules with imports. V8 validates everything else."""
    top = _Reader(data, 8, len(data))
    types: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    functions: list[int] = []
    exports: dict[str, int] = {}
    seen: set[int] = set()
    while top.pos < top.end:
        section_id = top.byte()
        size = top.u32()
        body = _Reader(data, top.pos, top.pos + size)
        top.take(size)
        if section_id == 0:
            continue
        if section_id in seen:
            raise ValueError("malformed WebAssembly module (duplicate section)")
        seen.add(section_id)
        if section_id == 1:
            for _ in range(body.count(2**20)):
                if body.byte() != 0x60:
                    raise ValueError(
                        "unsupported WebAssembly type section (only function types)"
                    )
                params = tuple(
                    _value_type(body.byte()) for _ in range(body.count(_MAX_PARAMS))
                )
                results = tuple(
                    _value_type(body.byte()) for _ in range(body.count(_MAX_RESULTS))
                )
                types.append((params, results))
        elif section_id == 2:
            if body.u32() != 0:
                raise ValueError(
                    "load_wasm: modules with imports are not supported (nothing on the host "
                    "side is wired into WebAssembly)"
                )
        elif section_id == 3:
            functions = [body.u32() for _ in range(body.count(2**20))]
        elif section_id == 7:
            for _ in range(body.count(2**20)):
                name_bytes = body.take(body.u32())
                try:
                    name = name_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError(
                        "malformed WebAssembly module (export name)"
                    ) from None
                kind = body.byte()
                index = body.u32()
                if name in exports:
                    raise ValueError("malformed WebAssembly module (duplicate export)")
                if kind == 0:
                    exports[name] = index
        if section_id in (1, 2, 3, 7) and body.pos != body.end:
            raise ValueError("malformed WebAssembly module (section size)")
    signatures = {}
    for name, index in exports.items():
        if index >= len(functions) or functions[index] >= len(types):
            raise ValueError("malformed WebAssembly module (export index)")
        signatures[name] = types[functions[index]]
    return signatures


def _value_type(code: int) -> str:
    # v128 and reference types are valid WebAssembly, but cannot cross from Python: kept by name so
    # that a call naming them is refused, not mistyped.
    return _VALUE_TYPES.get(
        code, {0x7B: "v128", 0x70: "funcref", 0x6F: "externref"}.get(code, "?")
    )


def prepare_args(
    name: str, signature: tuple[tuple[str, ...], tuple[str, ...]], args: tuple[Any, ...]
) -> tuple[list[Any], list[bool]]:
    """Check `args` against the parameter types; returns them with the i64 positions marked."""
    params = signature[0]
    if len(args) != len(params):
        raise TypeError(f"{name}() takes {len(params)} arguments ({len(args)} given)")
    out: list[Any] = []
    wide: list[bool] = []
    for position, (kind, value) in enumerate(zip(params, args)):
        if isinstance(value, bool):
            raise TypeError(
                f"{name}() argument {position}: a bool is not a WebAssembly {kind}"
            )
        if kind in _INT_RANGES:
            if not isinstance(value, int):
                raise TypeError(
                    f"{name}() argument {position}: a {kind} takes an int, not "
                    f"{type(value).__name__}"
                )
            low, high = _INT_RANGES[kind]
            if not low <= value < high:
                raise ValueError(
                    f"{name}() argument {position}: {value} does not fit an {kind}"
                )
            # An i64 crosses as a decimal string, which the bridge turns into a BigInt: the
            # ordinary conversion would make an int past 2**53 a (lossy) double.
            out.append(str(int(value)) if kind == "i64" else int(value))
        elif kind in ("f32", "f64"):
            if not isinstance(value, (int, float)):
                raise TypeError(
                    f"{name}() argument {position}: an {kind} takes a float or int, not "
                    f"{type(value).__name__}"
                )
            try:
                out.append(float(value))
            except OverflowError:
                raise ValueError(
                    f"{name}() argument {position}: {value} does not fit an {kind}"
                ) from None
        else:
            raise TypeError(
                f"{name}() parameter {position} is a {kind}, which Python cannot pass"
            )
        wide.append(kind == "i64")
    return out, wide


def _typed(kind: str, value: Any) -> Any:
    # The result conversion makes an integral double a Python int (and -0.0 a plain 0); a float
    # result type gives a float back. The sign of a negative zero is not recoverable here.
    if (
        kind in ("f32", "f64")
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        return float(value)
    return value


def _result(results: tuple[str, ...], value: Any) -> Any:
    from . import undefined

    if value is undefined:
        return None
    if isinstance(value, list):
        return tuple(_typed(k, v) for k, v in zip(results, value))
    return _typed(results[0], value) if len(results) == 1 else value


def flags_disable_wasm(flags: Sequence[str]) -> bool:
    """Whether these V8 flags leave the engine without WebAssembly (`--jitless` or `--lite-mode`,
    which implies it; last mention winning): the isolated runtimes then refuse `load_wasm` in the
    parent. Only an early, clearer refusal: the worker holds its own reference to the loader,
    taken before any guest code, and refuses whenever it has none, whatever the flags say."""
    from ._isolated import _bool_flag_setting

    return bool(
        _bool_flag_setting(flags, "jitless") or _bool_flag_setting(flags, "lite-mode")
    )


class _Base:
    __slots__ = ("_signatures", "_closed", "__weakref__")

    def __init__(
        self, signatures: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]
    ) -> None:
        self._signatures = signatures
        self._closed = False

    @property
    def signatures(self) -> Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        """Exported function name -> (parameter types, result types), e.g.
        ``{"add": (("i32", "i32"), ("i32",))}``."""
        return MappingProxyType(self._signatures)

    def _prepare(
        self, name: str, args: tuple[Any, ...]
    ) -> tuple[list[Any], list[bool]]:
        if self._closed:
            raise RuntimeError("this WebAssembly module was unloaded")
        if not isinstance(name, str) or name not in self._signatures:
            raise KeyError(name)
        return prepare_args(name, self._signatures[name], args)


class WasmModule(_Base):
    """A trusted WebAssembly module loaded with `load_wasm` (`Runtime`, `IsolatedRuntime`).

    `exports` maps each exported function's name to a callable; `call(name, *args)` does the same.
    Arguments are ints (for i32 and i64; an i64 takes any int in range) and floats (for f32 and
    f64); a result is an int, a float, a tuple (several results) or None (none). `unload()` (or
    leaving a ``with`` block) drops the instance.
    """

    __slots__ = ("_call", "_unload")

    def __init__(
        self,
        signatures: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
        call: Callable[[str, list[Any], list[bool], Any], Any],
        unload: Callable[[], None],
    ) -> None:
        super().__init__(signatures)
        self._call = call
        self._unload = unload

    @property
    def exports(self) -> Mapping[str, Callable[..., Any]]:
        """Exported function name -> a callable taking the arguments (and ``timeout=``)."""
        return MappingProxyType({name: _bound(self, name) for name in self._signatures})

    def call(
        self, name: str, *args: Any, timeout: float | timedelta | None = None
    ) -> Any:
        """Call the exported function `name`. `timeout` (seconds) defaults to the runtime's."""
        values, wide = self._prepare(name, args)
        return _result(
            self._signatures[name][1], self._call(name, values, wide, timeout)
        )

    def unload(self) -> None:
        """Drop the instance. Idempotent; later calls raise RuntimeError."""
        if not self._closed:
            self._closed = True
            self._unload()

    def __enter__(self) -> WasmModule:
        return self

    def __exit__(self, *exc: object) -> None:
        self.unload()

    def __repr__(self) -> str:
        state = "unloaded" if self._closed else ", ".join(self._signatures)
        return f"<WasmModule {state}>"


class AsyncWasmModule(_Base):
    """`WasmModule` for `AsyncIsolatedRuntime`: `call`, the `exports` and `unload` are
    coroutines; ``async with`` unloads."""

    __slots__ = ("_call", "_unload")

    def __init__(
        self,
        signatures: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
        call: Callable[[str, list[Any], list[bool], Any], Any],
        unload: Callable[[], Any],
    ) -> None:
        super().__init__(signatures)
        self._call = call
        self._unload = unload

    @property
    def exports(self) -> Mapping[str, Callable[..., Any]]:
        """Exported function name -> a coroutine function taking the arguments."""
        return MappingProxyType({name: _bound(self, name) for name in self._signatures})

    async def call(
        self, name: str, *args: Any, timeout: float | timedelta | None = None
    ) -> Any:
        values, wide = self._prepare(name, args)
        return _result(
            self._signatures[name][1], await self._call(name, values, wide, timeout)
        )

    async def unload(self) -> None:
        if not self._closed:
            self._closed = True
            await self._unload()

    async def __aenter__(self) -> AsyncWasmModule:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.unload()

    def __repr__(self) -> str:
        state = "unloaded" if self._closed else ", ".join(self._signatures)
        return f"<AsyncWasmModule {state}>"


def _bound(owner: WasmModule | AsyncWasmModule, name: str) -> Callable[..., Any]:
    def call(*args: Any, timeout: float | timedelta | None = None) -> Any:
        return owner.call(name, *args, timeout=timeout)

    call.__name__ = call.__qualname__ = name
    return call


def drain(dropped: list[int]) -> list[int]:
    """Take the ids queued by `track_drop`. One `pop` at a time, each atomic: a finalizer may
    append meanwhile, and two threads draining at once each get their own ids, none twice and
    none lost."""
    taken: list[int] = []
    while dropped:
        try:
            taken.append(dropped.pop(0))
        except IndexError:  # another thread took the last one
            break
    return taken


@contextmanager
def draining(dropped: list[int]) -> Iterator[list[int]]:
    """`drain` for one request: if the request raises, the ids go back on the queue and ride
    along with the next wasm command (it may not have been sent; forgetting twice is harmless),
    so a failed request never leaves instances behind in the worker."""
    taken = drain(dropped)
    try:
        yield taken
    except BaseException:
        dropped.extend(taken)
        raise


def track_drop(module: Any, dropped: list[int], wid: int) -> Any:
    """Queue `wid` for the worker to forget once `module` is garbage collected. A finalizer must
    not talk to the worker itself (it can run on any thread, inside another command), so the id
    rides along with the next wasm command. After `unload()` the id is already gone there, and
    forgetting it again is harmless."""
    weakref.finalize(module, dropped.append, wid)
    return module


def bridge_loader(runtime: Any) -> Any:
    """The bridge's loader in an in-process `Runtime` (a function handle), or None when V8 has no
    WebAssembly here.

    Where V8 has WebAssembly the bridge fixed this global (non-writable, non-configurable) before
    any guest code ran, so a guest cannot replace it. Where it has none (`--jitless`,
    `--lite-mode`, ...) a guest could plant a global of that name. The isolation worker therefore
    takes this reference once, right after its runtime is created and before any guest code, and
    uses only that. `Runtime.load_wasm` looks it up per call: an in-process `Runtime` is not a
    boundary for hostile code, and a process whose V8 has no WebAssembly is one the host set up."""
    return runtime.eval(
        "typeof __pydeno_wasm_load === 'function' ? __pydeno_wasm_load : null"
    )


def runtime_load_wasm(
    self: Any,
    module: Any,
    /,
    *,
    max_bytes: int = MAX_WASM_BYTES,
    timeout: float | timedelta | None = None,
) -> WasmModule:
    """Load a **trusted** WebAssembly module and return a `WasmModule`.

    `module` is the binary as bytes (or bytearray / memoryview), or a path the host reads; the
    guest never gets file access. At most `max_bytes` (default and ceiling 8 MiB). The module must
    have no imports. Its exported functions take and return numbers; see `WasmModule`.

    For trusted code only: the module runs in this process, and its linear memory is not bounded
    by `max_buffer_bytes` (nor by anything else in an in-process `Runtime`). `timeout` (seconds)
    bounds the instantiation, which runs the module's start function; it defaults to the runtime's.

    Raises:
        RuntimeError: V8 runs with ``--jitless`` here, so there is no WebAssembly.
        ValueError: Too large, not a WebAssembly module, malformed, or it has imports.
        JavaScriptError: V8 refused to compile or instantiate it.
    """
    data = read_module(module, max_bytes)
    signatures = parse_signatures(data)
    loader = bridge_loader(self)
    if loader is None:
        raise RuntimeError(JITLESS_MESSAGE)
    handle = loader(data, timeout=timeout)

    def call(name: str, values: list[Any], wide: list[bool], call_timeout: Any) -> Any:
        return handle(name, values, wide, timeout=call_timeout)

    def unload() -> None:
        handle(None, [], [])

    return WasmModule(signatures, call, unload)
