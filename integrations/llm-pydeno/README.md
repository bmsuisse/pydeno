# llm-pydeno

A plugin for [`llm`](https://llm.datasette.io/) that gives a model a sandboxed JavaScript
interpreter, backed by [pydeno](https://github.com/bmsuisse/pydeno). The code runs in V8 inside a
separate worker process under the OS sandbox (Seatbelt on macOS, Landlock and seccomp on Linux),
with no filesystem, network, processes or `require`. The session keeps its state between calls.

This is a separate package: installing `pydeno` does not install `llm`, and this plugin is the only
thing that depends on both.

> **Warning: one toolbox per conversation and user.** A `PyDeno` instance is one JavaScript global
> scope for as long as it lives. Everything a call defines (variables, functions, `globalThis`
> properties, data a model pasted in) is visible to every later call on the same instance. Never
> share one instance between users or conversations (for example as a module-level object in a
> server). Create one per conversation, or pass `fresh_session_per_call=True` to keep nothing
> between calls. Each `llm` command builds its own instance from `-T` (and `llm chat` keeps that
> one for the whole chat).

## Install

The package is not on PyPI yet. From a checkout of the pydeno repository:

```bash
llm install -e integrations/llm-pydeno
```

macOS or Linux, Python 3.10 or newer (the same platforms as pydeno's sandbox).

## Use

```bash
llm -T PyDeno 'What is the 30th Fibonacci number? Compute it.' --td
```

`--td` shows each tool call and its result. Options are passed as JSON:

```bash
llm -T 'PyDeno({"timeout": 5, "max_memory_mb": 128})' '...'
```

| Option | Default | Meaning |
|---|---|---|
| `timeout` | 10 | Seconds of JavaScript running time per call (above 0, at most 86400). Exceeding it stops the worker |
| `max_memory_mb` | 256 | Resident memory cap of the worker, in MiB |
| `max_output_bytes` | 16384 | Cap on each of `stdout` and `stderr` per call; past it the stream ends with `[truncated]` |
| `max_result_bytes` | 65536 | Cap on the returned value as JSON; a larger one is a failed call (the session goes on) |
| `sandbox` | `"require"` | `"require"` refuses to run without the complete OS sandbox; `"auto"` applies what the platform offers |
| `fresh_session_per_call` | `false` | Start every call in a new worker and stop it afterwards: no state is kept, at the cost of one worker start per call |

Calls on one instance run one at a time, even from several threads. A dropped instance stops its
worker process when Python collects it (`llm.Toolbox` has no close hook); `toolbox._close()` stops
it at once.

From Python:

```python
import llm
from llm_pydeno.plugin import PyDeno

model = llm.get_model("gpt-4.1-mini")
response = model.chain("Sum the squares of 1 to 100 using JavaScript.", tools=[PyDeno()])
print(response.text())
```

## The tool

The toolbox has one tool, `PyDeno_run_javascript(code)`. The code is the body of an async function:
`return` gives the result and `await` works at the top level. `const`, `let`, `var`, `function` and
`class` declarations that start a line, and `globalThis` properties, are kept for later calls (an
indented or destructured declaration is not; assign it to `globalThis` instead).

It returns pydeno's `ExecutionResult` fields as a JSON object:

```json
{"status": "Succeeded", "stdout": "stored\n", "stderr": "", "result": 3,
 "error": null, "error_type": null, "truncated": false}
```

- `stdout`: `console.log`, `info`, `debug`; `stderr`: `console.warn`, `error`, `trace`.
- `result`: the returned value as JSON (`undefined` is `null`, bytes are base64, dates ISO 8601).
- `error`, `error_type`: for a failed call, the message and a stable name (`TypeError`,
  `ResultTooLarge`, `RuntimeTimeout`, ...).

A call that stops the worker (a timeout, the memory cap) ends that session; the next call starts a
fresh one, and the failed call's `error` says that earlier state is gone.

The tool is synchronous. `llm` runs an `async def` tool from a synchronous chain with a new event
loop per call, and pydeno's `AsyncAgentSandbox` is bound to the loop it started on, so the plugin
uses the synchronous `AgentSandbox`, which runs on the same `IsolatedRuntime` worker.

Without `llm`, the engine is usable on its own:

```python
from llm_pydeno import JavaScriptSession

session = JavaScriptSession(timeout=5)
session.run("const xs = [3, 1, 2]")
print(session.run("return xs.sort()"))   # {'status': 'Succeeded', ..., 'result': [1, 2, 3], ...}
session.close()
```

## Tests

```bash
cd integrations/llm-pydeno
python -m pytest
```

`tests/test_session.py` needs only pydeno; `tests/test_llm_plugin.py` drives the toolbox through
`llm`'s tool-calling chain with a scripted model (no API key) and needs `llm` installed.
