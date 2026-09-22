# Quick Start

## Installation

Create or activate a virtual environment inside your project and install the package:

```bash
python -m venv .venv
source .venv/bin/activate

pip install pydeno  # or `uv install pydeno`
```

!!! warning "Platform Support"

    Supports macOS (Apple Silicon) and Linux (x86_64, ARM64) with glibc ([manylinux](https://github.com/pypa/manylinux)). Windows and musl-based distributions (e.g., Alpine) are not supported currently.


## Run JavaScript from Python

Use [`pydeno.eval`][pydeno.eval] to evaluate JavaScript code directly:

```python
>>> import pydeno
>>>
>>> print(pydeno.eval("2 + 2"))
4
>>> print(pydeno.eval("Math.sqrt(25)"))
5
```

`pydeno.eval()` runs synchronously and returns the result immediately.

## Share functions and data

Bind Python callables or objects so they are visible from JavaScript:

```python
import pydeno

pydeno.bind_function("notify", lambda msg: print("JS:", msg))
pydeno.eval("notify('hello from JS')")

pydeno.bind_object("config", {"debug": True})
pydeno.eval("config.debug")
```

Once bound, they will remain available to all subsequent evaluations.

## Run code asynchronously

Use `eval_async` to run JavaScript without blocking Python’s event loop.
This is useful when JavaScript performs long-running work or returns a [Promise][promise].

```python
import asyncio

async def main():
    # JavaScript expression
    result = await pydeno.eval_async("42")

    # Code that returns a Promise is also supported
    result = await pydeno.eval_async("Promise.resolve('done')")
    print(result)

asyncio.run(main())
```

## Keep types familiar

`pydeno` automatically converts common data types between Python and JavaScript, so you can work with familiar types on both sides. Numbers, strings, booleans, lists, dictionaries, and more are converted seamlessly.

```python
# Python → JavaScript → Python
result = pydeno.eval("[1, 2, 3].map(x => x * 2)")
print(result)  # [2, 4, 6]
```

For complete details on type conversion, including special types like `undefined`, `BigInt`, `Date`, and binary data, see [Type Conversion](concepts/types.md).

## Working with Runtime directly

!!! note "pydeno vs. Runtime"

    The [`pydeno`][pydeno] module provides a convenient interface that automatically manages a context-local [`Runtime`][pydeno.Runtime] for you.
    Each asyncio task or thread gets its own isolated runtime instance, created lazily and cleaned up automatically.
    This makes it perfect for everyday use where you just want to run JavaScript without managing runtime lifecycle.

For more control, you can work with the [`Runtime`][pydeno.Runtime] class directly:

```python
from pydeno import Runtime

runtime = Runtime()  # Create a runtime instance

runtime.eval("let counter = 0")
runtime.eval("counter++")
print(runtime.eval("counter"))  # 1

runtime.close()  # Clean up when done
```

Or use it as a context manager for automatic cleanup:

```python
with Runtime() as runtime:
    print(runtime.eval("42"))
```

Each [`Runtime`][pydeno.Runtime] runs on a dedicated thread with its own V8 isolate, where state persists across evaluations.

## Next steps

- Learn more about [`Runtime`][pydeno.Runtime] in [Concepts](concepts/runtime.md)

[promise]: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise
