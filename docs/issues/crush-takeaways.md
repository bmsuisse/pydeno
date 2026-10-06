# Crush takeaways for pydeno's agent surface

[charmbracelet/crush](https://github.com/charmbracelet/crush) is a Go terminal coding agent. It
runs shell commands for a model, so its problems overlap with pydeno's: what a model may run, who
approves it, how much output comes back, what happens to long jobs, and how state outlives a
turn. This note records what was read, what is worth adopting, and what is not.

All evidence is pinned to crush commit `65865e01950368379ad1f9746b620a77fb7a5da8` (shallow clone,
read only; nothing was executed). Line references are `path:lines` with a permalink in the
[reference list](#crush-references). pydeno references are `path:lines` on `future/0.10`.
Where a claim comes from reading code rather than running it, the text says so.

## How the two differ (read this first)

Crush runs the model's shell commands **on the user's machine, in the user's session**, with the
user watching a TUI. Its safety story is a prompt-time permission question plus an advisory
command blocklist. There is no OS confinement: commands inherit the full process environment
([shell.go:92-100][shell-env]) and the blocklist is matched against parsed command words
([bash.go:76-147][bash-banned], [bash.go:166-196][bash-block]).

pydeno runs the model's JavaScript in a **sandboxed worker with no ambient authority**; the only
way out is a tool the host bound, and the host sees every call as data (`ToolCall(name, args,
call_id)`). So most of crush's machinery (command parsing, blocklists, process groups, a shell
interpreter) answers questions pydeno does not have. The overlap that matters is the
*agent-facing protocol*: tool prompts, approval, output bounds, job handles, hooks, and tests.

## Summary

| # | Takeaway | Priority | Risk |
| --- | --- | --- | --- |
| 1 | Per-call policy hook for `run()` / `execute()` (deny wins, fail closed) | P1 | Medium |
| 2 | Opt-in head+tail console retention with a count of what was dropped | P2 | Low |
| 3 | Optional "your limits" block in `describe_tools()` | P2 | Low |
| 4 | A denial that ends the run, not only the call | P2 | Medium |
| 5 | Opt-in guard against identical repeated tool calls | P3 | Low |
| 6 | Job-handle recipe for long tools (docs first, helper later) | P3 | Low |
| 7 | CI hygiene: SHA-pinned actions, `persist-credentials: false` | P3 | Low |

Priorities: P1 worth an issue and a milestone slot, P2 worth an issue, P3 backlog.

## What pydeno already does (so nothing to take)

- **Hard bounds at capture, not after.** `OutputCapture` stops formatting past `max_output_bytes`
  (`python/pydeno/_result.py:185`, marker at `:46`); crush keeps the whole stream in memory in an
  unbounded `syncBuffer` and truncates when the call returns
  ([background.go:22-44][bg-buffer], [bash.go:395-396][bash-format]). A chatty or endless
  background command grows crush's memory; it cannot grow pydeno's.
- **Byte budgets, not display cells.** Crush measures its 30000-"character" cap with
  `ansi.StringWidth` ([bash.go:431-438][bash-truncate]), which counts CJK as two cells (its own
  test says so, `bash_test.go:188-197`). pydeno caps UTF-8 bytes and never splits a character
  (`docs/guides/agent-sessions.md`, "Bounded output"), which is the property a transport or
  storage limit needs.
- **Stable machine-readable errors.** `ExecutionResult.error_type` is a documented, closed-ish
  vocabulary (`ToolBudgetError`, `ResultTooLarge`, `RuntimeTimeout`, `WorkerCrashed`) and a guest
  cannot claim a host-side name. Crush's tool errors are free text; the only structured parts are
  sentinel Go errors ([errors.go:5-10][agent-errors]) and a `StopTurn` flag.
- **Redacted tool errors.** pydeno shows the guest the exception class, not the message
  (`redact_host_errors`). Crush returns `err.Error()` of an MCP call straight to the model
  ([mcp-tools.go:135-138][mcp-run]).
- **Fail-closed policy.** `GateUnavailable` (`python/pydeno/_gate.py:167`) denies when a gate is
  late or raises. Crush hooks fail **open** (takeaway 1, and "not worth copying").
- **A tool budget with a typed error.** `max_tool_calls` and `ToolBudgetError`
  (`python/pydeno/_agent.py:880`, journaled, survives `load()`). Crush has no call budget, only a
  heuristic loop stop (takeaway 5).
- **Time limits that distinguish waiting from running.** `timeout` counts guest time only,
  `max_pause` bounds waiting on the host, and each limit has its own error text
  (`python/pydeno/_errors.py:114`). Crush's nearest idea, an idle timeout for model streams with
  a message saying how to change it ([request_timeout.go:12-45][req-timeout]), is about its LLM
  client, which pydeno does not have. Crush's bash tool has no hard timeout at all, only an
  auto-background threshold ([bash.go:315-385][bash-wait]).
- **Concurrent use refused cleanly.** `_exclusive` (`python/pydeno/_front.py:1468`) raises
  rather than queueing. Crush queues prompts behind a busy session with accept sequences and
  cancel marks ([agent.go:395-417][queue], [run_marker.go][run-marker]); see "not worth copying".
- **Durable state.** Sealed journals and deterministic replay (`dump` / `load`) plus `SessionPool`
  size caps. Crush persists sessions in SQLite ([session.go:57-70][session]) and summarizes when
  the model's context fills ([agent.go:1095-1117][stop-when]); pydeno has no model context to
  manage.

## Takeaways

### 1. Per-call policy hook for `run()` / `execute()`

**Evidence.** Crush wraps every tool in a `hookedTool` that runs user hooks before the tool and
before the permission prompt ([hooked_tool.go:54-100][hooked]). Several hooks run in parallel and
are deduplicated by command ([runner.go:96-120][hook-run]). Results aggregate deterministically:
deny beats allow beats no opinion, a halt is sticky, reasons concatenate in config order
([hooks.go:94-158][hook-agg]). An explicit allow pre-approves the permission prompt, but the
approval is bound to the tool-call ID, so it cannot be replayed by a later call sharing a context
([permission.go:16-36][perm-hook], [permission.go:196-202][perm-hook-use]). A hook may also rewrite
arguments with a shallow merge ([hooks.go:160-189][hook-merge]).

**pydeno today.** Source-level gates (`python/pydeno/_gate.py:73`, `all_of` / `any_of` at
`:879-887`) decide whether code may *run*. Per tool call, the only policy point is the
`start()` / `resume()` loop (`docs/guides/agent-sessions.md`, "Pausing at tool calls"), or
wrapping each tool function by hand like the `per_tool_budget` example. `run()` and `execute()`,
the simple entry points most hosts use, have no per-call decision point.

**Proposal.** Add an optional `before_call=` (sync or async, like gates) accepted by
`AgentSandbox`, `AsyncAgentSandbox`, and the front door. It receives `(ToolCall, context)` and
returns the existing `Verdict` shape (allow, or deny with a reason). Combine several with the
existing `all_of`: any deny wins. A denied call throws in the guest as an Error whose `name` is
the new error class and never reaches the tool or the budget. Reuse the gate's timeout and
fail-closed behavior (`GateUnavailable` becomes a denied call, not a pass). Journal the denial as
the call's recorded outcome so replay stays deterministic. Leave argument rewriting out of the
first version: a rewritten argument would have to be journaled and digested exactly like the
original (`python/pydeno/_agent.py:352`), and it makes "what did the model ask for" ambiguous in
audit logs.

**Priority** P1. It closes the largest gap between the simple API and the safe one, and nearly
all of the machinery (`Verdict`, timeouts, combinators) exists. **Risk** Medium: replay and
journal semantics for denied calls need tests (denied call, then `dump` / `load` / replay), and
the sync path needs the same thread-budget care as sync gates.

### 2. Opt-in head+tail console retention, with a count of what was dropped

**Evidence.** On overflow crush keeps the first and last halves and says how much it dropped:
`... [N lines truncated, full output: PATH] ...` ([bash.go:431-448][bash-truncate]). The
persisted variant spends a line and byte budget from both ends so a short but very wide tail
survives ([truncate.go:91-132][trunc-head-tail]), and clips a single oversized line on a rune
boundary ([truncate.go:225-248][trunc-clip]). The reason is practical: for build or test output,
the failure is almost always at the end.

**pydeno today.** A prefix is kept; later calls are dropped unformatted and one `[truncated]`
line is appended (`python/pydeno/_result.py:46,185`). The guest or model cannot tell whether one
line or ten thousand were lost, and an error summary printed last is the first thing to go.

**Proposal.** An opt-in `output_tail_bytes=` (default 0, current behavior). The capture keeps
the first `max_output_bytes - output_tail_bytes` bytes and a ring of the last
`output_tail_bytes`, joined by a marker such as `[truncated: N calls, M bytes omitted]`. Counting
dropped calls needs only an integer increment, so the existing "never format after the cap"
guarantee holds. Keep `ExecutionResult.truncated` a boolean and add the counts as optional
fields only if hosts ask. Do **not** copy crush's spill-to-file: the guest has no filesystem and
a host that wants the full stream already has `print_callback` and the uncapped
`RuntimeConfig(on_console=...)`; document that as the recovery path instead.

**Priority** P2. **Risk** Low: opt-in, no change to the wire or journal, but it adds a second
retention mode to test (exact-cap boundaries, multi-byte characters straddling the head/tail
seam, stderr budget independence, as the OpenCode output-boundaries note already requires).

### 3. Optional "your limits" block in `describe_tools()`

**Evidence.** Crush's bash description is a template that injects its real limits and the
recovery path: `Truncate if exceeds {{ .MaxOutputLength }} characters. The truncation marker
names a file ... read it with the view tool` ([bash.md.tpl:11-14][bash-tpl],
[bash.go:149-164][bash-desc]), and lists the banned commands so the model does not try them.
Tools are also sorted by name before they are handed to the model, so the prompt is stable
([coordinator.go:953-955][sort-tools]).

**pydeno today.** `_PREAMBLE` (`python/pydeno/_agent.py:605-612`) explains the execution model
but not the numbers: the model is not told the stdout cap, the result cap, the remaining tool
budget, or that a thrown `ToolBudgetError` is final for the session. It learns by failing
(`ResultTooLarge` costs a run).

**Proposal.** `describe_tools(include_limits=True)` on the session (not the module function,
which has no configuration) appends two or three sentences from the session's actual settings:
`max_output_bytes`, `max_result_bytes`, `calls_remaining`, and "return less data rather than
more". Default stays off so existing golden descriptions are unchanged (the OpenCode catalog work
keeps the same constraint). Keep it stable: no timestamps, and the remaining budget only when the
caller asks, since a changing prompt defeats prompt caching.

