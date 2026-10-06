# TrueForge takeaways for the pydeno agent surface

Source: <https://github.com/truefoundry/trueforge>, `main` at
[`a82cd2b`](https://github.com/truefoundry/trueforge/tree/a82cd2bbad2a6aafa2dd7f82580b2a12c6ec5b83)
(read 2026-10-06 from a tarball; line numbers refer to that tree, paths are relative to the
repository root). Compared with `python/pydeno/_agent.py`, `_front.py`, `_gate.py` and
`docs/guides/agent-sessions.md` on `future/0.10`. No code was changed; every item below is a
proposal.

## What TrueForge is, and how much of it applies

TrueForge is a TypeScript (Node >= 22) agent harness: model loop, MCP tools, skills, approvals, a
chat UI and an HTTP SDK (`README.md`). It contains **no V8, Deno, WASM or in-process JavaScript
engine** (a search for `wasm|isolated-vm|quickjs|deno|v8` finds only unrelated hits). Its "Code Mode"
is *Python written by the model and run as an OS process* in a sandbox (Daytona, a remote TFY
sandbox, or a local bubblewrap/seatbelt wrapper from `@anthropic-ai/sandbox-runtime`), which calls
MCP tools back through a bridge. So the overlap with pydeno is the **agent surface around the
sandbox**, not the isolation technology, and the isolation is a weaker design than pydeno's (see
"Not worth copying"). The takeaways are therefore small, and mostly P2/P3.

Relevant TrueForge files (all under the repository root):

| Area | Files |
|---|---|
| Host-side bridge policy | `packages/trueforge-core/src/core/sandbox/codeMode/CodeModeDispatcher.ts`, `types.ts` |
| Guest-side client | `packages/trueforge-core/src/core/sandbox/scripts/mcp_client.py` |
| Sandbox glue, timeouts, env | `packages/trueforge-core/src/core/sandbox/Sandbox.ts` |
| Local sandbox | `packages/trueforge/src/sandbox/local/core/hostRun.ts`, `core/CodeModeUdsTransport.ts`, `core/frame.ts`, `provider/LocalSandboxProvider.ts` |
| Tool policy / approval | `packages/trueforge-core/src/core/mcp/ToolSet.ts` |
| Large results | `packages/trueforge-core/src/core/capabilities/builtins/LargeToolResponse.ts`, `docs/key-features/large-tool-responses.mdx` |
| Model-facing contract | `docs/key-features/code-mode.mdx` |
| Repo rules / CI / release | `AGENTS.md`, `RELEASING.md`, `SECURITY.md`, `.github/workflows/*` |

## Takeaways

Priority: P1 do soon, P2 worth a ticket, P3 note or doc only. Risk is the risk of *making the
change* in pydeno (API surface, journal compatibility, security).

### 1. Say who is at fault in a failed result (P2, risk low)

**Evidence.** The bridge reply carries `source: 'internal' | 'caller' | 'transport'`
(`codeMode/types.ts:164-177`), assigned in `classifyErrorSource`
(`CodeModeDispatcher.ts:32-43`: a 4xx or an unknown server/tool is `caller`, a closed dispatcher is
`transport`, the rest `internal`). The guest client turns it into three different messages
(`scripts/mcp_client.py:187-193`), so the model is told "your request was invalid" apart from "the
platform broke".

