# WebAssembly

[WebAssembly][webassembly] (Wasm) runs compiled code (Rust, C, Zig...) inside the V8 isolate.
`load_wasm()` loads a **trusted** module from Python and lets you call its exported functions,
next to whatever JavaScript the runtime runs.

## `load_wasm()`: a trusted module, loaded by the host

```python
from pydeno import Runtime

with Runtime() as rt:
    wasm = rt.load_wasm("add.wasm")        # or the bytes themselves
    wasm.call("add", 2, 3)                 # 5
    wasm.exports["add"](40, 2)             # 42
    wasm.signatures                        # {"add": (("i32", "i32"), ("i32",))}
    wasm.unload()                          # or: with rt.load_wasm(...) as wasm:
```

The same call works on `IsolatedRuntime` and `AsyncIsolatedRuntime`, which need `jitless=False`
(see below):

```python
from pydeno import AsyncIsolatedRuntime, IsolatedRuntime

with IsolatedRuntime(jitless=False) as rt:
    rt.load_wasm(module_bytes).call("add", 1, 2)

async with await AsyncIsolatedRuntime.create(jitless=False) as rt:
    wasm = await rt.load_wasm(module_bytes)
    await wasm.call("add", 1, 2)           # an AsyncWasmModule: call/unload are coroutines
```

What it does, and refuses:

- **The host reads the bytes.** `module` is `bytes` / `bytearray` / `memoryview`, or a path that
  the Python process reads. The guest, and the isolated worker, never get file access.
- **Size.** At most `max_bytes` (default, and ceiling, 8 MiB). Checked before anything is parsed;
  a file is read only up to the limit.
- **Validation.** The host parses the module's type, import, function and export sections (with
  every length bounded), then V8 compiles it. Invalid or hostile bytes raise `ValueError` (from the
  host's parser) or `JavaScriptError` (V8's `CompileError`), and the runtime keeps working.
- **No imports.** A module that imports anything is refused: nothing on the host side is wired into
  WebAssembly.
- **Plain numbers across.** Each argument is checked against the function's signature: an `int` for
  `i32` (from `-2**31` to `2**32 - 1`, wrapping as WebAssembly does) and `i64` (from `-2**63` to
  `2**64 - 1`, exact), an `int` or `float` for `f32` / `f64`. A `bool`, a string or a wrong count is a
  `TypeError`, a number out of the type's range (an integer, or an `int` past the float range for
  `f32` / `f64`) a `ValueError`. Results are an `int` (`i32`, `i64`), a `float` (`f32`, `f64`, also
  when integral), a `tuple` (several results) or `None` (none), converted with the runtime's usual
  result limits; a negative-zero float result comes back as `0.0`. `v128` and reference types
  cannot be passed.
- **Timeouts.** A call runs under the runtime's `timeout` (or `call(..., timeout=)`); a module that
  loops forever is terminated like a script. A trap (`unreachable`, out-of-bounds memory) raises
  `JavaScriptError`.
- **A controlled bridge.** The module is compiled and instantiated with WebAssembly intrinsics the
  runtime captured before any guest code ran, and the instance is held only by the host: a guest
  that replaced `WebAssembly.Module` or `WebAssembly.Instance` cannot see the bytes or swap the
  instance, and the guest has no reference to it. The isolated worker takes its reference to the
  loader once, before any guest code, so where V8 has no WebAssembly a loader the guest defined
  itself is never called.
- **Unloading.** `unload()` (or leaving the `with` block) drops the instance. A module object that
  is garbage collected without it is forgotten too: the in-process handle is released, and the
  isolated worker drops the instance with the next `load_wasm` or call on that runtime.

### Byte buffers: `call_bytes()`

A module's memory is its own, so passing bytes needs an agreed convention. `call_bytes` defines the
smallest one. The module exports:

- `memory`, its linear memory;
- `alloc(len: i32) -> i32` and `dealloc(ptr: i32, len: i32)`, its own allocator;
- a function `(in_ptr: i32, in_len: i32, out_ptr: i32, out_cap: i32) -> i32` that reads `in_len`
  bytes at `in_ptr`, writes at most `out_cap` bytes at `out_ptr` and returns how many it wrote (a
  negative number is an error).

```python
wasm = rt.load_wasm("codec.wasm")
packed = wasm.call_bytes("compress", data, max_result_bytes=2 * 1024 * 1024)   # -> bytes
# AsyncWasmModule: packed = await wasm.call_bytes("compress", data)
```

What the host guarantees:

- **Copies, no sharing.** The input is copied into a block from the module's `alloc`; the result is
  copied out into fresh `bytes`. Python never holds a view of the module's memory, and the module
  never sees a Python buffer, so a later change on either side is invisible to the other.