**Priority** P2. **Risk** Low: additive and opt-in. Cost is one more place that must track
limit defaults; derive the text from the same constants the checks use.

### 4. A denial that ends the run, not only the call

**Evidence.** In crush a permission denial returns an error response with `StopTurn = true`, so
the model is not asked again ([tools.go:72-78][perm-denied]); the agent loop treats a stop-turn
result as the end of the turn ([agent.go:1064-1075][finish-stop]). A hook can additionally
*halt* (exit code 49, chosen to avoid the generic-error, sysexits and signal ranges) as opposed
to merely denying one call ([hooks.go:18-22][hook-halt], [hooked_tool.go:62-73][hooked-halt]).

**pydeno today.** `resume(step, error=PermissionError("denied"))` makes the call throw a
catchable Error. Guest code can catch it and call the same tool again, which the host must then
refuse again, and each attempt spends budget. A model that retries a denied call can turn an
approval loop into a long conversation.

**Proposal.** Provide a `ToolDenied` exception (name and docs only at first) whose recommended
handling in `run()` is: end the run as `Failed` with `error_type == "ToolDenied"`, keep the
session usable, and charge the denied call once. `resume(step, error=ToolDenied(...))` in the
manual loop keeps today's catchable behavior unless the host passes `final=True`. Document that
"deny and let the model adapt" and "deny and stop" are both valid and name the option for each.

