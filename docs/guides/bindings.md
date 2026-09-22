# Bindings

Bindings let your JavaScript code talk to Python. Think of them as bridges: you expose Python functions and data, and JavaScript can use them naturally.

## Why Use Bindings?

Sometimes you want JavaScript to do the heavy lifting (parsing, transforming data), but you need Python for specific tasks:

- Call a Python API or library
- Access Python data without copying everything
- Let JavaScript trigger Python side effects (logging, notifications, etc.)

Instead of passing data back and forth with `eval()`, you bind once and call many times.

## Binding Functions

Use [`bind_function()`][pydeno.Runtime.bind_function] to expose a Python function to JavaScript:

```python
from pydeno import Runtime

with Runtime() as runtime:
    # Define a Python function
    def greet(name):
        return f"Hello, {name}!"

    # Bind it to JavaScript
    runtime.bind_function("greet", greet)

    # Now JavaScript can call it
    result = runtime.eval("greet('World')")
    print(result)  # "Hello, World!"
```

That's it. JavaScript sees `greet` as a regular function.

### Multiple Arguments

Python functions can accept any number of arguments:

```python
with Runtime() as runtime:
    def add(a, b, c=0):
        return a + b + c

    runtime.bind_function("add", add)

    print(runtime.eval("add(1, 2)"))     # 3
    print(runtime.eval("add(1, 2, 3)"))  # 6
```

Arguments are automatically converted between Python and JavaScript types (numbers, strings, lists, dicts, etc.).

### Async Functions

Async Python functions work too. JavaScript receives a Promise:

```python
import asyncio

async def main():
    with Runtime() as runtime:
        async def fetch_data(url):
            await asyncio.sleep(0.1)  # Simulate async work
            return {"url": url, "status": 200}

        runtime.bind_function("fetchData", fetch_data)

        # JavaScript gets a Promise
        result = await runtime.eval_async("""
            fetchData('https://example.com')
        """)
        print(result)  # {'url': 'https://example.com', 'status': 200}

asyncio.run(main())
```

JavaScript doesn't need to know the function is async, it just awaits the Promise.

## Binding Objects

Use [`bind_object()`][pydeno.Runtime.bind_object] to pass Python data to JavaScript:

```python
with Runtime() as runtime:
    config = {
        "debug": True,
        "timeout": 30,
        "retries": 3
    }

    runtime.bind_object("config", config)

    # JavaScript can read it
    result = runtime.eval("config.debug && config.retries > 0")
    print(result)  # True
```

### What Can You Bind?

You can bind Python values that can be converted to JavaScript:

- **Primitives**: `int`, `float`, `str`, `bool`, `None`
- **Collections**: `list`, `dict`, `tuple`
- **Binary data**: `bytes`, `bytearray`, `memoryview` (becomes `Uint8Array`)

```python
with Runtime() as runtime:
    # Bind various types (must be wrapped in a dict)
    runtime.bind_object("numbers", {"items": [1, 2, 3, 4, 5]})
    runtime.bind_object("user", {"name": "Alice", "age": 30})
    runtime.bind_object("data", {"bytes": b'\x00\x01\x02\x03'})

    # Use them in JavaScript
    runtime.eval("numbers.items.reduce((a, b) => a + b)")  # 15
    runtime.eval("user.name.toUpperCase()")                # "ALICE"
    runtime.eval("data.bytes[0] + data.bytes[1]")          # 1
```

### Objects Are Snapshots

When you bind an object, it gets serialized and JavaScript receives a **copy** of the data at that moment:

```python
with Runtime() as runtime:
    counter = {"value": 0}
    runtime.bind_object("counter", counter)

    # JavaScript modifies its copy
    runtime.eval("counter.value = 10")

    # Python's original is unchanged
    print(counter)  # {'value': 0}
```

If you need shared state, bind a function that returns fresh data each time.

## Practical Examples

### Configuration and Feature Flags

