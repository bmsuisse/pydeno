# Goose "Code Mode" takeaways for the pydeno agent surface

Status: research notes and issue drafts. No code changed; nothing here is filed on GitHub yet.

Reviewed read-only (shallow clones, no builds, nothing executed):

- goose `540df77` (aaif-goose/goose, main): `crates/goose/src/agents/platform_extensions/code_execution.rs`
  (the whole integration, 1279 lines), `reply_parts.rs`, `prompt_manager.rs`, docs under
  `documentation/docs/guides/managing-tools/code-mode.md` and `documentation/docs/mcp/code-mode-mcp.md`.
- pctx `4db8d99` (portofcontext/pctx, main, crate 0.6.0): the engine goose delegates to. Goose pins
  `pctx_code_mode = "0.5.0"` (`crates/goose/Cargo.toml:255`, optional `code-mode` feature,
  `Cargo.toml:23`), so pctx line numbers below may differ slightly from what goose ships.

Compared with pydeno `future/0.10` (`7921983`): `python/pydeno/_agent.py`, `_front.py`, `_gate.py`,
`_result.py`, `integrations/pydantic_ai.py`, `docs/guides/agent-sessions.md`,
`docs/guides/advanced/async-agent-sessions.md`. The earlier OpenCode notes (PR #114) are not on this
base; where a takeaway overlaps with them it says so.

## What goose Code Mode is

Goose's Code Mode is a platform extension (`code_execution`, off by default,
`platform_extensions/mod.rs:155-173`) that, when enabled, hides every other extension's tools from
the model and exposes three meta-tools instead (`code_execution.rs:501-541`): `list_functions`,
`get_function_details`, `execute_typescript`. The model writes TypeScript
(`async function run() { ... return x; }`); pctx type-checks it, transpiles it, and runs it in a
fresh Deno `JsRuntime` where each extension tool is a typed async function in a namespace
(`Developer.shell(...)`). Calls go back to the goose extension manager through one registry callback
(`create_tool_callback`, `code_execution.rs:389-453`). Three disclosure styles exist
(`CODE_MODE_TOOL_DISCLOSURE`, `code_execution.rs:674-680`): `catalog` (default: list, details,
execute), `filesystem` (`execute_bash` over a virtual file tree of signatures,
`code_execution.rs:542-569, 644-646`) and `sidecar` (the normal tool list stays, with each tool's
output schema appended to its description, and execution only chains calls,
`reply_parts.rs:287-306`).

Facts that shape the comparison (all from the files above):

| Aspect | Goose / pctx | Evidence |
|---|---|---|
| Isolation | In-process V8 (deno_core), one `JsRuntime` per execution, **no OS sandbox, no worker process**. `NoopModuleLoader` refuses `import()`; the only ops are `op_invoke` and timers; no fetch/fs. | `pctx_executor/src/lib.rs:362-370`, `pctx_code_execution_runtime/src/{invoke_ops,timer_ops}.rs` |
| Concurrency | One **process-wide V8 mutex** serialises type check and execution of every session. | `pctx_executor/src/lib.rs:17-27, 134` |
| Time limit | Only the goose extension timeout (default 300 s, `config/extensions.rs:10`), a `tokio::select!` around the run in a `spawn_blocking` thread. | `code_execution.rs:318-387` |
| Memory / heap / output / result limits | **None found** in goose or pctx. Console lines go to unbounded JS arrays; the return value is pretty-printed whole. The only guard is prompt text ("TOKEN USAGE WARNING"). | `runtime.js:47-72`, `pctx_executor/src/lib.rs:451-495`, `descriptions/tools/execute_typescript_catalog/v1.txt` |
| Session state | **None.** "Variables don't persist between executions." Fresh runtime each call; the only cached thing is the generated tool bindings, keyed by an order-independent hash of tool configs. | `execute_typescript_catalog/v1.txt`, `code_execution.rs:139-169, 707-720` |
| Result shape | Success: markdown `Code Executed Successfully: {bool}` + `# Return Value` JSON + `# STDOUT` + `# STDERR`. | `pctx_code_mode/src/model.rs:185-206`, `code_execution.rs:314` |
| Error shape | A failing script is still `CallToolResult::success` (flag only in the text). Host failures (timeout, cancel, parse) are `CallToolResult::error("Error: ...")`. Type errors never run the code and come back as stderr lines `Line L, Column C, TSnnnn: message`. | `code_execution.rs:620-625`, `pctx_executor/src/lib.rs:140-175, 243-280` |
| Tool results into JS | Only text blocks addressed to the assistant, joined with `\n`, JSON-parsed if possible else a string. Images, resources, `structuredContent` and `_meta` are dropped. | `code_execution.rs:455-476`, tests `731-773`, doc note "Text-Only Results" |
| Tool exposure | App-only MCP tools (`_meta.ui.visibility` without `model`) and resource-backed tools are not bindable. `outputSchema` is deliberately **not** passed (`output_schema: None`). | `code_execution.rs:103, 115-121, 872`, `reply_parts.rs:826-840` |
| Discovery | `list_functions` (all functions by namespace), `get_function_details(["Ns.fn", ...])` (batched), a per-turn reminder with the live count. | `code_execution.rs:211-240, 632-672` |
| Approval | `execute_typescript` is annotated non-read-only / destructive / open-world, so the **script as a whole** is one approval decision; nested calls go straight to `dispatch_tool_call`. (Approval state machine not traced to the end; `ops_tool_approval.rs` has no `code_execution` special case.) | `code_execution.rs:528-540, 1190-1223`, `extension_manager/mod.rs:1226-1235` |
| UI aid | The model also fills a `tool_graph` (DAG of intended calls) which the server ignores and only the CLI/desktop render. | `code_execution.rs:42-61, 285-289`, `goose-cli/.../output.rs:898-910`, `ToolCallWithResponse.tsx:413` |

