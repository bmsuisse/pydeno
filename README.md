<div align="center">

# peno

**A JS sandbox for AI agents**

`peno` = **p**ython + d**eno**: a Python-embeddable JavaScript sandbox built on
[deno_core][deno_core] and [V8][v8].

Run untrusted, LLM-generated JavaScript safely in Python — real V8, real isolation, real tool-calling.

<br />

[![Publish](https://github.com/bmsuisse/peno/actions/workflows/workflow.yaml/badge.svg)][workflows-ci]
[![PyPI](https://img.shields.io/pypi/v/peno.svg)][peno-pypi]
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-bmsuisse.github.io%2Fpeno-blue)][peno-docs]

<p align="center">
  <a href="https://bmsuisse.github.io/peno/"><strong>Documentation</strong></a>
  ·
  <a href="https://github.com/bmsuisse/peno/tree/main/examples"><strong>Examples</strong></a>
  ·
  <a href="https://github.com/bmsuisse/peno/issues"><strong>Issues</strong></a>
</p>

**Retain one `Runtime` per session.** A warm `Runtime` does a *complete*
host tool call — JS calls a bound Python function, the op crosses the
boundary, a value comes back — in **~13.4&nbsp;µs**, and ~3.8&nbsp;µs for a
bare `eval`. That is the fast path, and it is the one that supports tool
calling. Measured with Criterion and pytest-benchmark — proven, not claimed:
see [`BENCHMARKS.md`](BENCHMARKS.md).

</div>

## What this is

Agent frameworks and tool-execution runtimes increasingly need to run
**JavaScript the model itself wrote** — a code-generation step, a small data
transform, a "call this JS snippet to compute the answer" tool. That code is
untrusted by construction. `peno` embeds a real [V8][v8] isolate per
runtime (via Rust/[PyO3][pyo3]) so that code runs with no filesystem, no
network, and no ambient Node.js APIs by default, with heap and wall-clock
limits you set, and a real way to kill it if it doesn't stop on its own.

It is not a general "embed any JS anywhere" library and not a JS
interpreter reimplemented in Python — it's V8, the same engine behind
Chrome and Node.js, so the JS your model writes actually behaves like JS.

```python
import peno

peno.bind_function("add", lambda a, b: a + b)
print(peno.eval("add(2, 3)"))  # 5
```

## Why it's different

- **Real V8, not a toy interpreter.** Every language quirk, every standard
  library method, every performance characteristic your model expects from
  "JavaScript" is the real thing.
- **Cross-thread termination that actually works.** A watchdog thread
  calling `.terminate()` on a runaway `while(true){}` used to panic instead
  of stopping the code (`pyo3_runtime.PanicException: Runtime is
  unsendable, but sent to another thread`) — a real bug in the upstream
  design that meant a stuck agent tool call couldn't be killed from outside.
  `TerminationHandle` fixes this: it exposes only the `Send + Sync` part of
  V8's isolate handle, so a watchdog on a separate thread can interrupt
  execution safely. See [`upstream-divergence.md`](docs/contributing/upstream-divergence.md) for the full root cause and
  proof; `tests/test_termination_handle.py` is the regression test.

  As of 0.2.0 this works on *every* shape of stuck script, not just runaway
  loops. `terminate()` used to be a no-op against a runtime parked on a
  pending promise — V8 only acts on a termination when it next enters
  JavaScript, and `new Promise(() => {})` never does, so it was unkillable.
  The dispatcher now reads the termination flag between polls, killing all
  such shapes in **~1.7 ms**, while `while(true){}` still dies the cheap way
  at ~0.12 ms and a killed runtime keeps its bound functions. See
  [`BENCHMARKS.md`](BENCHMARKS.md) and
  [`tests/test_parked_termination.py`](tests/test_parked_termination.py).
- **A host-callback bridge for tool-calling from JS.** `bind_function`/
  `bind_object` let the sandboxed JS call back into Python — the primitive
  an agent needs when the model's JS wants to invoke a real tool, not just
  compute a value. `ToolBridge` builds the product shape on top: hand it a
  dict of callables and get a call budget, fail-closed name checking, and
  JS-catchable typed errors, instead of hand-rolling all three.
- **Tool errors JS can branch on.** A Python exception raised inside a bound
  tool reaches JavaScript as a real `Error` whose `name` is the exception's
  class name, so the model's JS can tell "bad arguments" from "rate limited"
  from "tool not found" with a plain `catch (e) { if (e.name === ...) }` —
  rather than string-matching one flattened `TypeError`. The same
  `name`/`message` shape a JS exception already carries into Python, so the
  two directions are symmetric.
- **`console.log` you can actually read.** Sandboxed `console.*` output can
  be routed back to a Python callback with `RuntimeConfig(on_console=...)`,
  receiving structured `(level, args)` — the single most common thing you
  want when showing a model what its own script printed. Composes with
  `enable_console` rather than replacing it.

## Get Started

```bash
pip install peno  # or uv pip install peno
```

Requires Python 3.10+ on macOS or Linux. `peno` is experimental —
expect breaking changes between versions.

### Quickstart: sandboxed eval, tool-calling, and a timeout

```python
import asyncio

from peno import Runtime, RuntimeConfig

# Cap memory; the runtime terminates itself if JS tries to exceed it.
config = RuntimeConfig(max_heap_size=10 * 1024 * 1024)  # 10 MB


async def main():
    with Runtime(config) as runtime:
        # The tool-calling primitive: expose a Python function to the sandboxed JS.
        def get_weather(city: str) -> str:
            return f"72F and sunny in {city}"

        runtime.bind_function("getWeather", get_weather)

        # Model-generated JS calling back into your tool.
        result = runtime.eval("getWeather('Zurich')")
        print(result)  # "72F and sunny in Zurich"

        # Enforce a wall-clock timeout on code that might not terminate.
        try:
            await runtime.eval_async("while (true) {}", timeout=2.0)
        except Exception as exc:
            print(f"killed a runaway eval after 2s: {exc}")


asyncio.run(main())
```

Running an untrusted sync `eval()` you can't await? Grab a
[`TerminationHandle`](https://bmsuisse.github.io/peno/api/runtime/)
before the call and `.terminate()` it from a watchdog thread — see
[`tests/test_termination_handle.py`](tests/test_termination_handle.py) for
the exact pattern (and the bug it fixed, in
[`docs/contributing/upstream-divergence.md`](docs/contributing/upstream-divergence.md)).

### Many tools, one budget

When the model gets more than one tool, `ToolBridge` gives you the whole
surface — budget, name checking, typed errors — in one object:

```python
from peno import Runtime, ToolBridge

bridge = ToolBridge(
    {"get_weather": get_weather, "send_email": send_email},
    max_calls=50,  # total tool calls this agent turn may make
)

with Runtime() as runtime:
    bridge.attach(runtime)
    runtime.eval("tools.get_weather('Zurich')")
```

The call that exceeds `max_calls` never reaches your Python function — JS
gets a catchable `ToolBudgetError`.

## Integrations

- [**FastMCP tool bridge**](examples/fastmcp_tool_bridge.py) - expose FastMCP tools to sandboxed JS via `bind_function` and an in-process `fastmcp.Client`
- [**pydantic-ai "code mode" agent**](examples/pydantic_ai_agent.py) - an agent tool where the model submits one JS batch script instead of many separate tool calls, run safely with a timeout
- [**ToolBridge**](examples/tool_bridge.py) - hand a sandbox several Python tools with a total call budget, typed errors the model's JS can branch on, and `console.log` routed back to Python
- [**Vendored npm libraries**](examples/vendored_npm_libraries.py) - run real npm document-generation libraries (`pptxgenjs`, `pdf-lib`) from their browser bundles inside the sandbox; see the [guide](https://bmsuisse.github.io/peno/guides/advanced/vendored-npm-libraries/) for what makes a library a good candidate
- [**Arrow IPC dataframes**](examples/arrow_ipc_dataframes.py) - move 100k+ row tables into the sandbox as Arrow IPC `bytes` instead of JSON objects (5 ms vs 253 ms, and it works at the default 10 MB serialization limit that rejects the JSON payload); see the [guide](https://bmsuisse.github.io/peno/guides/advanced/arrow-ipc-dataframes/)

## Documentation

- [Quick Start](https://bmsuisse.github.io/peno/quickstart/)
- [Concepts](https://bmsuisse.github.io/peno/concepts/runtime/): runtimes, type conversion, resource controls
- [Guides](https://bmsuisse.github.io/peno/guides/bindings/): binding functions, module loading, snapshots
- [Use cases](https://bmsuisse.github.io/peno/use-cases/ai-agent/): AI agent sandboxes, workflow runners, plugin systems
- [API reference](https://bmsuisse.github.io/peno/api/peno/)
- [Benchmarks](BENCHMARKS.md) — measured, reproducible numbers, including the pooling comparison above

[v8]: https://v8.dev
[deno_core]: https://crates.io/crates/deno_core
[pyo3]: https://pyo3.rs/
[peno-pypi]: https://pypi.org/project/peno/
[peno-docs]: https://bmsuisse.github.io/peno/
[workflows-ci]: https://github.com/bmsuisse/peno/actions/workflows/workflow.yaml