```python
with Runtime() as runtime:
    runtime.bind_object("features", {
        "darkMode": True,
        "experimentalUI": False,
        "maxUploadSize": 10_000_000
    })

    result = runtime.eval("""
        if (features.darkMode) {
            "dark-theme.css"
        } else {
            "light-theme.css"
        }
    """)
    print(result)  # "dark-theme.css"
```

### Logging from JavaScript

```python
with Runtime() as runtime:
    def log(level, message):
        print(f"[{level.upper()}] {message}")

    runtime.bind_function("log", log)

    runtime.eval("""
        log('info', 'Starting process...');
        log('error', 'Something went wrong!');
    """)
    # Output:
    # [INFO] Starting process...
    # [ERROR] Something went wrong!
```

### Data Validation

```python
with Runtime() as runtime:
    def validate_email(email, *args):
        # Accept extra args that JS array methods pass (index, array)
        return "@" in email and "." in email

    runtime.bind_function("validateEmail", validate_email)

    result = runtime.eval("""
        const emails = ['user@example.com', 'invalid', 'test@domain.org'];
        emails.filter(validateEmail)
    """)
    print(result)  # ['user@example.com', 'test@domain.org']
```

### Processing with Python Libraries

```python
with Runtime() as runtime:
    def process_image(data):
        # Imagine using Pillow, OpenCV, etc.
        return f"Processed {len(data)} bytes"

    runtime.bind_function("processImage", process_image)

    # JavaScript sends binary data to Python
    runtime.bind_object("image", {"data": b'\x89PNG\r\n...'})
    result = runtime.eval("processImage(image.data)")
    print(result)  # "Processed 9 bytes"
```

## Decorator Style

For a cleaner syntax, use the `@runtime.bind()` decorator:

```python
import asyncio

async def main():
    with Runtime() as runtime:
        @runtime.bind()
        def calculate(x, y):
            return x * y + 10

        @runtime.bind()
        async def fetch_user(user_id):
            # Simulate async database call
            await asyncio.sleep(0.1)
            return {"id": user_id, "name": "Alice"}

        result = runtime.eval("calculate(5, 3)")
        print(result)  # 25

        user = await runtime.eval_async("fetch_user(123)")
        print(user)  # {'id': 123, 'name': 'Alice'}

asyncio.run(main())
```

The decorator automatically uses the function's name as the binding name in JavaScript. If you want a different name, pass the `name` parameter:

```python
with Runtime() as runtime:
    @runtime.bind(name="add")
    def my_addition_function(a, b):
        return a + b

    result = runtime.eval("add(2, 3)")  # 5
```

## Module-Level API

For quick scripts, use the module-level functions (they use a context-local runtime):

```python
import pydeno

# Bind to the default runtime
pydeno.bind_function("add", lambda a, b: a + b)
pydeno.bind_object("config", {"version": "1.0"})

# Use them immediately
print(pydeno.eval("add(2, 3)"))        # 5
print(pydeno.eval("config.version"))   # "1.0"
```

This is perfect for interactive sessions or simple scripts where you don't need explicit runtime management.

## Typed Tool Errors

When a bound Python function raises, JavaScript gets a real `Error` whose
`name` is the Python exception's class name and whose `message` is the
exception's message:

```python
from pydeno import Runtime


class RateLimited(Exception):
    pass


def get_weather(city):
    if city == "Atlantis":
        raise ValueError(f"unknown city: {city}")
    if city == "Springfield":
        raise RateLimited("try again in 30s")
    return f"72F and sunny in {city}"


with Runtime() as runtime:
    runtime.bind_function("getWeather", get_weather)

    print(runtime.eval("""
      const classify = (city) => {
        try { return 'ok: ' + getWeather(city) }
        catch (e) {
          if (e.name === 'ValueError')   return 'bad input';
          if (e.name === 'RateLimited')  return 'retry later';
          return 'unexpected: ' + e.name;
        }
      };
      [classify('Zurich'), classify('Atlantis'), classify('Springfield')].join(' | ')
    """))
    # ok: 72F and sunny in Zurich | bad input | retry later
```