## What pydeno already does (so we do not re-open it)

- **A real sandbox boundary.** One OS-sandboxed worker process per session, hard deadline, RSS
  ceiling, SIGKILL on timeout/cancel (`agent-sessions.md` "Security notes", `async-agent-sessions.md`
  "Cancellation"). Goose runs guest code in-process and serialises all sessions behind one mutex
  (above). A synchronous `while (true) {}` is already covered by tests here
  (`tests/test_aio_agent.py:214,235,297`, `test_agent_sandbox.py:961`).
- **Bounded output and results.** `max_output_bytes` (64 KiB, UTF-8 safe, `truncated` flag) and
  `max_result_bytes` (1 MiB, `ResultTooLarge`, session stays usable) (`_result.py:92-122`,
  `agent-sessions.md` "Results and console output"). Goose has neither.
- **Structured, stable result and error shapes.** `ExecutionResult(status, stdout, stderr, result,
  error, error_type, truncated)` with a host-vs-guest-stable `error_type` and `.ok`, instead of a
  boolean embedded in markdown.
- **Session state, pause/approve per call, durability.** Globals persist across runs;
  `start`/`resume` hands every tool call to the host before it runs; signed journal `dump`/`load`
  by replay; crash recovery with budget accounting. Goose has none of this; its approval is one
  coarse decision per script.
- **A call budget and no leftover calls.** `max_tool_calls` per session, refusal without charge
  for undiscovered tools, settle-before-end for un-awaited calls. Goose has no call cap on nested
  calls.
- **A lazy catalog with host-enforced discovery.** `tools_catalog=` with `search_tools` and
  `describe_tool`; a catalog tool is callable only after the host has shown it to the guest
  (`_agent.py:888-904`, `2903+`). Goose's `list_functions`/`get_function_details` are advisory: once
  the script runs, every bound function is callable whether or not it was "discovered".
- **JSON-Schema (MCP style) tools to TypeScript declarations**, including `$ref`/`$defs`, recursive
  types, unions, tuples (`_schema.py`, `tests/test_agent_schema_tools.py:40-56, 252-275`). The
  goose recursive-schema test (`code_execution.rs:1243-1278`) is already covered.
- **Hybrid native/code split and prompt-cache stability** in the pydantic-ai integration:
  `tool_selector` (the analogue of goose's "first-class extensions stay native",
  `reply_parts.rs:267-285`), `dynamic_catalog` (byte-stable tool definitions), `restart: true`,
  retries, a model-facing guide (`integrations/pydantic_ai.py:183-215, 652-700`).
- **Untrusted-output hygiene and errors redacted by default**, a source gate (`_gate.py`) and the
  per-feed front door (`_front.py`). Goose passes tool error text through unredacted
  (`"Tool error: {e.message}"`, `code_execution.rs:436`).
- **Cancellation reaches async tools.** Pending async tool tasks are cancelled when the worker is
  killed (`async-agent-sessions.md:62-81`); see takeaway 3 for the remaining sync-tool gap.

## Takeaways

Priority: P1 do soon, P2 worthwhile, P3 nice to have. Risk covers API surface, security and test
cost for pydeno.

### 1. A host-observed call trace on `ExecutionResult` (P2, risk low-medium)

