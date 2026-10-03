# pydantic-ai code mode in JavaScript

`JSCodeMode` is a [pydantic-ai](https://ai.pydantic.dev) capability that puts an agent's tools
behind one tool, `run_javascript`. The model doesn't call `get_weather` three times over three
model turns. It writes one JavaScript snippet that calls `get_weather` three times, concurrently,
and returns the answer. The snippet runs in a pydeno
[`IsolatedRuntime`](advanced/isolation.md): a V8 isolate in a supervised worker process with an OS
sandbox, a hard deadline and a memory ceiling.

It is the JavaScript counterpart of `CodeMode` from
[pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness), which runs Python in
[Monty](https://github.com/pydantic/monty). The options mirror `CodeMode`'s, so you can switch
between the two by changing one line.

```bash
pip install pydeno pydantic-ai-slim   # plus your model provider's extra
```

Importing `pydeno` never imports pydantic-ai; only `pydeno.integrations.pydantic_ai` does.

## Quickstart

```python
from pydantic import BaseModel
from pydantic_ai import Agent
from pydeno.integrations.pydantic_ai import JSCodeMode

class Weather(BaseModel):
    city: str
    celsius: float

agent = Agent("openai:gpt-5-mini", capabilities=[JSCodeMode()])

@agent.tool_plain
async def get_weather(city: str) -> Weather:
    """Current temperature in a city."""
    ...

result = agent.run_sync("Which of Zurich, Lisbon and Oslo is warmest?")
```

The model sees a single tool, `run_javascript(code, restart?)`. The tool's description explains
the rules and declares your tools as TypeScript, generated from their JSON schemas:

```ts
declare namespace tools {
  /** Current temperature in a city. */
  function get_weather(args: {
    city: string;
  }): Promise<{
    city: string;
    celsius: number;
  }>;
}
```

A typical snippet looks like this:

```js
const cities = ["Zurich", "Lisbon", "Oslo"];
const reports = await Promise.all(cities.map((city) => tools.get_weather({ city })));
return reports.reduce((a, b) => (b.celsius > a.celsius ? b : a));
```

[`examples/pydantic_ai_agent.py`](https://github.com/bmsuisse/pydeno/blob/main/examples/pydantic_ai_agent.py)
runs this end to end with no API key, using pydantic-ai's `FunctionModel` as a scripted model. Set
`PYDENO_EXAMPLE_MODEL=openai:gpt-5-mini` (or any model string) to run it against a real model.

## Options

| Option | Default | Meaning |
|---|---|---|
| `tools` | `'all'` | Which tools become callable from JavaScript: `'all'`, a list of names, a predicate `(ctx, tool_def) -> bool`, or a metadata dict. The others stay native tool calls next to `run_javascript`. |
| `max_retries` | `3` | Retries for `run_javascript`. Syntax errors, uncaught errors and sandbox resets each cost one. |
| `max_tool_calls` | `100` | Nested tool calls per snippet. Further calls throw `ToolBudgetError` in JavaScript. |
| `max_session_tool_calls` | `None` | Nested tool calls per agent run, across all snippets. |
| `timeout` | `30.0` | Seconds of JavaScript execution per snippet. The worker is killed when it runs over. Time spent waiting on tools doesn't count. |
| `max_memory` | `256 MiB` | Resident-memory ceiling of the worker process. |
| `approvals` | `'inline'` | See [Approvals](#approvals). `'defer'` raises `NotImplementedError`. |
| `dynamic_catalog` | `False` | Put the declarations in the instructions instead of the tool description, so the tool definitions stay byte-stable (prompt cache) when tools appear mid-run. |
| `runtime_options` | `{}` | Extra `IsolatedRuntime` arguments: `sandbox="require"`, `max_host_wait`, `max_inflight_host_calls`, `clock`, `random_seed`, `jitless`, ... |

Some tools always stay native, whatever `tools` says:

- framework tools (`tool_kind` set: tool search, capability loading)
- tools that aren't available yet (deferred loading)
- output tools
- tools with a native counterpart (`unless_native`)
- other code-execution tools

`JSCodeMode` orders itself outermost, around `ToolSearch`, the same way `CodeMode` does.

## How it compares to `CodeMode` (Monty)

| | pydantic-ai-harness `CodeMode` | `JSCodeMode` |
|---|---|---|
| Language | Python (Monty's subset) | JavaScript (V8, `--jitless`) |
| Tool | `run_code(code, restart?)` | `run_javascript(code, restart?)` |
| Calling a tool | `await get_weather(city="Paris")` (keyword arguments) | `await tools.get_weather({city: "Paris"})` (one object argument) |
| Concurrency | `asyncio.gather(...)` | `Promise.all([...])` |
| Result | last expression | `return` value |
| Printed output | `print` → `{"output", "result"}` | `console.*` → `{"output", "result"}` |
| Stubs | Python signatures and `TypedDict`s | TypeScript `declare namespace tools { ... }` |
| Type check before running | yes (Monty type-checks the first snippet) | no; only V8's syntax check |
| Isolation | the Monty interpreter (or remote Monty workers) | V8 in a separate, OS-sandboxed process (Seatbelt / Landlock + seccomp) |
| Limits | `max_duration_secs`, `max_memory`, `max_suspensions` | `timeout` (hard kill), `max_memory` (RSS kill), CPU cap, `max_host_wait`, `max_inflight_host_calls` |
| Host access | optional `os_access` / `mount` | none: no filesystem, network, env or timers; expose tools instead |
| Clock / randomness | real clock unless `os_access` overrides | `Date.now()` frozen at session start, `Math.random` seeded |
| Startup | microseconds | one worker process per agent run (a prewarmed one when available) |
| Nested calls | nested `ToolManager.handle_call(wrap_validation_errors=False)` | the same |
| Approvals | inline via `HandleDeferredToolCalls` | the same |
| Eager / speculative execution | yes | no |

Under the hood both do the same thing. Each nested call becomes a `ToolCallPart` with id
`{parent}__{n}`, goes through pydantic-ai's own `ToolManager` (argument validation, capability
hooks, approval handling, usage accounting), and is recorded in the `run_javascript` return's
metadata.

## The calling convention

- **One object argument.** `await tools.name({field: value, ...})`, with the fields of the tool's
  parameters schema. A tool without required parameters can be called as `tools.name()`.
  Anything else (`tools.get_weather("Paris")`, two arguments) throws a `TypeError` that says how
  to call it. Properties set to `undefined` count as absent, so defaults apply.
- **Names.** A tool name that isn't a JavaScript identifier is mapped to one, and calls are mapped
  back to the real name: `get-weather` → `tools.get_weather`, `delete` → `tools.delete_`, and a
  collision gets a suffix (`get_weather_2`). The declaration notes the original name.
- **Results.** A tool's return value arrives as plain JSON data, in the shape its return type's
  JSON schema describes: models become objects, dates become ISO strings, bytes become base64.
- **The snippet's result.** The code is the body of an async function. Its `return` value is
  converted for the model: `undefined` → `null`, `NaN`/`Infinity` → strings, `Set` → array,
  `Date` → ISO string, `BigInt` → int, bytes → `{"bytes_base64": ...}`. With `console` output,
  the result is `{"output": "...", "result": ...}` (or just `{"output": ...}` without a
  `return`). A snippet that returns nothing and logs nothing gets a note telling the model to
  `return`.
- **State.** Top-level `const`/`let`/`var`/`function`/`class` declarations written at the start
  of a line are copied to `globalThis` when a snippet ends, so the next `run_javascript` call of
  the same agent run sees them. The detection is a pattern match, not a parser. For anything
  else, assign to `globalThis` yourself. `restart: true` starts from a fresh sandbox.
- **Metadata.** The `ToolReturnPart` of each `run_javascript` call carries `metadata` with
  `code_mode`, `language`, `tool_calls` and `tool_returns` (the nested parts, by id), `console`
  (`[{level, text}]`) and `duration_ms`.

## Errors

| What happened | What the model gets |
|---|---|
| Syntax error (checked by compiling the snippet first) | `ModelRetry("Syntax error: ...")`. Nothing ran. |
| Uncaught exception | `ModelRetry("Runtime error: Name: message")`, plus console output and a list of the tool calls that already started (with their outcomes), so a retry doesn't repeat side effects. |
| Over `timeout`, over `max_memory`, worker crash | The sandbox is reset (state is gone), then `ModelRetry` with the reason and the calls that already started. The next snippet gets a fresh worker. |
| The result can't leave the sandbox (a function, a symbol) | `ModelRetry` asking for plain data. The sandbox is kept. |
| `max_retries` spent | pydantic-ai's `UnexpectedModelBehavior`, as for any tool. |
| `UsageLimits(tool_calls_limit=...)` reached by a nested call | `UsageLimitExceeded` ends `agent.run`, even if the snippet catches the error it sees. |

A tool that fails inside a snippet throws a JavaScript `Error` at the `await`. The snippet can
catch it. Its `name` tells the snippet what happened, and its `message` is chosen deliberately:

| `e.name` | When | `e.message` |
|---|---|---|
| `ValidationError` | arguments failed validation | the fields, e.g. `invalid arguments for get_weather: city: Field required` |
| `ModelRetry` | the tool raised `ModelRetry` | its message |
| `ToolFailed` | the tool raised `ToolFailed` | its message |
| `ToolDenied` | an approval handler denied the call | the denial message |
| `ApprovalRequired` / `CallDeferred` | no handler resolved it | why |
| `ToolBudgetError` | `max_tool_calls` or `max_session_tool_calls` reached | the budget |
| `UsageLimitExceeded` | the run's `tool_calls_limit` reached | the limit |
| `TypeError` | the call didn't pass one object | how to call it |
| `ToolUnavailable` | the tool is no longer offered this step | the tool |
| any other Python class name | the tool raised something else | `tool 'x' failed (details are not shown)` |

Unexpected exceptions keep their class name but not their message, because messages can carry
paths, queries or secrets. A tool opts in to showing its messages with
`metadata={"expose_errors": True}`:

```python
@agent.tool_plain(metadata={"expose_errors": True})
def lookup(key: str) -> str: ...
```

## Approvals

`approvals="inline"` (the only mode so far) resolves a tool that needs approval
(`requires_approval=True`, or one that raises `ApprovalRequired`) during the snippet. The snippet
waits at its `await` while the agent's `HandleDeferredToolCalls` capability decides:

```python
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.tools import DeferredToolResults, ToolDenied

async def review(ctx, requests):
    return DeferredToolResults(approvals={
        call.tool_call_id: call.args["amount"] < 100 or ToolDenied("too large")
        for call in requests.approvals
    })

agent = Agent(model, capabilities=[JSCodeMode(), HandleDeferredToolCalls(handler=review)])
```

An approved call returns its value. A denied call throws `ToolDenied` in JavaScript. Without a
handler, it throws `ApprovalRequired`, and if the snippet doesn't catch it, the model gets a retry
that says a `HandleDeferredToolCalls` capability is needed. Time spent waiting for the decision
pauses the snippet's `timeout`, but counts against the worker's `max_host_wait` (600 s by
default, settable through `runtime_options`).

`approvals="defer"`, which would end the run with `DeferredToolRequests` and resume it later by
replaying the snippet with the recorded results (the way [`AgentSandbox`](agent-sessions.md)
replays a journal), is future work. Today it raises `NotImplementedError`.

## Limits and security

- **Each agent run gets its own worker,** started on its first `run_javascript` call and closed
  when the run ends, including when the run fails or is cancelled. Concurrent runs never share
  a sandbox.
- **The sandbox confines the JavaScript, not your tools.** Tools run in your process with its full
  authority, and their arguments are model output. Validate them (the schema does part of that)
  and don't expose a tool you wouldn't let the model call directly.
- **The guest only holds what it was given.** Each tool is bound as an unguessable capability
  token. A tool that is no longer offered in a later step is removed from `tools` and refuses
  calls, even through a reference the snippet kept. There is no `fetch`, `require`, `import`,
  `setTimeout`, filesystem, network, environment or wall clock. See
  [Isolated runtime](advanced/isolation.md) for the OS sandbox and its limits.
- **Concurrency is bounded.** At most `max_inflight_host_calls` (default 64) tool calls are in
  flight at once. Further ones fail with a `RuntimeError` in JavaScript. A tool marked
  `sequential=True` runs alone, and `ToolManager.parallel_execution_mode("sequential")` makes
  every nested call sequential.

## Limitations

- **No static type check.** Only V8's syntax check runs before the code. A wrong field name shows
  up as a `ValidationError` at runtime, and an unknown tool as a `TypeError`. Syntax error
  messages have no line numbers.
- **The model must `return`.** Monty's last-expression rule doesn't apply. A snippet that forgets
  `return` gets a note telling the model so.
- **No timers, network or filesystem.** Expose them as tools if the agent needs them.
- **State persistence is a heuristic.** Only declarations at the start of a line are kept, and a
  reset (timeout, crash, memory) discards all state.
- **One worker spawn per agent run.** `IsolatedRuntime` keeps one prewarmed worker ready, which
  saves most of the process start-up for the next run. A burst of concurrent runs pays the full
  spawn for all but the first.
- **No `approvals="defer"`, eager execution or speculation** (all of which `CodeMode` has).
- **Unannotated tools are `Promise<unknown>`.** A tool without a return annotation (or an MCP tool
  without `outputSchema`) gets that type, and `JSCodeModeReturnSchemaWarning` is emitted once per
  tool.
- **It relies on pydantic-ai internals,** as `CodeMode` does: `ToolManager(...)`,
  `handle_call(wrap_validation_errors=False)`, `ToolsetTool`, `WrapperToolset`. It is tested
  against pydantic-ai 2.46, so pin a compatible range.

## The schema converter

`schema_tools_to_dts(tools, namespace="tools")` is the converter behind the tool description, and
works on its own. It accepts `ToolDefinition`s or plain mappings with `name`,
`parameters_json_schema` and, optionally, `return_schema`, `description` and `sequential`:

```python
from pydeno.integrations.pydantic_ai import schema_tools_to_dts

print(schema_tools_to_dts([{
    "name": "search",
    "parameters_json_schema": {"type": "object", "properties": {"q": {"type": "string"}},
                               "required": ["q"]},
    "return_schema": {"type": "array", "items": {"type": "string"}},
}]))
```

It handles:

- `$ref`/`$defs`, including recursive models, as named `interface`/`type` declarations
- `anyOf`/`oneOf` (unions) and `allOf` (intersections)
- `enum`/`const` literals and `type` lists
- `prefixItems` tuples, with rest elements
- required and optional properties, and `additionalProperties` as `Record<...>` or an index signature
- quoted keys for names that aren't identifiers
- JSDoc from `description`, `format` and `default`

`integer` and `number` both become `number`, and every string format becomes `string`. Anything
it can't express becomes `unknown`. `js_tool_names(names)` exposes the name mapping.