**pydeno today.** `ExecutionResult.error_type` is already stable and already refuses a guest that
claims a host-side name (`docs/guides/agent-sessions.md`, "Stable `error_type`"). But a harness that
wants to decide "retry with the model" vs "alert an operator" has to keep its own table of names
(`TypeError` vs `WorkerCrashed` vs `ToolBudgetError` vs a tool's own exception class).

**Proposal.** Add an additive, derived `ExecutionResult.source` (`"guest"`, `"tool"`, `"limit"`,
`"host"`, or `None` on success), computed from the same classification that already guards
`error_type`, and include it in `to_dict()`. It must not enter the journal outcome hash (existing
journals keep loading) and must never be guest-supplied. Document the mapping under "Results and
console output". **Risk:** a new `to_dict()` key breaks exact-equality goldens in user code; ship it in a
minor release and say so in the changelog.

### 2. Annotation-driven approval in the pause loop (P2, risk low-medium)

**Evidence.** TrueForge decides approval from MCP tool annotations: `ToolSet.callTool` returns
`approvalRequired` unless the tool is allowed and has an applicable policy
(`ToolSet.ts:101-114`, `142-158`). Approval policies carry an expiry and stale ones are dropped
(`ToolSet.ts:52-59`). In Code Mode a destructive tool is refused outright, and the model is told to call
it directly so it reaches the approval flow (`mcp_client.py:224-245`).

**pydeno today.** The pause loop (`start`/`resume`, "Pausing at tool calls") is strictly better than
refusing: code can stop at any tool call and resume after approval. But `ToolCall` has only `name`,
`args`, `call_id` (`python/pydeno/_agent.py:160-171`) and `SchemaTool` has no field for MCP
`annotations` (`python/pydeno/_schema.py:408-427`), so every driver reinvents "which tools need a
human".

**Proposal.** Let `SchemaTool` (and the mapping spelling) carry an optional `annotations` mapping
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) and expose it read-only on
`ToolCall` (for example `step.annotations`). No behaviour change: pydeno still never decides approval,
the driver does. **Risk:** `ToolCall` is a public frozen dataclass; add the field with `compare=False`
like `_session`, so equality is unchanged. Annotations are hints from a possibly untrusted MCP
server, never a boundary (say so in the docs). Do not copy TrueForge's default: `_is_destructive`
returns `False` for a tool with **no** annotations (`mcp_client.py:224-227`), i.e. unknown means safe.
Any pydeno helper should treat unknown as "ask", and the docs should recommend that.

### 3. Do not drop an oversized result on the floor: bounded preview (P2, risk medium)

**Evidence.** Past a per-response token threshold the full tool result is written to a sandbox file and
replaced in context by a head/tail preview plus the path; when parallel results exceed a combined
threshold, the largest go first (`LargeToolResponse.ts:13-15`, `40-45`, `89-107`, `183-200`; user
explanation in `docs/key-features/large-tool-responses.mdx`). The model can then `grep` or parse the
file. Failure messages are cut to 500 characters (lines 16, 128).

**pydeno today.** A result over `max_result_bytes` becomes `Failed` with `error_type="ResultTooLarge"`
and `result=None` (`agent-sessions.md`, "Bounded result"); the session stays usable. That is correct and
bounded, but the model learns only "too large" and must re-run the computation blind.

**Proposal.** Keep failing, but give `ResultTooLarge` a short, cleaned head/tail text preview and the
byte size in the error message. Not a storage mechanism: the guest has no filesystem and pydeno should
not grow one. A host that wants offload can do it with tools (document a recipe: a `save_result` tool
plus a `read_result(handle, offset, n)` tool). The combined-threshold idea does not apply, because
pydeno tool results go to the guest, not into the model's context. **Risk (why medium):** a preview
means the worker serialises a bounded prefix of a value it was told not to send, so the cap must be
applied inside the worker; the preview must go through the same control-character cleaning as
`stdout`; and it must stay out of the journal outcome hash (the cap decides the outcome, the preview is
cosmetic). Prototype only if the worker-side bound is cheap; otherwise ship just the docs recipe.

### 4. A per-tool-call host deadline (P2, risk medium)

**Evidence.** The bridge wait is derived, not guessed: MCP request timeout + connect timeout + a 30 s
buffer, so the outer wait always outlives the inner one (`Sandbox.ts:43`, `247-248`), pinned by
`packages/trueforge-core/tests/core/sandbox/sandboxBridgeTimeout.test.ts`. Each bridged request has its
own timeout (`mcp_client.py:145`, `170-182`).