**Goose evidence.** Because the host cannot tell the approver or the UI what a script will do,
goose asks the model to declare it: `ExecuteWithToolGraph.tool_graph` (`code_execution.rs:42-61`),
a DAG of tool names and descriptions. The server never reads it (`code_execution.rs:285-289` uses
only `args.input.code`); only the CLI and desktop draw it (`output.rs:898-910`,
`ToolCallWithResponse.tsx:413`). pctx, meanwhile, *records* a true per-call trace
(`ExecutionTrace` with `McpToolCall`/`CallbackInvocation`/`TypeCheck` events with start and end
times, `pctx_executor/src/lib.rs:191-221`), and goose drops it by rendering only `output.markdown()`
(`code_execution.rs:314`). Net: the UI shows what the model *claims* it did, not what happened.

**pydeno today.** The host sees every call as a `ToolCall(name, args, call_id)` and the journal
records them, but `ExecutionResult`/`Done`/`Failed` carry no list of what was called. A host that
wants an audit line, a progress UI or a "what did that script do" summary has to wrap its tools
itself.

**Proposed change.** Add an opt-in, bounded `calls` field to `ExecutionResult`: a tuple of
`(call_id, name, outcome)` where outcome is `"ok"` or the answering error's `error_type`, capped
(for example 64 entries plus a `calls_truncated` flag), with no arguments or values (those can be
large and sensitive; the host already has them at `ToolCall` time). Default empty and left out of
`to_dict()` unless requested (`execute(code, trace_calls=True)`), so existing JSON consumers and
`Done(3) == step` equality are unchanged. Document it as host-observed and therefore trustworthy,
and explicitly decline a model-declared `tool_graph` parameter (see "Not worth copying").

**Risk.** Low: data the session already has at `_answer`/`_observe` time (`_agent.py:1465-1528`).
The cap and the default-off flag are the guards. Make sure it is also bounded across a run that is
lost to a crash (the lost-run journal record already carries the calls).

### 2. An MCP-result-to-guest-value adapter, with visibility filtering (P2, risk low)

**Goose evidence.** The conversion is the most carefully tested part of the file:
`callback_result_to_value` keeps only text blocks whose audience is empty or includes the
assistant, joins them with `\n`, JSON-parses the result and falls back to a string
(`code_execution.rs:455-476`). The tests pin that hidden `structuredContent`, `_meta`, images,
embedded resources and user-audience text never reach the script (`:731-773`). Separately, tools the
MCP Apps spec marks as app-only (`_meta.ui.visibility` lacking `"model"`) are not bound at all
(`code_execution.rs:103`, `reply_parts.rs:826-840`, test `:836-873`). And goose passes
`output_schema: None` (`:120`; asserted `:872`): it parses text, so a declared output type would
promise a shape the adapter does not deliver.

**pydeno today.** `SchemaTool` accepts MCP spellings for the *definition* (`inputSchema`,
`outputSchema`, `_schema.py:409-420`) but nothing converts a `CallToolResult`, so every host
re-implements it. Two traps are easy to fall into: forwarding the whole result object (so `_meta`,
audience-restricted text, and images cross the boundary to the guest), and declaring an
`outputSchema` type while the callable returns text (the stubs then lie to the model; the pydantic-ai
integration already warns for the missing-annotation case, `pydantic_ai.py:174-180`).

**Proposed change.** Pure-Python helpers in `pydeno.tools` (no `mcp` dependency, duck-typed on
mapping/attribute access):

- `mcp_result_value(result, *, prefer="structured")`: use `structuredContent` when the tool
  declared an `outputSchema` (so the declared type is true), otherwise assistant-audience text
  parsed as JSON else string; ignore images/resources/`_meta`; raise `ToolResultError` (a named
  error the guest sees, message redacted per `redact_host_errors`) when `isError` is true.
- `schema_tools_from_mcp(tools, call)`: builds `SchemaTool`s, skips app-only tools, and drops the
  output schema when `prefer` is text. Returns what it skipped so the host can log it.

**Risk.** Low and additive. The security value is the allow-list shape (hidden fields are never
forwarded) and the `isError` mapping, so cover both with goose's cases as golden tests. Keep it out
of `AgentSandbox` itself so pydeno keeps no MCP dependency.

### 3. Cooperative cancellation for tools still running when a run is killed (P3, risk medium)

**Goose evidence.** Goose threads a child `CancellationToken` through every nested dispatch
(`code_execution.rs:258, 295, 335-338`). On timeout or cancellation it cancels that token, gives
nested calls `DISPATCH_DRAIN_TIMEOUT` (500 ms) to stop (for example a long `developer.shell`
killing its child process), then abandons the future (`:361-380`). Four tests pin it
(`:954-1042`). The motivation is stated in the comment at `:325-334`.