**Priority** P2. **Risk** Medium: making a run end from the host means the guest's `try/catch`
no longer sees the error, which is a semantic change and must be journaled so replay ends the
same run at the same call. Ship behind the explicit option.

### 5. Opt-in guard against identical repeated tool calls

**Evidence.** Crush stops a turn when, in the last 10 steps, one `(tool, input, output)`
signature appears more than 5 times ([loop_detection.go:11-39][loop], wired as a stop
condition at [agent.go:1117-1119][loop-wire]); the signature is a SHA-256 over the call and its
result ([loop_detection.go:45-71][loop-sig]).

**pydeno today.** Only the global `max_tool_calls` budget bounds a model that polls the same
tool forever, and a legitimately large budget (default 1000) means a stuck model burns most of
it before anything intervenes. The canonical argument digest needed for a signature already
exists for journals (`python/pydeno/_agent.py:352`).

**Proposal.** An opt-in `max_identical_calls=` (default off): the Nth identical `(name, args
digest)` call within a session throws a typed `ToolRepeatError`, recorded in the journal like
`ToolBudgetError`. Use arguments only, not results: the result is not known at call time, and
including it would mean checking after the fact. Off by default because legitimate polling
(`job_output`, takeaway 6) repeats identical calls on purpose.

**Priority** P3. **Risk** Low: opt-in; needs a clear message so a model knows to change its
approach rather than retry.