This is the same `name`/`message` shape that a JavaScript exception reaching
Python carries on [`JavaScriptError`][pydeno.JavaScriptError], so the two
directions are symmetric. An *uncaught* tool exception therefore arrives in
Python with the class name preserved as well:

```python
with Runtime() as runtime:
    runtime.bind_function("getWeather", get_weather)
    try:
        runtime.eval("getWeather('Atlantis')")
    except Exception as exc:
        print(exc.name)     # "ValueError"
        print(exc.message)  # "unknown city: Atlantis"
```

Only the class name and message cross the boundary — never a Python
traceback, module path or local variable.

## Capturing `console` Output

By default `console.log` from sandboxed JavaScript goes nowhere. Pass
`on_console` to get it back in Python:

```python
from pydeno import Runtime, RuntimeConfig

lines = []

config = RuntimeConfig(on_console=lambda level, args: lines.append((level, args)))
with Runtime(config) as runtime:
    runtime.eval("console.log('processing', 3, {items: [1, 2]}); void 0;")

print(lines)  # [('log', ['processing', 3, {'items': [1, 2]}])]
```

The callback receives `(level, args)`, where `level` is the console method
name (`"log"`, `"info"`, `"warn"`, `"error"`, `"debug"`, `"trace"`) and
`args` is that call's argument list, converted with the same rules as any
other host callback — so you get the *values*, not a pre-rendered string.
This is what you want when showing a model what its own script printed.

### `on_console` and `enable_console` are independent

`enable_console` controls only whether the process's own stdout/stderr
receives console output. `on_console` controls whether *you* receive it. They
compose:

| `enable_console`  | `on_console` | Result                                  |
| ----------------- | ------------ | --------------------------------------- |
| `False` (default) | `None`       | `console.*` is a no-op; output discarded |
| `True`            | `None`       | Output goes to the process's stdout/stderr |
| `False`           | set          | Output goes **only** to your callback   |
| `True`            | set          | Callback fires, then the process's console |

Other semantics worth knowing:

- The callback must be **synchronous**. `console.log` is synchronous in
  JavaScript, so an async callback would never be awaited.
- Output from a `bootstrap` script is captured too — the hook is installed
  before bootstrap runs.
- A `console` call can never break the script that made it. If an argument
  list cannot be represented as a Python value (a circular object, or one
  over the serialization limits), the bridge retries with the arguments
  stringified, and drops the message entirely rather than throwing if even
  that fails. A callback that raises is likewise swallowed.