**pydeno today.** The worker is killed at once and pending *async* tool tasks are cancelled
(`async-agent-sessions.md:62-81`), so `finally:` blocks in async tools run. A *sync* tool on a thread
"keeps that thread until it returns (threads cannot be interrupted)" (`:83`) and has no way to learn
the run is gone, so a long subprocess or network call started by a tool runs on after its session
died, still holding a tool-thread slot.

**Proposed change.** Optional, opt-in signal for tool authors: a `contextvars`-based accessor
(`pydeno.current_tool_call()` returning an object with `.cancelled: threading.Event` and
`call_id`) set for each call in `_run_tool` (`_agent.py:2356-2386`, which already copies the
caller's context per call; `_TOOL_OF` at `:1702` is the existing precedent). The event is set from
the same place that kills the worker (`_stop_run`, `_mark_dead`). Document that it is advisory:
pydeno still kills the worker and still cannot interrupt the thread.

**Risk.** Medium for a small gain: new public surface, a new lifetime to get right (events must be
set on every death path: timeout, `max_pause`, memory kill, `close()`, cancel), and a contract
that sync tools would have to honour themselves. Do the async-lifecycle tests from PR #114 first;
this builds on them. Not before a user asks.

### 4. One canonical, bounded, model-facing rendering of a result (P3, risk low)

**Goose evidence.** `ExecuteTypescriptOutput::markdown()` (`model.rs:185-206`) is what the model
sees: a `Code Executed Successfully: false` line, a pretty-printed JSON block, `# STDOUT`,
`# STDERR`. Two weaknesses are visible: it is unbounded, and a failed script is returned as a
successful tool call (`code_execution.rs:620-625` maps only host-level errors to `is_error`), so
generic tool-result handling, approval logs and telemetry see success. The *type-check*
diagnostics format is good, though: `Line L, Column C, TSnnnn: message`, with internal file names
stripped (`pctx_executor/src/lib.rs:243-280`).

**pydeno today.** `ExecutionResult.to_dict()` only (`_result.py:119-121`). The pydantic-ai
integration builds its own text for the model; each other host does the same.

**Proposed change.** `ExecutionResult.to_text(max_chars=...)` (name open): status line, then
`error_type: message`, the returned JSON, stdout, stderr and a one-line truncation note, built from
the already-bounded fields and cleaned with the same control-character rules as the front door's
printer. Add a documented `is_error` alias (`not ok`) so adapters can set the protocol-level error
flag correctly. Keep `to_dict()` as is.

**Risk.** Low; mostly a documentation and wording decision. The thing to get right is that the text
is a *rendering*, never parsed back.

### 5. Make the catalog enumerable and cheaper to read (P3, risk low)

**Goose evidence.** The model can get the whole function list grouped by namespace
(`handle_list_functions`, `code_execution.rs:211-221`), fetch details for several functions in one
call (`GetFunctionDetailsInput.functions`, `:224-240`), and gets a fresh per-turn reminder with the
live count and "do not call callback function names directly as tools"
(`get_moim`/`catalog_disclosure_moim`, `:632-672`; the test at `:1157-1165` pins that the reminder
leaks no function names).

**pydeno today.** `search_tools(query, limit)` returns at most 50 results with no offset or total,
and no namespace; `describe_tool(name)` takes one name and every call is charged to
`max_tool_calls` (`_agent.py:2903+`, `agent-sessions.md` "A lazy tool catalog"). The guide text
(`_CATALOG_GUIDE`, `_agent.py:649-654`) does not say how large the catalog is, so a model cannot tell
whether to enumerate (small catalog) or search (large). With a catalog of 300 tools there is no way
to list everything even deliberately.

**Proposed change.** Three small, backward-compatible additions: (a) `describe_tool` also accepts a
list of names and returns a list, charged as one call; (b) the guide string and
`describe_tools()` state the catalog size (`"The catalog holds N tools."`, a number, never names);
(c) `search_tools` results gain an optional `namespace` key when the catalog was built from
namespaced names, and an `offset` argument. Do not change the existing result shape.

**Risk.** Low. (a) changes the budget accounting for a rare shape, so state it in the docs and test
it; (b) alters prompt text (goldens). Overlaps with the OpenCode "fair bounded catalog" draft in
PR #114: if that lands, do (b) and (c) together with it rather than twice.

### 6. Treat prompt-cache stability as a first-class property of tool listings (P3, risk low)

**Goose evidence.** `prepare_inference_tools` sorts tools by name, with the explicit comment that
stable ordering matters for prompt caching across sessions (`reply_parts.rs:309-312`), and the code
bindings are cached under an order-independent hash so a reorder does not rebuild them
(`code_execution.rs:139-169, 707-720`).

**pydeno today.** `describe_tools()` and `typescript_stubs()` follow insertion order of the mapping,
so a host that builds the mapping from a dynamic MCP listing gets a different prompt for the same
tool set. The pydantic-ai `dynamic_catalog` covers the "tools change mid-run" case, not the "same
tools, different order" case.

**Proposed change.** `describe_tools(..., sort=False)` / `typescript_stubs(..., sort=False)` and the
`AgentSandbox` methods; `sort=True` orders by name. Fold into PR #114's catalog helper (which already
sorts namespaces) if that is still open.