### 6. Job-handle recipe for long tools

**Evidence.** Crush auto-moves a command that runs past 60 s into a background job and returns
an ID ([bash.go:54,315-385][bash-wait]); the model polls with `job_output` (optionally blocking
with `wait`, [job_output.go:48-90][job-output]) and stops it with `job_kill`. Jobs are capped at
50 ([background.go:16][bg-cap]), completed jobs expire after 8 hours
([background.go:19,174-192][bg-expire]), `Kill` waits for the job to exit
([background.go:147-156][bg-kill]), and the manager is killed with a bounded wait at shutdown
([app.go:926][app-killall], [background.go:194-211][bg-killall]). A fast failure is detected
before returning a job ID, by sleeping one second ([bash.go:260-262][bash-fast-fail]).

**pydeno today.** A long tool blocks its `await`, and `max_pause` (default 600 s per run) is the
only ceiling. There is no documented pattern for work that outlives one run, and the guest
cannot sleep (virtual time), so polling costs tool calls.

**Proposal.** Documentation first: a worked example in `docs/guides/agent-sessions.md` of three
host tools (`start_job(cmd) -> {id}`, `job_output(id, wait=False)`, `job_kill(id)`) with a job
cap and retention. Call out the one pydeno-specific trap: job state is host memory and tools are
**not called during replay**, so after `load()` a stored job ID refers to nothing. The recipe must
return a typed "job not found" error and the doc should say whether jobs survive a restart (they
do not). If hosts ask for it, promote the recipe to a helper under `python/pydeno/tools/` next to
`http_fetch`.

**Priority** P3. **Risk** Low (docs, then an opt-in helper). Do not add an implicit auto-background
rule: it makes a tool's result shape depend on timing, which does not belong in a journaled,
replayed call.

### 7. CI hygiene: SHA-pinned actions and credential persistence

