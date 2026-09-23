# Value conversion between Python and JavaScript

Checked against pydeno 0.4.1. Conversion is by value (a copy), not a live
proxy. The one exception is a JS function, which comes back as a callable
`JsFunction` handle.

## JavaScript to Python (return values, arguments to bound functions)

| JavaScript | Python | Notes |
| --- | --- | --- |
| `null` | `None` | |
| `undefined`, array holes | `pydeno.undefined` | a falsy `JsUndefined` singleton; test with `is pydeno.undefined` |
| `boolean` | `bool` | |
| integral `number` (`4`, `4.0`) | `int` | a whole-valued float comes back as `int` |
| other `number`, `NaN`, `±Infinity` | `float` | |
| `BigInt` | `int` | |
| `string` | `str` | |
| `Array` | `list` | recursive |
| plain object, class instance | `dict` | own enumerable string keys only; methods and prototype are dropped |
| `Set` | `set` | |
| `Date` | `datetime` (UTC, tz-aware) | |
| `Uint8Array`, `ArrayBuffer` | `bytes` | other typed arrays become a `dict` of index keys: convert with `Array.from` first |
| function | `JsFunction` | call it, or `await fn.call_async(...)`; `await fn.close()` releases it |
| `Promise` | `{}` from `eval`, the settled value from `eval_async` | always use `eval_async` for promises |
| `ReadableStream` | `JsStream` | an async iterator in Python |
| `Map`, `Error`, `RegExp` | `{}` | lossy, with no error raised: convert in JS first (`Object.fromEntries(map)`, `{name, message}`, `String(re)`) |
| `Symbol` | raises `RuntimeError` | "Cannot serialize V8 symbol" |

## Python to JavaScript (arguments to `JsFunction`, values from bound functions, `bind_object`)

| Python | JavaScript | Notes |
| --- | --- | --- |
| `None` | `null` | |
| `pydeno.undefined` | `undefined` | |
| `bool` | `boolean` | |
| `int` within ±2^53 | `number` | |
| larger `int` | `BigInt` | |
| `float` | `number` | |
| `str` | `string` | |
| `list` | `Array` | |
| `dict` with `str` keys | plain object | a non-`str` key raises `TypeError` |
| `set`, `frozenset` | `Set` | |
| `bytes`, `bytearray` | `Uint8Array` | |
| `datetime` | `Date` | a naive datetime is treated as UTC |
| `tuple`, arbitrary objects | raises `RuntimeError` | "Unsupported Python type": convert to a `list` or `dict` first |
| async iterable | `ReadableStream` | wrap with `rt.stream_from_async_iterable(it)` |

## Limits on every crossing

- `RuntimeConfig(max_serialization_depth=...)`, default 100: deeper nesting
  raises `RuntimeError`.
- `RuntimeConfig(max_serialization_bytes=...)`: an oversized value raises
  `RuntimeError`, and the runtime stays usable.
- For large tabular data, send Arrow IPC or JSON as `bytes`/`str` and parse
  it on the other side. That is far faster than converting a big list of
  dicts field by field.