**Risk.** Very low; opt-in, so existing goldens stay.

### 7. Test matrix to borrow (P2, risk low)

Goose's tests encode failure modes that are cheap to reproduce here:

- **A hung execution must not wedge anything else** (`code_execution.rs:1044-1094`: a script that
  never resolves times out, and a normal script then runs). pydeno's process-per-session design
  makes this structurally true, but nothing asserts it across sessions of one `Pydeno` pool or
  `SessionPool`: kill session A on a hard timeout and run session B within a bounded time, also
  under `max_tool_threads` pressure. The existing sync-loop tests are single-session.
- **Callback completing from another loop does not hang** (`:1096-1154`): a tool that hands work to a
  different event loop or thread and awaits the result. Add the same shape to the async agent tests.
- **Nothing hidden crosses** (`:731-773`): once takeaway 2 exists, port these cases verbatim.

## Not worth copying

- **Model-declared `tool_graph`.** It costs output tokens on every call, is unverifiable (the server
  ignores it), and shows the approver the model's claim rather than the call. pydeno's per-call pause
  (`start`/`resume`) shows the real call with real arguments at the moment of decision; takeaway 1
  covers the UI need with host-observed data.
- **In-process V8 and a global execution mutex.** Goose's design trades safety and concurrency for
  startup cost. pydeno's process-per-session sandbox is the product; do not weaken it for latency.
- **Script-level approval via tool annotations** (`read_only: false, destructive: true`,
  `code_execution.rs:528-540`). Coarser than what `ToolCall` pausing already gives, and nested calls
  then skip any per-call policy.
- **TypeScript as the guest language with an in-process type checker.** pctx ships a TypeScript
  type-check runtime and a snapshot to catch wrong argument shapes before any tool runs
  (`pctx_executor/src/lib.rs:136-175`). The feedback loop is genuinely good, but pydeno guests are
  plain JavaScript, and bundling `tsc` (and its `lib.d.ts` set) into the wheel is a large cost for a
  library whose pitch is a small attack surface. The cheap route already exists: `typescript_stubs()`
  plus a host `gate=` (`_gate.py`, `Verdict.reason` is returned to the caller) that runs the host's
  own `tsc --checkJs` is a documentation recipe, not a feature. Worth one paragraph in
  `docs/guides/gate.md` if anyone asks.
- **Passing nested-tool text through unredacted** (`"Tool error: {e.message}"`,
  `code_execution.rs:436`). pydeno redacts by default on purpose.
- **Text-only tool results with images and resources silently dropped** as a documented limitation
  ("Text-Only Results" in `code-mode.md`). Take the allow-list idea (takeaway 2), not the lossy
  default; a pydeno host that wants binary data returns it as bytes/base64 on purpose.
- **A whole-catalog build that fails if any single tool fails** (`CodeModeState::new`,
  `code_execution.rs:688-704`: one bad callback config returns an error for every tool). pydeno
  already rejects bad names at construction with a precise error; nothing to adopt.
- **Stateless executions.** Goose tells the model "Variables don't persist between executions";
  pydeno's persisted globals plus journal are a deliberate advantage.

## Suggested order

1. Takeaway 7 tests (no API change; protects what pydeno already claims).
2. Takeaway 2 (MCP adapter) and takeaway 4 (rendering), which every MCP host currently rewrites.
3. Takeaway 1 (call trace), then 5 and 6 alongside the PR #114 catalog work.
4. Takeaway 3 only on request.

## Verification notes

Everything above is from reading source and docs at the commits named at the top; none of it was
built or run. Two statements are inferences and are marked as such where they appear: that a
synchronous infinite loop inside goose's guest cannot be interrupted by its `tokio::select!` timeout
(the select runs on the same single-thread runtime that the V8 loop blocks; goose's own hung-script
test uses a never-resolving promise, not a busy loop, `code_execution.rs:1048-1069`), and that nested
calls get no per-call approval (no `code_execution` handling found in `ops_tool_approval.rs`).
Neither is relied on for any proposal.