**Evidence.** Crush pins every action to a full commit SHA with the version in a comment, sets
`persist-credentials: false` on checkout, and sets `permissions: contents: read` per workflow
([build.yml:5-30][ci-build], [security.yml:1-30][ci-sec]). Its build job also runs
`go mod tidy` followed by `git diff --exit-code`, and `go test -race -failfast ./...` on Linux,
macOS and Windows. The security workflow adds CodeQL, a vulnerability scan, `govulncheck`, and
dependency review on pull requests, on a nightly schedule too ([security.yml:1-100][ci-sec]).

**pydeno today.** (checked in `.github/workflows/`) Actions are pinned by tag (`actions/checkout@v5`,
`astral-sh/setup-uv@v7`, `Swatinem/rust-cache@v2`, `PyO3/maturin-action@v1`), no workflow sets
`persist-credentials: false`, and most workflows already set `contents: read`. Supply-chain
scanning exists (`security.yml`: cargo-deny with `deny.toml`, cargo-audit, pip-audit, osv-scanner)
and installs use `uv sync --frozen`.

**Proposal.** Pin third-party actions by SHA (a Dependabot or Renovate rule can keep them
current), add `persist-credentials: false` to checkouts that do not push, and consider a
lockfile-drift check (`uv lock --check`, `cargo metadata --locked`) as crush's tidy-diff gate.

**Priority** P3. **Risk** Low. The cost is update noise unless automated.

## Looked at, nothing relevant to pydeno

- **MCP client lifecycle.** Crush runs MCP servers as child processes with a state machine,
  generation counters, lazy renewal and pure reconcile logic
  ([lifecycle.go:14-60][mcp-life], [init.go:152-160][mcp-state]), and kills the whole process
  group of a stdio server because grandchildren leaked in production
  ([process_unix.go:11-40][mcp-pgrp]). pydeno has no MCP client; `SchemaTool` already accepts
  MCP-shaped definitions (`python/pydeno/_schema.py`) and hosts bring their own transport. The
  per-server `enabled_tools` / `disabled_tools` filters
  ([mcp/tools.go:184-208][mcp-filter]) are what a host would write when it builds the tool dict.
  Namespacing as `mcp_<server>_<tool>` ([mcp-tools.go:58-60][mcp-name]) is what pydeno's
  `namespace=` already gives, without name mangling.
- **Edit staleness checks.** `filetracker` records the last read of a file per session and edit
  refuses if the file changed since ([filetracker/service.go:14-30][filetracker]). pydeno has no
  filesystem for the guest.
- **Context-window summarization, usage accounting, provider fallbacks, LSP tools, TUI.** These
  belong to an LLM client, which pydeno is not.
- **Process isolation tests.** Crush tests that a child's `kill -INT -$$` does not reach the
  parent and that `/dev/tty` access fails fast under `Setsid`
  ([isolation_unix_test.go:19-60][iso-test]). pydeno's worker is a separate process confined by
  seccomp/Seatbelt and the repository has tests for worker kill reasons and orphan handling; whether any
  covers signal or controlling-terminal leakage was not audited here, so this is noted only as the
  same class of regression test.

## Not worth copying

- **Auto-approving by command-text prefix.** Bash skips the permission prompt when a command
  starts with an entry of `safeCommands` and contains none of `;`, `|`, `&&`, `$(`, backtick
  ([safe.go:9-75][safe], [bash.go:210-222][bash-safe]). By reading (not executed): the list
  includes wrappers that run other commands (`timeout`, `nice`, `nohup`, `env`, `time`), mutating
  git forms (`git branch`, `git tag`, `git remote`, `git diff --output=...`), and `kill`/`killall`;
  the chaining check does not look for `>` redirection, `&`, a newline, or `<(`. For example
  `timeout 5 rm -rf build` and `echo x > file` start with a "safe" word and contain no listed
  metacharacter. pydeno's tools take structured arguments, so approval can and should key on
  parsed data, never on a string prefix.
