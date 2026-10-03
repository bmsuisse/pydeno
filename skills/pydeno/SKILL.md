---
name: pydeno
description: Embed and run JavaScript from Python with pydeno, a V8 sandbox built on deno_core and Rust. Use when you need to run JavaScript/TypeScript from Python, evaluate untrusted or LLM-generated JS in a V8 sandbox, call JS functions from Python or expose Python functions and tools to JS, load ES modules or npm packages (npm: imports via a module loader, CDN bundles), enforce timeouts, heap and serialization limits on guest code, or kill runaway JS. Triggers - "pydeno", "run JavaScript from Python", "execute JS in Python", "V8 sandbox", "embedded JavaScript runtime", "deno_core Python", "npm: imports", "sandboxed code execution", "JS tool calling", "RuntimeTimeout", "RuntimeTerminated".
---

# pydeno

pydeno runs JavaScript inside a V8 isolate owned by a Python object. Each
`Runtime` has its own OS thread and isolate; values cross the boundary as
native Python types. It is **deno_core, not the Deno CLI**: there is no
`fetch`, no filesystem, no network, no `setTimeout`, no `Deno` global and no
permission flags. The guest can only reach what you hand it (bound functions,
modules you load). That is what makes it a sandbox.

## Install

```bash
pip install pydeno        # or: uv add pydeno
```

Python >= 3.10. Prebuilt wheels: Linux x86_64 (manylinux_2_28), macOS arm64,
Windows x64. Elsewhere pip builds from the sdist and needs a Rust toolchain.

## Core API in one screen

| Need | Use |
| --- | --- |
| Run a script, get a value | `rt.eval(code)` |
| Await promises / top-level async | `await rt.eval_async(code, timeout=...)` |
| Call a JS function | `fn = rt.eval("(a) => ...")`, then `fn(x)` or `await fn.call_async(x, timeout=...)` |
| Give JS a Python function | `rt.bind_function(name, f)`, `@rt.bind`, `rt.bind_object(name, {...})`, or `ToolBridge` |
| ES modules | `rt.add_static_module`, `rt.set_module_resolver`, `rt.set_module_loader`, `rt.eval_module[_async]` |
| Limits | `RuntimeConfig(timeout=, max_heap_size=, max_serialization_bytes=, max_serialization_depth=)` |
| Stop runaway JS | a timeout, `rt.terminate()`, or `rt.termination_handle().terminate()` from another thread |
| Capture `console.*` | `RuntimeConfig(on_console=lambda level, args: ...)` |

Full signatures: [references/api.md](references/api.md). Type mapping:
[references/types.md](references/types.md).

### Errors

| Exception | Base | Meaning | Runtime usable afterwards? |
| --- | --- | --- | --- |
| `JavaScriptError` | `Exception` | JS threw; has `.name`, `.message`, `.stack`, `.frames` | yes |
| `RuntimeTimeout` | `RuntimeError` | a `timeout` expired | yes |
| `RuntimeTerminated` | `RuntimeError` | `terminate()`, a termination handle, or heap limit hit | **no**, make a new one |
| `RuntimeForceKilled` | `RuntimeTerminated` | termination not acknowledged within `force_kill_grace` | no |
| `RuntimeError` | | anything else, e.g. a value over `max_serialization_bytes` | yes |

`JavaScriptError` is *not* a `RuntimeError`. Catch it separately.

## Recipes

Every recipe below was run against pydeno 0.4.1.

### 1. Evaluate and keep state

```python
from pydeno import Runtime

with Runtime() as rt:
    print(rt.eval("[1, 2, 3].map(x => x * 2)"))  # [2, 4, 6]
    rt.eval("globalThis.total = 40")  # globals persist across evals
    print(rt.eval("total + 2"))  # 42
```

### 2. Timeouts and JS errors

```python
from pydeno import JavaScriptError, Runtime, RuntimeConfig, RuntimeTimeout

with Runtime(RuntimeConfig(timeout=0.5)) as rt:
    try:
        rt.eval("while (true) {}")
    except RuntimeTimeout as exc:
        print("timed out:", exc)
    print(rt.eval("1 + 1"))  # 2 -- a timeout is recoverable
    try:
        rt.eval("null.x")
    except JavaScriptError as exc:
        print(exc.name, exc.message)  # TypeError Cannot read properties of null ...
```

`rt.eval()` has no `timeout=` argument: bound it with `RuntimeConfig(timeout=...)`.
The async entry points and `JsFunction` calls take a per-call `timeout=`
(seconds, or a `timedelta`), which overrides the runtime-wide one.

### 3. Call JS functions from Python

```python
import asyncio

from pydeno import Runtime, RuntimeTimeout


async def main():
    with Runtime() as rt:
        add = rt.eval("(a, b) => a + b")
        print(add(2, 3))  # 5 -- sync JS function, plain value
        times_ten = rt.eval("async (x) => x * 10")
        print(await times_ten.call_async(4))  # 40 -- call_async always awaits
        spin = rt.eval("async () => { await 0; while (true) {} }")
        try:
            await spin.call_async(timeout=0.3)
        except RuntimeTimeout:
            print("call timed out")


asyncio.run(main())
```

### 4. Expose Python tools to JS, with a call budget

```python
import asyncio

from pydeno import Runtime, ToolBridge, ToolError


def lookup_price(sku: str) -> float:
    prices = {"A-1": 9.5}
    if sku not in prices:
        raise ToolError(f"unknown sku {sku}")
    return prices[sku]


async def slow_double(x: int) -> int:
    await asyncio.sleep(0.01)
    return x * 2


async def main():
    with Runtime() as rt:
        ToolBridge({"lookup_price": lookup_price, "slow_double": slow_double}, max_calls=100).attach(rt)
        print(await rt.eval_async("""
            (async () => {
              let missing;
              try { tools.lookup_price('nope') } catch (e) { missing = e.name }
              return [tools.lookup_price('A-1'), await tools.slow_double(21), missing];
            })()
        """))  # [9.5, 42, 'ToolError']


asyncio.run(main())
```

A Python exception reaches JS as an `Error` whose `.name` is the exception's
class name. Async tools return a JS promise, so drive them from `eval_async`
or `call_async`.

### 5. ES modules

```python
from pydeno import Runtime

with Runtime() as rt:
    rt.add_static_module("mathlib", "export const square = (x) => x * x;")
    rt.add_static_module(
        "main", "import { square } from 'mathlib'; export const answer = square(7);"
    )
    print(rt.eval_module("main")["answer"])  # 49
```

### 6. `npm:` imports through your own loader

Nothing is importable by default, and pydeno does not resolve `npm:` or `jsr:`
itself. You map specifiers to URLs, and you fetch the source, which keeps the
allow-list in your hands:

```python
import asyncio
import urllib.request

from pydeno import Runtime

CDN = "https://cdn.jsdelivr.net/npm/"


def resolve(specifier: str, referrer: str) -> str | None:
    if specifier.startswith("npm:"):  # npm:ms@2.1.3 -> ESM build on jsDelivr
        return CDN + specifier[4:] + "/+esm"
    if specifier.startswith("/npm/"):  # jsDelivr's own absolute imports
        return "https://cdn.jsdelivr.net" + specifier
    return None


def load(url: str) -> str:
    if not url.startswith(CDN):  # allow-list: the guest cannot fetch anything else
        raise ValueError(f"blocked module: {url}")
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode()


async def main():
    with Runtime() as rt:
        rt.set_module_resolver(resolve)
        rt.set_module_loader(load)
        rt.add_static_module("app", "import ms from 'npm:ms@2.1.3'; export const t = ms('2h');")
        ns = await rt.eval_module_async("static:app", timeout=30)
        print(ns["t"])  # 7200000


asyncio.run(main())
```

In production, vendor the bundle into your package and serve it from the
loader or `add_static_module` rather than fetching at runtime. Packages that
need Node or browser globals (`setTimeout`, `TextEncoder`, `Buffer`, `fetch`)
need polyfills installed first, e.g. via `RuntimeConfig(bootstrap=...)`.

### 7. Kill runaway JS from another thread

```python
import threading

from pydeno import Runtime, RuntimeTerminated

rt = Runtime()
handle = rt.termination_handle()  # take it on the owning thread
threading.Timer(0.5, handle.terminate).start()  # safe from any thread
try:
    rt.eval("while (true) {}")
except RuntimeTerminated:
    print("stopped; this runtime is now dead")
rt.close()
```

