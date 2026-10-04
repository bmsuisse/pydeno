# Agent sessions

An AI agent that can write code needs more than "evaluate this string". It needs to call your
tools from that code, to keep what it computed between turns, to stop at a tool call so a human
(or a policy) can approve it, and to survive the process it runs in being restarted while it waits.
[pydantic/monty](https://pydantic.dev/monty) offers that for agents that write Python.
`AgentSandbox` offers it for agents that write JavaScript, on top of
[`IsolatedRuntime`](advanced/isolation.md): every session runs in its own sandboxed worker process.

| Monty | `AgentSandbox` |
|---|---|
| external functions | `tools={"name": callable}` (sync or async), or tools described by JSON Schema; one call budget per session |
| type-checking stubs, `TOOL_DESCRIPTION` | `typescript_stubs()`, `describe_tools()` (from signatures or schemas) |
| - | `tools_catalog=`: hundreds of tools, only `search_tools`/`describe_tool` declared |
| result with captured output | `execute(code)` → `ExecutionResult(status, stdout, stderr, result, error, error_type, truncated)` |
| session state between feeds | globals and functions persist between `run()`/`start()` calls |
| `feed_start` → `FunctionSnapshot` → `resume` | `start()` → `ToolCall` → `resume()` |
| `dump()` / `load_snapshot()` | `dump(key)` / `AgentSandbox.load(blob, key, tools)` by deterministic **replay** (see the limits below) |

## Quickstart

```python
from pydeno import AgentSandbox

def query_rows(sql: str) -> list[dict]:
    """Run a read-only SQL query against the orders table."""
    ...

async def analyze_sentiment(text: str) -> float:
    """Score text from -1.0 (negative) to +1.0 (positive)."""
    ...

with AgentSandbox({"query_rows": query_rows, "analyze_sentiment": analyze_sentiment},
                  max_tool_calls=100) as session:
    system_prompt = session.describe_tools()          # paste into the model's system prompt
    code = my_llm(system_prompt, "Which customers are unhappy?")
    result = session.run(code)                        # tools answered by the real functions
    follow_up = session.run(my_llm(system_prompt, "Show their emails"))  # sees the first run's state
```

`run()` returns the code's result or raises what it failed with (a `JavaScriptError` for a bug in
the model's code, which you can hand back to the model as a retry prompt). `execute()` runs the
same way but returns one bounded result instead of raising; see below.

## Results and console output

```python
result = session.execute(code)
result.status      # "Succeeded" or "Failed"
result.stdout      # console.log / info / debug, one line per call, in call order
result.stderr      # console.warn / error / trace
result.result      # the returned value as JSON data (None when the run failed)
result.error       # "TypeError: x is not a function", or None
result.error_type  # "TypeError", "ReferenceError", "ToolBudgetError", "RuntimeTimeout", ...
result.truncated   # some console output was cut
result.to_dict()   # plain JSON, e.g. for a tool result or an API response
```

The same shape comes from every step: `Done` and `Failed` carry `stdout`, `stderr` and
`truncated` (the output of the whole run, across its pauses) plus `status`, `result` and
`error_type`, and `step.to_result()` builds the `ExecutionResult`. `IsolatedRuntime.execute(code)`
and `execute_async(code)` return it too (pass `capture_console=True` when creating the runtime to
collect console output; every `console.*` call is then a host call).

- **Bounded output.** `stdout` and `stderr` are each capped at `max_output_bytes` (default 64 KiB,
  UTF-8, never splitting a character). Past the cap the stream ends with a `[truncated]` line,
  `truncated` is set, and further calls are dropped without being formatted.
- **Bounded result.** `result` is the value as compact JSON data: `undefined` becomes `null`,
  bytes become base64 text, dates ISO 8601 text, sets lists, `NaN`/`Infinity` `null`. If that is
  larger than `max_result_bytes` (default 1 MiB), the run is `Failed` with
  `error_type == "ResultTooLarge"`, and the session stays usable (`run()` raises
  `pydeno.ResultTooLarge`). The cap decides outcomes, so it is recorded in the journal.
- **Stable `error_type`.** For an error the guest threw it is the JavaScript `name` (`TypeError`,
  `SyntaxError`, a tool's `ToolBudgetError`, or a name the guest's code chose). For a failure on the
  host's side it is the pydeno exception class (`RuntimeTimeout`, `WorkerCrashed`,
  `ResultTooLarge`). A guest error that claims one of those host-side names is reported as
  `Error`, so `error_type` never says "the worker timed out" because the guest said so.