- **"Always allow" keyed by directory.** A persistent grant is stored under
  `(session, tool, action, path)` ([permission.go:88-93,163-170][perm-key]) and bash passes its
  working directory as `path` with a constant action `execute`
  ([bash.go:229-239][bash-perm]). Approving "always" once therefore approves every later bash
  command in that directory for the session, whatever it says. If pydeno ever ships an approval
  helper, grants must be keyed on the tool name plus a canonical argument digest, or be
  tool-name-wide by explicit, loud opt-in.
- **Failing open on hook errors.** A hook that times out, crashes, or exits with an unrelated
  code is logged and the call proceeds ([runner.go:210-254][hook-open],
  crush `docs/hooks/README.md:400-404`). That is the right default for a developer convenience hook and
  the wrong one for a security control; pydeno's gates already fail closed and the new
  `before_call` (takeaway 1) must too.
- **Skipping hooks for sub-agents.** Crush wraps tools with hooks only for the top-level agent
  to avoid firing a hook N times per delegated turn ([coordinator.go:957-962][hook-sub],
  [hooked_tool.go:27-35][hook-sub2]), relying on the outer sub-agent tool call being hooked. A
  policy that must hold for every call cannot have a bypass for nested actors. In pydeno a
  nested session is a session; any policy hook should apply to it by construction.
- **Exit-code protocol and Claude Code envelope compatibility for hooks.** Exit 2 / exit 49 and
  the `hookSpecificOutput` format ([input.go:78-182][hook-input]) suit shell scripts. pydeno
  hooks are Python callables returning a `Verdict`.
- **Command blocklists and an embedded shell interpreter.** pydeno exposes no shell. The
  blocklist is advisory in crush too: it is applied to the command words of an interpreter it
  embeds, not to the OS.
- **Queueing prompts behind a busy session**, with accept reservations, per-run IDs, and cancel
  marks so a follow-up queued before a cancel is dropped
  ([agent.go:60-135][accept], [runid.go][runid], [run_marker.go][run-marker]). This solves a
  multi-client TUI/server race. pydeno raises on concurrent use, which is simpler to reason
  about and to replay.
- **Spill files for oversized output.** Retention of 7 days and a 256 MiB cap with a
  pattern-guarded sweep ([truncate.go:27-47,171-223][spill]) is careful work, but only makes
  sense where the model can read files back. Not applicable to a guest with no filesystem.
- **Unbounded in-memory job buffers, and a model-set auto-background threshold with no upper
  bound** (`auto_background_after`, [bash.go:30,319-321][bash-wait]). Both are conveniences that
  a runtime enforcing budgets should not copy.

## Test and release practice, compared

| Practice | Crush | pydeno |
| --- | --- | --- |
| Test command in CI | `go test -race -failfast ./...` on 3 OSes ([build.yml][ci-build]) | pytest and cargo on a platform matrix (`.github/workflows/test.yml`, `platforms.yml`) |
| Golden files | `-update` flag regenerates ([AGENTS.md][agents-md]) | golden description tests (see the OpenCode catalog note) |
| Recorded LLM interactions | VCR cassettes under `internal/agent/testdata`, re-recorded by `task test:record` ([Taskfile.yaml:87-92][taskfile]) | not needed; the journal replays tool results, not model output |
| Vulnerability scanning | CodeQL, grype, govulncheck, dependency review, nightly ([security.yml][ci-sec]) | cargo-deny, cargo-audit, pip-audit, osv-scanner (`security.yml`); no CodeQL job found |
| Release | GoReleaser with nightly builds, notarization, shell completions and man pages generated in a `before` hook ([.goreleaser.yml:1-50][goreleaser]) | maturin wheels (`PyO3/maturin-action`); release gates were not reviewed here |
| Log style lint | script requiring capitalized log messages | not applicable |