**pydeno today.** `timeout=` is the guest's own running time and excludes time paused at a tool call;
`max_pause=` (600 s) bounds a whole paused run. A search for `tool_timeout`/`call_timeout`/`per call` in
`_agent.py`, `_aio_agent.py` and the two agent guides finds no per-call bound, so one hung tool uses the
whole `max_pause` and then costs the session its worker.

**Proposal.** `tool_timeout=` (seconds, default `None` = today's behaviour) on `AgentSandbox` and
`AsyncAgentSandbox`. On expiry the guest sees an Error named `ToolTimeoutError` (recorded in the journal
like any tool error, so replay stays deterministic) and the run continues. Async tools: `asyncio.wait_for`.
Sync tools cannot be killed: answer the guest on time, discard the late result, and keep counting the
thread against `max_tool_threads` until it returns (document this plainly). **Risk:** the sync case holds
a busy thread; reuse the existing thread budget (`_front.py:1097`, `ToolThreadLimitError`) so it stays
bounded. Needs a failing-first test per mode.

### 5. Run the same behaviour suite against every twin or backend (P2, risk low)

**Evidence.** TrueForge pins interchangeable backends with shared contract suites that each
implementation only binds: the session store ("shared behavior MUST live in `storeContractSuite.ts`",
`packages/trueforge-core/src/agent-session/store/AGENTS.md:1-3`), the sandbox provider
(`tests/core/sandbox/provider/sandboxProviderContractSuite.ts`) and the Code Mode transport
(`tests/core/sandbox/codeMode/codeModeTransportContractSuite.ts:36-93`: "start succeeds and is
idempotent", "stop is safe after start and before start", "closed dispatcher returns source transport").
The rule that an interface change must update every implementation and the suite in the same change is
written down.

**pydeno today.** `AgentSandbox` and `AsyncAgentSandbox` are twins with separate test files
(`tests/test_agent_sandbox.py` vs `tests/test_aio_agent.py`, `tests/test_agent_execution_result.py` vs
`tests/test_aio_agent_results.py`). I did not audit them for drift, so this is a hygiene proposal, not a
bug report.

**Proposal.** One parametrised behaviour suite for the user-visible semantics both classes promise
(budget accounting, `ResultTooLarge`, crash-then-`dump`, closed-session dispatch, catalog discovery),
run once per class, plus a line in `CLAUDE.md` that a behaviour change to one twin lands in the shared
suite. **Risk:** test churn only.

### 6. Make trace context follow the tool call (P3, docs and a test only)

**Evidence.** TrueForge snapshots the W3C trace context into the sandbox env per exec and re-extracts it
on every bridged request, so tool spans nest under the exec span (`Sandbox.ts:31-46`,
`CodeModeDispatcher.ts:45-48`, `78-85`).

**pydeno today.** Tools run under a copy of the caller's `contextvars` (`_agent.py:1689`;
`docs/guides/advanced/async.md:76`), the in-process equivalent: a span active when `run()` was called is
the parent inside the tool. OpenTelemetry instrumentation is listed as not provided
(`docs/guides/quickstart-pydeno.md:103`).

**Proposal.** No code. Add a short recipe to `agent-sessions.md` (a tool reading the current span) and a
test that a `ContextVar` set before `run()` is visible in a sync and an async tool, so the behaviour is a
promise and not an accident. **Risk:** none.

## What pydeno already does (no action)

- **Isolation of the guest.** A V8 isolate in a worker with `env={}`
  (`python/pydeno/_isolated.py:1903`), a 16 MiB frame cap (`python/pydeno/_wire.py:43`) and a start-up
  self-test of the OS sandbox, instead of a shell plus Python behind a path allow-list.
- **Tool boundary enforced on the host.** TrueForge enforces allow-listing and approval in
  `ToolSet.callTool`, and the dispatcher refuses an `approvalRequired` reply
  (`CodeModeDispatcher.ts:137-141`); the guest-side `_check_tool_allowed` is only an early, friendly
  failure. pydeno's catalog check is host-side by construction (`agent-sessions.md`, "The catalog is
  enforced in the host").
- **Lazy tool catalog.** TrueForge's deferred tool loading and `get_tool_output_schema` correspond to
  `tools_catalog=`, `search_tools` and `describe_tool` (which returns the output schema).
- **Bounded console and results.** `max_output_bytes`, `max_result_bytes`, truncation markers and
  control-character cleaning. TrueForge kills the whole process group when buffered output passes 14 MiB
  (`hostRun.ts:31`, `581-587`); pydeno stops formatting and the session lives on.
- **Secrets stay in the harness.** Same principle; pydeno's worker also starts with an empty
  environment, where TrueForge builds a curated one (`hostRun.ts:301-324`).
- **Never retry a call that may have run.** The guest client retries only connect failures and
  `NoRespondersError`, never a timeout, "so a non-idempotent tool can't double-execute"
  (`mcp_client.py:132-140`, `176-182`). pydeno's journal is the same and stricter: calls of a lost run
  stay spent and are never answered again (`agent-sessions.md`, "After a crash").
- **Support probe at start-up.** `LocalSandboxProvider.isSupported` returns `{supported, reason,
  attempts}` (`LocalSandboxProvider.ts:148-160`, `255-417`); pydeno's `sandbox_status()` does this and
  also verifies SIGKILL authority and resource readability (`python/pydeno/_status.py`).
- **Session state and durability.** TrueForge persists session/turn events in SQLite or Postgres with a
  persist-before-mutate rule (`packages/trueforge-core/src/core/runtime/AGENTS.md`). pydeno's signed,
  replayable, rollback-aware journal and `SessionPool` solve a harder problem (restoring interpreter
  state); nothing to adopt.
- **Source gate.** `_gate.py` is a fail-closed host-side check of the exact source; TrueForge has no
  equivalent (the model's script is confined, never inspected).

## Not worth copying

- **Policy in the untrusted process.** `mcp_client.py` runs in the sandbox and carries the allow-list and
  the destructive check; a hostile script can bypass it (it holds only because the host re-checks).
  pydeno should keep every decision in the host.
- **Network egress for the guest.** The local sandbox allows `pypi.org`, `github.com` and wildcards of
  both (`hostRun.ts:85-99`) so `pip` and `git clone` work, and the Linux policy sets
  `allowAllUnixSockets: true` (`hostRun.ts:364-369`). pydeno's guest has no network and no filesystem;
  network is the allow-listed `http_fetch` tool. Do not trade that for convenience.
- **Process-based sandbox and one-shot sockets.** Bubblewrap/seatbelt around a shell, `denyRead: ['/']`
  plus a hand-maintained per-platform allow-read list (`hostRun.ts:233-299`), a 0700 socket directory
  limited to 65 bytes, one JSON request per UDS connection with a 64 MiB cap
  (`CodeModeUdsTransport.ts:27-33`, `frame.ts:314`). Complexity pydeno avoids by owning the runtime.
- **Offloading to a sandbox file.** Needs a writable guest filesystem; pydeno deliberately has none (see
  takeaway 3 for the bounded alternative).
- **Repo process.** Patch-only changesets, a `CLAUDE.md` mirror per `AGENTS.md`, `snake_case` wire rules
  and a Windows `npx` smoke gate belong to a multi-package npm monorepo. pydeno's changelog and
  release-artifact gates already serve this role. The one transferable habit, "write the rule next to the
  code and let CI enforce it", is already how `CLAUDE.md` works here.
- **Fern-generated SDK trees** (`packages/trueforge-sdk`, `python/trueforge_sdk`). Irrelevant.

## Suggested order

1. Takeaways 5 and 6 (shared test suite, docs recipe plus `ContextVar` test): no API change.
2. Takeaways 1 and 2 (additive fields): one small release, one changelog entry.
3. Takeaway 4 (`tool_timeout=`) after a short design note on the sync-tool thread case.
4. Takeaway 3 only if the worker can produce a bounded preview cheaply; otherwise the docs recipe.

None of these is a security fix. TrueForge has no finding that changes pydeno's threat model.
