# pydeno

**A JS sandbox for AI agents**

[![PyPI](https://img.shields.io/pypi/v/pydeno.svg)](https://pypi.org/project/pydeno/)

`pydeno` (**py**thon + **deno**) is a Python-embeddable JavaScript sandbox built
on [deno_core][deno_core] and the [V8][v8] engine. Run untrusted,
LLM-generated JavaScript safely from Python — real V8, real isolation, real
tool-calling.

Whether you need to run user scripts, integrate JavaScript libraries, execute code for AI agents, or build extensible Python applications, `pydeno` provides a robust solution.

## Highlights

- 🚀 **Fast**: Powered by [V8][v8], the JavaScript engine used in Chrome and Node.js
- 🔌 **Extensible**: Bind Python functions and objects to JavaScript
- ⚡ **Async First**: Support JavaScript [Promises][promise] and [async function][async function] in Python [asyncio][asyncio]
- 🔒 **Isolated Sandbox**: Runtime is a V8 isolate that has no I/O access by default
- 🎛️ **Resource Controls**: Prevent abuse by setting per-runtime heap memory limits and execution timeouts
- 🧵 **Parallelism**: Run multiple runtimes in parallel on different Python threads
- 📦 **Module Support**: ES modules with custom loaders and resolvers
- ⚙️ **WebAssembly**: Execute WebAssembly (WASM) directly in native runtime
- 🎯 **Typing**: Comprehensive type hints for PyO3 bindings

## Quick Example

```python
import pydeno

result = pydeno.eval("2 + 2")
print(result)  # 4

# Bind Python function
pydeno.bind_function("add", lambda a, b: a + b)
print(pydeno.eval("add(2, 3)"))  # 5
```

## About the Name

`pydeno` is **py**thon + **deno**: the [deno_core][deno_core] JavaScript engine,
wrapped in Rust and bolted onto Python via [PyO3][pyo3].

## Next Steps

- **[Quick Start](quickstart.md)** - Get up and running with essential features
- **[Core Concepts](concepts/runtime.md)** - Understand the runtime architecture and execution model
- **[Type Conversion](concepts/types.md)** - Learn how data types map between Python and JavaScript
- **[Use Cases](use-cases/playground.md)** - Explore practical examples and real-world applications
- **[API Reference](api/pydeno.md)** - Comprehensive API documentation for all classes and functions

[v8]: https://v8.dev/
[promise]: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Using_promises
[async function]: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Statements/async_function
[asyncio]: https://docs.python.org/3/library/asyncio.html
[deno_core]: https://crates.io/crates/deno_core
[pyo3]: https://pyo3.rs/