Prefer `RuntimeConfig(timeout=...)`: it raises `RuntimeTimeout` and leaves the
runtime usable. Termination is final.

### 8. Resource limits and console capture

```python
from pydeno import Runtime, RuntimeConfig, RuntimeTerminated

config = RuntimeConfig(
    timeout=2.0,
    max_heap_size=64 * 1024 * 1024,  # bytes; exceeding it kills the runtime
    max_serialization_bytes=1_000_000,  # cap on any value crossing JS -> Python
    on_console=lambda level, args: print(f"[js {level}]", *args),
)
with Runtime(config) as rt:
    rt.eval("console.log('hello from JS', 42)")
    try:
        rt.eval("'x'.repeat(2_000_000)")
    except RuntimeError as exc:  # plain RuntimeError, runtime still usable
        print("too big:", str(exc)[:60])
    try:
        rt.eval("const a = []; while (true) a.push(new Array(1e5).fill(1))")
    except RuntimeTerminated:
        print("heap limit hit; build a new Runtime")
```

## Sandboxing model

- The guest global scope is ECMAScript built-ins plus `console`,
  `queueMicrotask`, `ReadableStream`, `WebAssembly` and pydeno's own bridge
  functions. `Deno`, `__bootstrap` and `__infra` are deleted.
- Every host capability is explicit: a bound function, a `ToolBridge`, or a
  module your loader agreed to return. Bound functions are called through
  unguessable capability tokens; `rt.revoke_op(token)` or
  `bridge.detach(rt)` takes one back.
- Always set `timeout` and `max_heap_size` for untrusted code. The defaults
  are no timeout and V8's own heap limit.
- `SnapshotBuilder` is **not** sandboxed: its scripts run with the raw op
  table and no timeout. Only feed it your own code.
- There is no TypeScript compiler. Strip types before evaluating (for example
  with `esbuild` or `sucrase` on the host) or have the model emit plain JS.

## Threading and concurrency

- A `Runtime` is bound to the thread that created it. Calling any method
  from another thread raises `PanicException`. The one exception is
  `TerminationHandle.terminate()`.
- One runtime runs one thing at a time. You can `asyncio.gather` several
  `eval_async` or `call_async` calls on one runtime; they interleave at `await`
  points, not in parallel. For parallelism, use one runtime per thread or task.
  Blocking calls release the GIL, so runtimes on different threads really do
  run concurrently.
- **Cross-talk:** a fired deadline terminates *whatever the isolate is running
  at that moment*. If an async job's deadline expires while an unrelated sync
  call is running on the same runtime, that call is the one stopped, and it
  reports a bare `execution terminated` error instead of a timeout. If a
  termination error must refer to the call that raised it, give each
  concurrent job its own runtime.

## Common pitfalls

- **`rt.eval()` does not await.** `rt.eval("Promise.resolve(5)")` returns `{}`
  (the promise object, serialised). Use `await rt.eval_async(...)`.
- **`fn(...)` may or may not return an awaitable.** A JS `async` function that
  finishes during the call's microtask checkpoint returns a plain value, and
  `await` on it raises `TypeError`. Use `await fn.call_async(...)` for anything
  async.
- **Top-level `let`/`const` persist.** Evaluating `let y = 1` twice on one
  runtime raises `SyntaxError: Identifier 'y' has already been declared`. Wrap
  snippets in `(() => { ... })()` or use modules.
- **`undefined` is not `None`.** JS `undefined` comes back as
  `pydeno.undefined` (falsy, a `JsUndefined`); `null` becomes `None`.
- **A termination is permanent, a timeout is not.** After `RuntimeTerminated`,
  including from a heap-limit hit, every call raises again. Build a new runtime.
- **Async methods start work immediately.** Call `eval_async`,
  `eval_module_async` and `call_async` inside a running event loop; outside
  one they raise `RuntimeError: no running event loop`.
- **Async bound functions need an async entry point.** From a sync `rt.eval`
  you get back an unresolved promise (`{}`) and the Python coroutine never
  runs.
- **Deep values:** nesting past `max_serialization_depth` (default 100) raises
  `RuntimeError`. Flatten or JSON-encode deep structures yourself.
- **Close runtimes.** Use `with Runtime() as rt:` or call `rt.close()`, since
  each runtime owns a thread and an isolate.