- **Bounded.** `max_input_bytes` and `max_result_bytes` default to 1 MiB and cannot exceed 4 MiB
  (`ValueError` otherwise). The output block is exactly `max_result_bytes` long, so the module
  cannot produce more; a returned length above it is refused, never truncated.
- **Checked pointers.** A block `alloc` returns that lies outside the live memory is refused.
  Both blocks are `dealloc`ed after every call, a failed one included.
- **Same limits as any call.** The runtime's `timeout`, `timeout=`,
  traps and `max_memory` apply. A module's memory still grows past `max_buffer_bytes`
  (see below), and a module that lacks the convention gets a `TypeError` (or a `JavaScriptError`
  if it has no `memory` export).

The result is bounded, but the module is still trusted: its own `alloc` runs inside the call, and a
module that leaks in `dealloc` or loops is bounded only by memory limits and the timeout.

### The cost: `jitless=False`, and memory

Only load modules you trust, as you would a native library.

- **WebAssembly needs V8's JIT.** The isolated runtimes default to `jitless=True`, which has no
  WebAssembly, and there `load_wasm` raises `RuntimeError` before anything reaches the worker (also
  for `v8_flags` that imply jitless, such as `--lite-mode`).
  `jitless=False` turns on V8's JIT compiler and WebAssembly for that runtime, and so for its guest
  code too: a larger attack surface (most V8 exploits are in the JIT). The OS sandbox and the other
  limits still apply. See [Isolation](isolation.md#-jitless).
- **Linear memory is outside `max_buffer_bytes`.** A module's memory (`memory.grow`) is not an
  `ArrayBuffer` the capped allocator sees. In `IsolatedRuntime` and `AsyncIsolatedRuntime` it counts
  toward `max_memory` (the worker's resident memory): past it the worker is killed (`WorkerCrashed`),
  not a catchable error. An in-process `Runtime` has no bound on it.

### Not on `Pydeno`, `AgentSandbox` or `SessionPool`

Those sessions are journaled and replayed on fresh single-use workers, and a module loaded by the
host is not part of the journal: a replayed or restored session would silently lack it. Use
`IsolatedRuntime(jitless=False)` or `AsyncIsolatedRuntime(jitless=False)` directly, or a runtime
from `SandboxPool(jitless=False)` / `AsyncSandboxPool(jitless=False)`: a pool checkout is a plain
isolated runtime, not journaled, so `checkout().load_wasm(...)` works.

### Is it faster?

Measured, not assumed: `benches_py/wasm_kernel_bench.py` runs one integer kernel
(`acc = ((acc + i * i) | 0) ^ (i >>> 3)`, ten million iterations) as JavaScript and as hand-assembled
WebAssembly, call overhead included (median of 15, Apple M-series):

| Runtime | JavaScript | WebAssembly |
|---|---:|---:|
| `Runtime` | 20.1 ms | 19.6 ms |
| `IsolatedRuntime(jitless=False)` | 19.9 ms | 19.9 ms |
| `IsolatedRuntime()` (jitless) | 381 ms | (no WebAssembly) |

For a kernel V8's JIT already compiles well, WebAssembly is no faster than the same JavaScript; the
large difference is the JIT itself. Load WebAssembly to run an existing compiled routine (a parser,
a codec, a numeric library), not to speed up JavaScript.

## Raw WebAssembly from JavaScript

With WebAssembly available, guest JavaScript can also use the [`WebAssembly`][webassembly-mdn] API
itself, for example on bytes the host bound:

```python
from pydeno import Runtime

with Runtime() as runtime:
    with open("add.wasm", "rb") as f:
        runtime.bind_object("wasm", {"bytes": f.read()})  # arrives as a Uint8Array

    result = runtime.eval("""
        const module = new WebAssembly.Module(wasm.bytes);
        const instance = new WebAssembly.Instance(module);
        instance.exports.add(10, 20);
    """)
    print(result)  # 30
```

`WebAssembly.compile` / `WebAssembly.instantiate` do the same asynchronously (with `eval_async`).
Here the guest holds the instance, and the size, import and argument checks above are yours to make.

## Limitations

- **No file system**: Wasm can't access files directly. Pass data via JavaScript bindings.
- **No threads**: V8's Wasm doesn't support threads (SharedArrayBuffer-based parallelism). Use multiple runtimes instead.
- **Numbers, or byte buffers by convention**: `call()` takes and returns numbers only; bytes need
  `call_bytes()` and the convention below. Strings, structs and shared memory are not supported.

## Next Steps

- Learn about [Inspector](inspector.md) to debug Wasm modules with DevTools
- Read [Isolation](isolation.md) for what `jitless=False` changes
- Check out [WebAssembly.org][webassembly] for tools and specifications

[webassembly]: https://webassembly.org/
[webassembly-mdn]: https://developer.mozilla.org/en-US/docs/WebAssembly