Nothing in this table is a recommendation beyond takeaway 7; it is here so a reader can see
that the comparison was made.

## Suggested order

1. Takeaway 1 (per-call hook) first: highest value, mostly existing parts, and it defines the
   deny semantics that takeaway 4 builds on.
2. Takeaways 2 and 3 together: both change what the model sees, share the "limits" constants, and
   share golden-test updates.
3. Takeaways 4, 5, 6, 7 as backlog issues; 5 and 6 interact (polling), so decide 6 first.

## Crush references

Base: `https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/`

[shell-env]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/shell.go#L92-L100
[bash-banned]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L76-L147
[bash-block]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L166-L196
[bg-buffer]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/background.go#L22-L44
[bash-format]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L390-L426
[bash-truncate]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L428-L448
[agent-errors]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/errors.go#L5-L10
[mcp-run]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/mcp-tools.go#L135-L138
[req-timeout]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/request_timeout.go#L12-L45
[bash-wait]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L315-L385
[queue]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/agent.go#L395-L417
[run-marker]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/run_marker.go
[runid]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/runid.go
[accept]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/agent.go#L60-L135
[session]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/session/session.go#L57-L70
[stop-when]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/agent.go#L1095-L1117
[hooked]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/hooked_tool.go#L54-L100
[hook-run]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/hooks/runner.go#L96-L120
[hook-agg]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/hooks/hooks.go#L94-L158
[perm-hook]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/permission/permission.go#L16-L36
[perm-hook-use]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/permission/permission.go#L196-L202
[hook-merge]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/hooks/hooks.go#L160-L189
[trunc-head-tail]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/truncate.go#L91-L132
[trunc-clip]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/truncate.go#L225-L248
[bash-tpl]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.md.tpl#L11-L14
[bash-desc]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L149-L164
[sort-tools]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/coordinator.go#L953-L955
[perm-denied]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/tools.go#L72-L78
[finish-stop]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/agent.go#L1064-L1075
[hook-halt]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/hooks/hooks.go#L18-L22
[hooked-halt]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/hooked_tool.go#L62-L73
[loop]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/loop_detection.go#L11-L39
[loop-wire]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/agent.go#L1117-L1119
[loop-sig]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/loop_detection.go#L45-L71
[job-output]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/job_output.go#L48-L90
[bg-cap]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/background.go#L16
[bg-expire]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/background.go#L174-L192
[bg-kill]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/background.go#L147-L156
[bg-killall]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/background.go#L194-L211
[app-killall]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/app/app.go#L926
[bash-fast-fail]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L260-L262
[ci-build]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/.github/workflows/build.yml
[ci-sec]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/.github/workflows/security.yml
[mcp-life]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/mcp/lifecycle.go#L14-L60
[mcp-state]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/mcp/init.go#L152-L160
[mcp-pgrp]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/mcp/process_unix.go#L11-L40
[mcp-filter]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/mcp/tools.go#L184-L208
[mcp-name]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/mcp-tools.go#L58-L60
[filetracker]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/filetracker/service.go#L14-L30
[iso-test]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/isolation_unix_test.go#L19-L60
[safe]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/safe.go#L9-L75
[bash-safe]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L210-L222
[perm-key]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/permission/permission.go#L88-L93
[bash-perm]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/tools/bash.go#L229-L239
[hook-open]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/hooks/runner.go#L210-L254
[hook-sub]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/coordinator.go#L957-L962
[hook-sub2]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/agent/hooked_tool.go#L27-L35
[hook-input]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/hooks/input.go#L78-L182
[spill]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/internal/shell/truncate.go#L27-L223
[agents-md]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/AGENTS.md
[taskfile]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/Taskfile.yaml#L82-L92
[goreleaser]: https://github.com/charmbracelet/crush/blob/65865e01950368379ad1f9746b620a77fb7a5da8/.goreleaser.yml#L1-L50