- `execute()` still raises for misuse (non-string code, a paused or closed session).
- `Done(3) == step` compares the value only; the output fields are not part of equality.

## How the model's code runs

The code is the **body of an async function**: tools return Promises, so it writes `await`, and it
ends with `return value`. That is what `describe_tools()` tells the model, in these words:

```text
You can run JavaScript in a sandbox. Write the code as the body of an async function:
call tools with `await` and `return` the final result. Top-level `const`, `let`, `var`,
`function` and `class` declarations written at the start of a line are kept for later
runs; to keep anything else, store it on `globalThis`. There is no network, filesystem,
`require` or `import`. `Date.now()` is frozen and `Math.random()` is seeded. A tool that
fails throws an Error whose `name` is the failure's type.
```

- **State.** When a run ends, its top-level declarations (recognised at the start of a line; there
  is no parser behind this, so an indented or destructured declaration is not kept) are copied to
  `globalThis`, and later runs see them as globals. Anything else the code wants to keep goes on
  `globalThis` explicitly.
- **No call is left behind.** A run does not end while one of its tool calls is unanswered, even a
  call the code forgot to `await`, or one still running when a `Promise.all` rejected. (A worker
  that finished its command and then received a late answer would break; the session prevents it.)
- **Tools are called positionally** (`query_rows("SELECT ...")`), since JavaScript has no keyword
  arguments. A tool with a *required* keyword-only parameter is refused when the session is made.

## Telling the model about the tools