- Passing a JS function (or a `Symbol`) to a host tool is **refused** with a
  `TypeError` naming the argument path -- it does not arrive as `{}`. See
  [Capabilities and revocation](#capabilities-and-revocation).
- `null`/`undefined` both arrive as the [`undefined`][pydeno.undefined]
  sentinel, as everywhere else on the host-callback path.

## Capabilities and revocation

`bind_function` returns an **op capability token**, `bind_object` returns one
per callable key, and `register_op` returns the token for the op it created.
The token is an unguessable integer drawn from a CSPRNG, and it is the whole
authority to call that op: guest JS reaches a host handler only through a
token a completed bind step installed in its scope.

That matters for two reasons:

- **Do not hand a token to guest code you do not mean to grant the
  capability to.** The name is convenience; the token is authority.
- **A namespace is not a trust boundary by itself, but a token is.** Two
  `ToolBridge`es with different trust levels on one `Runtime` no longer
  collapse into one trust level, because neither can address the other's ops.

Revoke with [`revoke_op`][pydeno.Runtime.revoke_op], or
[`ToolBridge.detach`][pydeno.ToolBridge.detach] for a whole bridge:

```python
token = runtime.bind_function("dangerous", do_something)
runtime.eval("dangerous()")      # works
runtime.revoke_op(token)
runtime.eval("dangerous()")      # raises: the capability is gone
```

The global name stays on `globalThis` -- a guest may have captured the
function reference already anyway -- but the capability behind it is dropped,
so the call fails.

A guest-visible failure to reach an op is always the same, name-free
`Unknown host op`, whether the token is unknown, was never exposed, or was
revoked. That is deliberate: distinguishing them would let a guest enumerate
what a runtime has registered.

Passing a JS function to a host tool raises rather than silently arriving as
`{}`. Supporting it properly means a host-held reference with a documented
lifetime (ownership, release, behaviour after the supplying call returned),
which is a feature rather than a conversion detail. If a tool needs a
callback shape, have it return a value and let the guest apply its own
function to it.

## `ToolBridge`: many tools, with a budget

`bind_function` and `bind_object` are the primitives. If what you actually
want is "give this sandbox N callable tools safely", `ToolBridge` packages
the three things you would otherwise write yourself: a total call budget,
fail-closed name checking, and the typed errors described above.

```python
from pydeno import Runtime, ToolBridge


def get_weather(city: str) -> str:
    return f"72F and sunny in {city}"


async def send_email(to: str, subject: str, body: str) -> bool:
    return True


bridge = ToolBridge(
    {"get_weather": get_weather, "send_email": send_email},
    max_calls=50,          # total across all tools; None = unlimited
    namespace="tools",     # tools.get_weather(...); None = bare globals
)

with Runtime() as runtime:
    bridge.attach(runtime)
    print(runtime.eval("tools.get_weather('Zurich')"))
    print(bridge.calls_made, "of", 50)
```

- **The budget is total, not per tool.** It caps how many tool calls one
  agent turn may make, which is the quantity you actually want to bound.
  The call that exceeds it never reaches your Python function: JS gets a
  catchable error named `ToolBudgetError` (or, with
  `on_exhausted="silent"`, a `null` result).
- **Sync and async tools both work**, detected automatically; an async tool
  becomes an awaitable JS function.
- **Names are checked at construction**, not when JS happens to call them.
  Anything outside `[A-Za-z_][A-Za-z0-9_]*`, and names that would corrupt
  JavaScript's object machinery (`__proto__`, `constructor`, ...), are
  rejected immediately.
- `calls_made`, `calls_remaining`, `tool_names` and `reset_budget()` let you
  inspect and recycle the budget between turns.

`pydeno` ships `ToolError`, `ToolBudgetError` and `ToolNotFoundError` as a
shared vocabulary, but typed errors work for *any* Python exception class —
you do not have to inherit from them.

Only `ToolBudgetError` is raised by the library. `ToolNotFoundError` is there
for *your* tools to raise when a lookup inside one of them misses; `pydeno`
never raises it for an unknown tool *name*, because a name this bridge does
not expose is not a property on the namespace object at all, so guest JS gets
V8's own `TypeError: tools.nope is not a function`.

### `ToolBridge` requires `Runtime`

`ToolBridge.attach()` takes a [`Runtime`][pydeno.Runtime] and raises
`TypeError` immediately for anything else, because binding a Python callable
needs a real op registry (`deno_core::JsRuntime`) to attach to.

## Tips and Best Practices

**Keep functions simple**: Bound functions should be fast. If you have expensive operations, consider running them in a thread pool and returning a future.

**Bind early**: Set up all your bindings before running complex JavaScript. It's cleaner and easier to debug.

**Use meaningful names**: Make function names clear and follow JavaScript conventions (`camelCase`).

**Don't bind everything**: Only expose what JavaScript actually needs. Keep your API surface small.

**Remember the copy**: Objects are snapshots. For dynamic data, bind a function that returns fresh values.

## Next Steps

- Learn about [Type Conversion](../concepts/types.md) to understand how Python and JavaScript types map
- Explore [Modules](modules.md) to organize code with imports and exports
- See [`examples/tool_bridge.py`](https://github.com/bmsuisse/pydeno/blob/main/examples/tool_bridge.py)
  for a runnable `ToolBridge` + console-capture walkthrough
