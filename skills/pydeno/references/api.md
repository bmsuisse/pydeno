# pydeno API reference (0.4.1)

This is a condensed version of `python/pydeno/_pydeno.pyi` and
`python/pydeno/_tools.py`, which remain the source of truth. Timeouts
accept seconds (`float`/`int`) or a `datetime.timedelta`.

## `Runtime(config: RuntimeConfig | None = None)`

Owns a V8 isolate on a dedicated thread. It is a context manager, and every
method must be called from the thread that created it.

| Method | Returns | Notes |
| --- | --- | --- |
| `eval(code)` | value | synchronous; does **not** await promises; bounded only by `RuntimeConfig.timeout` |
| `await eval_async(code, *, timeout=None)` | value | awaits the resulting promise |
| `eval_module(specifier)` | `dict` namespace | synchronous |
| `await eval_module_async(specifier, *, timeout=None)` | `dict` namespace | supports top-level `await` |
| `add_static_module(name, source)` | `None` | importable as `name` or `static:name` |
| `set_module_resolver(fn(specifier, referrer) -> str \| None)` | `None` | map a specifier to a URL; `None` falls back to static resolution |
| `set_module_loader(fn(specifier) -> str \| Awaitable[str])` | `None` | return module source; raise to refuse |
| `bind_function(name, fn)` | `int` token | installs `globalThis[name]`; an async `fn` becomes a promise-returning JS function |
| `bind(fn=None, *, name=None)` | `fn` | decorator form of `bind_function` |
| `bind_object(name, mapping)` | `dict[str, int]` tokens | values are copied; callables become JS functions |
| `register_op(name, fn, *, mode="sync"\|"async")` | `int` token | low level: JS calls `__host_op_sync__(token, ...)` / `__host_op_async__(token, ...)` |
| `revoke_op(token)` | `bool` | the JS name stays, but calling it raises |
| `stream_from_async_iterable(aiter)` | `PyStreamSource` | pass it into JS, where it is a `ReadableStream` |
| `terminate()` | `None` | owning thread only; the runtime is dead afterwards |
| `termination_handle()` | `TerminationHandle` | `.terminate()` is safe from any thread; `.is_terminated()` |
| `get_stats()` | `RuntimeStats` | heap bytes, execution counters and times, active timers and streams |
| `inspector_endpoints()` | `InspectorEndpoints \| None` | DevTools URLs when `RuntimeConfig(inspector=...)` is set |
| `close()` / `is_closed()` | | `close()` is idempotent |

## `JsFunction`

Returned whenever JS hands back a function.

| Call | Behaviour |
| --- | --- |
| `fn(*args, timeout=None)` | Returns the value if the JS finishes during the call (including an `async` function whose promise settles in the microtask checkpoint); otherwise returns an awaitable. |
| `await fn.call_async(*args, timeout=None)` | Always a coroutine; work starts immediately; safe to `asyncio.create_task` / `gather`. |
| `await fn.close()` | Releases the handle. |

A method returned as `obj.method` loses its `this`: return
`obj.method.bind(obj)` or a closure instead.

## `RuntimeConfig(...)`

| Argument | Default | Meaning |
| --- | --- | --- |
| `timeout` | `None` | seconds; applies to every call without its own `timeout=`; raises `RuntimeTimeout` |
| `max_heap_size` | V8 default | bytes; exceeding it raises `RuntimeTerminated`, and the runtime is dead |
| `initial_heap_size` | V8 default | bytes |
| `max_serialization_depth` | 100 | nesting limit for values crossing the boundary |
| `max_serialization_bytes` | 10 MiB (10485760) | size limit for values crossing the boundary |
| `bootstrap` | `None` | JS source run at startup (polyfills, helpers) |
| `snapshot` | `None` | bytes from `SnapshotBuilder.build()`, for fast startup |
| `enable_console` | `False` | let `console.*` write to the process's stdout/stderr |
| `on_console` | `None` | `fn(level: str, args: list)`, synchronous; receives every `console.*` call |
| `force_kill_grace` | `None` | seconds to wait for a termination before abandoning the runtime thread (`RuntimeForceKilled`); `pydeno.SUGGESTED_FORCE_KILL_GRACE` is 0.1 |
| `inspector` | `None` | `InspectorConfig(host=..., port=..., wait_for_connection=..., break_on_next_statement=...)` |

## Exceptions

```
Exception
├── JavaScriptError        .name .message .stack .frames (list of JsFrame dicts)
└── RuntimeError
    ├── RuntimeTimeout     recoverable
    └── RuntimeTerminated  final: terminate(), a TerminationHandle, or the heap limit
        └── RuntimeForceKilled
```

## `ToolBridge(tools, *, max_calls=None, namespace="tools", on_exhausted="raise")`

- `tools`: `{js_name: callable}`, sync or async. Names must match
  `[A-Za-z_][A-Za-z0-9_]*` and must not be `__proto__`, `constructor` and
  similar.
- `max_calls`: total budget across all tools. Exceeding it raises
  `ToolBudgetError` in JS, or returns `null` with `on_exhausted="silent"`.
- `namespace=None` installs each tool as a bare global.
- `.attach(rt)`, `.detach(rt) -> int` (revokes everything), `.calls_made`,
  `.calls_remaining`, `.reset_budget()`, `.tool_names`.
- A tool's exception reaches JS as `Error` with `.name` set to the Python
  class name. `ToolError`, `ToolBudgetError` and `ToolNotFoundError` are
  provided as shared vocabulary.

## Module-level helpers

- `pydeno.eval(code)` and `await pydeno.eval_async(code, **kw)` use a default
  runtime per thread or asyncio task, with default configuration.
- `pydeno.get_default_runtime()` and `pydeno.close_default_runtime()`.
- `pydeno.undefined` is the JS `undefined` sentinel.

## `SnapshotBuilder(*, bootstrap=None, enable_console=False)`

`.execute_script(name, source)`, then `.build() -> bytes`, which you pass as
`RuntimeConfig(snapshot=...)`. Scripts run **unsandboxed**, with the raw op
table and no timeout, so only use trusted code.
