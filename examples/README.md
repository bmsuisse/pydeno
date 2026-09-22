# pydeno Examples

This directory contains practical examples demonstrating pydeno's features and use cases.

## Getting Started

### Basic Usage

- [**basic_eval.py**](basic_eval.py) - Introduction to pydeno with both context-local API and explicit Runtime usage
- [**context_local_api.py**](context_local_api.py) - Comprehensive guide to the context-local API with automatic runtime management

### Bindings

- [**bindings_basic.py**](bindings_basic.py) - Exposing Python functions and data to JavaScript
- [**bindings_async.py**](bindings_async.py) - Async function bindings and decorator-style syntax

### Type System

- [**type_conversion.py**](type_conversion.py) - Reference for type conversion between Python and JavaScript

## Advanced Features

### Modules

- [**modules_basic.py**](modules_basic.py) - ES6 module loading with static modules and custom resolvers
- [**modules_wasm.py**](modules_wasm.py) - Loading and executing WebAssembly modules

### Performance

- [**snapshot_example.py**](snapshot_example.py) - Pre-initialize V8 for faster startup times

### Security & Resource Control

- [**resource_limits.py**](resource_limits.py) - Memory limits, timeouts, and safe execution of untrusted code

### Debugging

- [**inspector.py**](inspector.py) - Chrome DevTools integration for debugging JavaScript execution

## Integration Examples

### Web Applications

- [**fastapi_multitenant.py**](fastapi_multitenant.py) - Multi-tenant JavaScript execution with FastAPI, resource limits, and error handling

### External Libraries

- [**markdown_parser.py**](markdown_parser.py) - Load external JS libraries (marked.js) from CDN and expose async functions to Python
- [**vendored_npm_libraries.py**](vendored_npm_libraries.py) - Run real npm document-generation libraries (`pptxgenjs`, `pdf-lib`) from their browser bundles, host-supplied polyfills only, no `require()`/npm access for guest code — see [`docs/guides/advanced/vendored-npm-libraries.md`](../docs/guides/advanced/vendored-npm-libraries.md) for the full pattern and what did/didn't work
- [**pptxgenjs_presentation.py**](pptxgenjs_presentation.py) - The same pattern taken all the way: the pinned, unmodified 460,889-byte `pptxgenjs` 4.0.1 bundle from [`vendor/pptxgenjs/`](../vendor/pptxgenjs/) builds a six-slide deck (table, three native OOXML charts including a combo chart on a secondary axis, an embedded PNG), then validates it with `zipfile`, reads it back with `python-pptx`, and renders every slide to PNG via LibreOffice so you can actually look at it. No network needed. Hermetic regression guard: [`tests/test_vendored_bundle_execution.py`](../tests/test_vendored_bundle_execution.py)

### Concurrency

- [**threading_gil.py**](threading_gil.py) - Per-thread runtime isolation and GIL release demonstration

### Agent Frameworks

- [**fastmcp_tool_bridge.py**](fastmcp_tool_bridge.py) - Bridge a FastMCP server's tools into a `pydeno.Runtime` via `bind_function`, so sandboxed JS can call real tools through an in-process `fastmcp.Client`
- [**pydantic_ai_agent.py**](pydantic_ai_agent.py) - A `pydantic-ai` `Agent` with a "code mode" tool: the model submits one JS batch script instead of N separate tool calls, run safely with a timeout (uses `FunctionModel` so it runs offline, no API key)

Both examples need extra dependencies not required by `pydeno` itself: `uv sync --group examples` (or `pip install pydantic-ai fastmcp`).

`pptxgenjs_presentation.py` also uses `python-pptx` (in the same `examples` group) to read its output back, and optionally LibreOffice + Poppler (`soffice`, `pdftoppm`) to render slides to PNG. The render step is skipped with a clear message if those aren't installed; validation still runs.

## Running Examples

All examples can be run directly with Python:

```bash
python examples/basic_eval.py
python examples/context_local_api.py
python examples/bindings_basic.py
# ... etc
```

Or using uv:

```bash
uv run python examples/basic_eval.py
```