`describe_tools()` is a block for the system prompt: the rules above, then for each tool its
signature, its docstring and an example call. `typescript_stubs()` is the same information as a
`.d.ts` file, which you can show the model, or use to type-check its code with `tsc` before running
it (the same idea as Monty's `type_check_stubs`). Both come from the Python signatures:

```python
def query_rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a read-only SQL query against the orders table."""
```

```ts
/**
 * Run a read-only SQL query against the orders table.
 */
declare function query_rows(sql: string, params?: Record<string, unknown> | null): Promise<Record<string, unknown>[]>;
```

| Python | TypeScript |
|---|---|
| `int`, `float` | `number` |
| `str` / `bool` / `None` | `string` / `boolean` / `null` |
| `bytes`, `bytearray` | `Uint8Array` |
| `datetime` | `Date` |
| `list[T]`, `Sequence[T]`, `tuple[T, ...]` | `T[]` |
| `tuple[A, B]` | `[A, B]` |
| `dict[str, T]`, `Mapping[str, T]` | `Record<string, T>` |
| `set[T]` | `Set<T>` |
| `X \| Y`, `Optional[X]`, `Union[...]` | `X \| Y`, `X \| null` |
| `Literal["a", 1]` | `"a" \| 1` |
| a parameter with a default / `*args` | `name?: T` / `...name: T[]` |
| anything else (`Any`, classes, `dict[int, T]`, no annotation) | `unknown` |

Every tool returns `Promise<...>`. Both helpers also exist as module-level functions that take the
tools mapping, so you can build a prompt without starting a worker.

## Tools described by JSON Schema

A host with tools described by JSON Schema (MCP style) rather than Python signatures passes them as
`SchemaTool`s, or as plain mappings with the same keys (`inputSchema`/`outputSchema`, the MCP
spellings, work too), in a list or as values of the tools mapping, mixed with plain callables:

```python
from pydeno import AgentSandbox, SchemaTool

def get_weather(args: dict) -> dict:
    city = args.get("city")
    if not isinstance(city, str) or len(city) > 100:      # validate: it is untrusted guest data
        raise ValueError("city must be a short string")
    return {"city": city, "temp": 21.5}

weather = SchemaTool(
    name="get_weather",
    description="Current weather for a city.",
    input_schema={"type": "object", "properties": {"city": {"type": "string"}},
                  "required": ["city"]},
    output_schema={"type": "object", "properties": {"temp": {"type": "number"}}},
    callable=get_weather,
)
session = AgentSandbox([weather, {"name": "now", "description": "...", "callable": now}])
```

A schema tool takes **one object argument**: the guest writes
`await get_weather({city: "Paris"})` and the callable receives `{"city": "Paris"}` (properties set
to `undefined` are dropped; no argument means `{}`). Anything else throws a `TypeError` in the
guest that says how to call the tool. `typescript_stubs()` declares it from the schemas:

```ts
/** Current weather for a city. */
declare function get_weather(args: {
  city: string;
}): Promise<{
  temp?: number;
}>;
```

Objects (required and optional properties, `additionalProperties` as an index signature), arrays
and `prefixItems` tuples, `enum`/`const` literals, `anyOf`/`oneOf` unions, `allOf`
intersections, `nullable: true` and `type: [..., "null"]`, and `$ref` into `$defs` (nested and
recursive) as named `interface`/`type` declarations, shared by all the tools. Without an
`output_schema` the result is `Promise<unknown>`.

**pydeno does not validate the arguments against the schema.** The schema tells the model what to
send; the callable must check what it got, as for any tool (type, range, length, allowed values),
before acting on it. A JSON Schema validator in the callable is a fine way to do that.

## A lazy tool catalog

Declaring a hundred tools in every prompt is expensive. With `tools_catalog=`, those tools are
**not** declared: only `search_tools(query, limit?)` and `describe_tool(name)` are, so the declared
surface (`describe_tools()`, `typescript_stubs()`, and the script installed in the guest) is the
same size whether the catalog holds three tools or three thousand.

```python
session = AgentSandbox({"send_reply": send_reply}, tools_catalog=mcp_tools, max_tool_calls=50)
```

```js
const hits = await search_tools("translate text");      // [{name, description}], best first
const info = await describe_tool(hits[0].name);         // {name, description, input_schema,
                                                         //  output_schema, typescript, usage}
return await tools.translate_text({text: "hi", to: "de"});
```

- A catalog tool becomes callable once a `search_tools` or `describe_tool` answer has shown it to
  the guest, and stays callable for the rest of the session (and after `load()`: discovery is
  replayed from the recorded answers). Before that, `tools.anything(...)` throws a
  `ToolNotDiscoveredError` whose message tells the model to search first. This message is never
  redacted: pydeno wrote it, and it holds nothing of yours.
- Catalog tools are called as `<namespace or "tools">.<name>(args)`, with one object argument.
  The guest-side `tools` object is a proxy that turns every unknown name into a call to one hidden
  host function; **what such a call may reach is decided in the host**, which checks that the name
  is in the catalog and has been found. Replacing the proxy only fools the guest's own code.
- Catalog calls, `search_tools` and `describe_tool` are tool calls like any other: `ToolCall`
  steps when you drive the session with `start`/`resume`, and charged to `max_tool_calls`. A
  refused (not yet found) call is not charged. A driver can run any call with the real tool via
  `session.call(step)`; what an answer to `search_tools` names is what becomes callable, whoever
  produced the answer.
- Search is a plain keyword match over names and descriptions (names weigh more), up to `limit`
  results (default 10, at most 50); descriptions in results are cut at 200 characters.
- `load()` needs the same `tools_catalog` (by name) as the original session.

## Budgets per tool

`max_tool_calls` is one budget for the whole session. For a limit on one tool (at most three
emails, say), count in the tool itself and raise when it is spent; the guest sees an Error whose
`name` is your exception's class and can stop or adapt:

```python
from pydeno import ToolBudgetError

def per_tool_budget(fn, limit):
    used = 0
    def guarded(*args):
        nonlocal used
        if used >= limit:
            raise ToolBudgetError(f"{fn.__name__} may be called {limit} times per session")
        used += 1
        return fn(*args)
    guarded.__name__, guarded.__doc__ = fn.__name__, fn.__doc__
    return guarded

session = AgentSandbox({"send_email": per_tool_budget(send_email, 3), ...})
```

The counter lives in your process, so it does not survive `load()` by itself; tools are not called
during replay, so a restored session starts from zero unless you keep the count in your own store
(next to the journal) and restore it.

## Pausing at tool calls

`start(code)` runs until the code calls a tool, then hands you the call instead of running it:

```python
from pydeno import AgentSandbox, Done, Failed, ToolCall

with AgentSandbox(tools) as session:
    step = session.start(code)
    while isinstance(step, ToolCall):
        if step.name == "send_email" and not reviewer_approves(step.args):
            step = session.resume(step, error=PermissionError("denied by reviewer"))
        else:
            step = session.resume(step, tools[step.name](*step.args))
    if isinstance(step, Failed):
        raise step.error
    print(step.value)
```

- A step is a `ToolCall(name, args, call_id)`, `Done(value)` or `Failed(error)`.
- `resume(step, value)` makes the tool call return `value`; `resume(step, error=exc)` makes it
  throw an Error whose `name` is `type(exc).__name__` (the message is replaced by
  `"host function failed"` unless the session was made with `redact_host_errors=False`).
- Calls the code makes concurrently (`Promise.all`) come to you one at a time, in the order the
  code made them. Only the call the session is paused at can be answered, and only once.
- `run(code)` is this loop with the real tools. A tool that raises is reported to the guest the
  same way, so the model's code can catch it.
- Time paused at a tool call does not count against `timeout=` (the guest's own running time per
  run). It does count against `max_pause=` (default 600 s per run), so a session nobody resumes
  gives its worker back.

## Durability: dump, load, replay

```python
blob = session.dump(key)               # e.g. while paused, waiting for a human
...                                    # the process restarts
session = AgentSandbox.load(blob, key, tools)
step = session.pending                 # the same ToolCall, if it was paused
session.resume(step, answer)
```

A V8 isolate cannot be serialised in the middle of a run, so `AgentSandbox` does not snapshot the
interpreter the way Monty does. It records a **journal** instead: the code of every run, every
answer a tool call got (a value, or an error's class name), and a hash of every outcome the caller
saw. `load()` starts a fresh worker with the *same* frozen clock and random seed, runs the recorded
code again, answers each tool call with the recorded answer (the real tools are **never** called
during replay), and compares every outcome with the recorded hash.

- **Signed, not encrypted.** The journal is HMAC-SHA256-signed with your key (at least 16 bytes) and
  checked before anything runs: a tampered, truncated or wrongly keyed blob raises `JournalError`
  and no worker is ever started for it. A signed V8 snapshot is not a journal (different header),
  even under the same key. It contains the code and tool results in clear: store it like you would
  store the conversation. Redacted error messages (the default) are not written to it.
- **Bound to an identity.** `dump(key, associated_data=b"tenant-42")` folds the bytes into the
  signature without storing them, and `load(..., associated_data=b"tenant-42")` must be given the same
  bytes, so one tenant's journal cannot be loaded as another's even when they share a key. The journal
  also records the pydeno release and the `redact_host_errors` setting and is refused, before any worker
  starts, if either differs. **A journal alone cannot prevent rollback:** loading an older dump of the
  same session restores the tool budget it had spent since. If that matters, keep a counter in your own
  store and include it in `associated_data`, or use
  [`SessionPool`](advanced/async-agent-sessions.md#sessionpool), which does exactly that.
- **Bounded.** `max_journal_bytes` (default 8 MiB) caps the journal. Past it the session keeps
  working, but `dump()` raises; `load()` refuses a blob larger than its own cap before checking it.
- **Divergence is detected, not prevented.** The clock is frozen and `Math.random` seeded for every
  session (`clock=`, `random_seed=`; the defaults are "now" and a random seed, both recorded), so
  the guest has no ordinary source of nondeterminism. If it still behaves differently on replay
  (a different `RuntimeConfig(bootstrap=...)`, a different pydeno or V8 version, or a guest that
  counts loop iterations against something that varies) the first differing outcome raises
  `ReplayDivergence` and the new session is closed. Pass the same runtime options to `load()` as
  to the original session.
- **Cost.** Loading re-runs everything the session ever ran (minus the tools' own time). A session
  with long computations is slow to restore. Monty's snapshots restore in constant time; this does
  not.
- **After a crash, the last good state.** When a run kills the worker (a crash, a hard timeout,
  a memory kill, `max_pause`), `dump()` still works: it returns the journal as of the last run that
  ended with the worker alive, and `load()` restores exactly that state. The run that died is never
  replayed; it is marked in the journal by a `lost` record (and counted in `session.lost_runs`).
  That record carries the tool calls the lost run made, so a loaded session has spent them too: a
  crash cannot be used to win back tool budget. Those calls did run, with their side effects; the
  restored session simply does not know their answers. A run that failed with a JavaScript error
  or `ResultTooLarge` is a completed run and stays in the journal. Signing, `associated_data`, the
  release check and `max_journal_bytes` apply unchanged.
- **Tools' side effects are not replayed**, which is the point: a restored session does not send
  the email a second time. It also means the journal is the only record of what a tool returned;
  if the outside world changed since, the restored session still sees the old answers.

## Security notes

`AgentSandbox` changes nothing in the [isolation model](advanced/isolation.md); it adds a layer on
top, so every `IsolatedRuntime` limit still applies (and its keyword arguments, such as `config`,
`max_memory` and `sandbox="require"`, are accepted).

- **Tool arguments are untrusted.** `ToolCall.args` and the arguments your tools receive are data
  the guest (and so, the model and whatever text it read) chose. Validate them in the tool, and
  show them to an approver as data, not as instructions.
- **Errors are redacted** by default: the guest learns a failing tool's exception class, not its
  message. Use `redact_host_errors=False` only for tools whose errors carry nothing sensitive.
- **The budget** (`max_tool_calls`) counts every call over the session's life, across `start`,
  `resume` and `run`, and survives `load()` (also of a journal dumped after a crash). A call over
  budget throws a `ToolBudgetError` in the guest and never reaches you.
- **The catalog is enforced in the host.** A catalog tool is reachable only through one hidden
  host function that refuses names that are not in the catalog or not yet found.
- **Console output is a host call.** The session routes `console.*` to the parent to capture it,
  so with `max_host_calls=` set, console calls count against it. Output is capped per run
  (`max_output_bytes`); a `RuntimeConfig(on_console=...)` you pass still sees every call.
- **One session per trust unit.** Everything in a session can see everything else in it. Do not
  share a session, or a journal, between users.
- **Cleanup.** Each session owns one worker process and one thread. `close()` (or the `with` block)
  releases both, also while paused; a crash, a hard timeout or `max_pause` releases them at once;
  a session dropped without `close()` is released by the garbage collector.

## API

```python
AgentSandbox(tools, *, max_tool_calls=None, namespace=None, tools_catalog=None, clock=None,
             random_seed=None, timeout=30.0, max_pause=600.0, max_journal_bytes=8 MiB,
             max_output_bytes=64 KiB, max_result_bytes=1 MiB, **isolated_runtime_options)

session.run(code) -> Any
session.execute(code) -> ExecutionResult
session.start(code) -> ToolCall | Done | Failed
session.resume(step, value) / session.resume(step, error=exc) -> ToolCall | Done | Failed
session.call(step) -> Any                      # run the real tool for a ToolCall
session.pending -> ToolCall | None
session.dump(key, *, associated_data=b"") -> bytes
AgentSandbox.load(blob, key, tools, *, max_journal_bytes=8 MiB, associated_data=b"",
                  tools_catalog=None, **isolated_runtime_options)
session.describe_tools() -> str
session.typescript_stubs() -> str
session.calls_made, session.calls_remaining, session.clock, session.random_seed
session.catalog_names, session.discovered_tools, session.lost_runs
session.close(), session.is_closed()

Done(value, stdout, stderr, truncated) / Failed(error, stdout, stderr, truncated)
    .status, .result, .error_type, .to_result() -> ExecutionResult
ExecutionResult(status, stdout, stderr, result, error, error_type, truncated).to_dict()
SchemaTool(name, description, input_schema, callable, output_schema=None)
IsolatedRuntime(..., capture_console=False).execute(code) / await .execute_async(code)
```

`namespace="tools"` installs the tools as `tools.query_rows(...)` instead of globals; the prompt
helpers follow it.

For an asyncio service, `AsyncAgentSandbox` is this class with coroutine methods and no thread per
session, and `SessionPool` manages many of them per user: see
[Async agent sessions and the session pool](advanced/async-agent-sessions.md).
