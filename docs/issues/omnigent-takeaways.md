# Omnigent: what it offers pydeno's agent surface

Research note, no code changes. Source: [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent)
at `2abdeb1` (`main`, package version 0.18.0.dev0). Paths below are relative to that repository;
line numbers refer to that commit. pydeno references are to `future/0.10`.

## What Omnigent is, and how much of it applies

Omnigent is a Python **meta-harness**: it launches whole coding-agent CLIs (Claude Code, Codex,
Cursor, ...) as child processes, wraps them in OS sandboxes, and puts a policy engine and a web UI
around them. The "guest" is an entire agent process with a filesystem and shell, not a snippet of
model-written code.

- **No JavaScript, V8, Deno, WASM, QuickJS or Monty use.** A word-bounded search of `omnigent/`,
  `docs/`, `designs/`, `sdks/`, `integrations/` and `examples/` finds one `.wasm` MIME-type entry
  (`omnigent/server/app.py:259`) and nothing else. There is no "code mode" where a model writes a
  program that calls tools; tools are called one at a time by the harness.
- Its sandboxing is process confinement (bubblewrap, Seatbelt, a Windows job object, seccomp)
  around a trusted-ish CLI, which is a different problem from pydeno's secret-free V8 worker.
- Most of the repository (web UI, auth, Kubernetes deploy, harness adapters, CI) is unrelated.

So this is a narrow note: one concrete defect it exposed in `http_fetch`, a few policy-layer ideas
worth adapting, and a list of things not to copy.

## Takeaways

### 1. `http_fetch` does not refuse the Azure WireServer address (168.63.129.16)

**Evidence.** Omnigent's egress proxy keeps a list of "public-looking" addresses that a cloud routes
only inside its own network and refuses them even though `ipaddress` calls them global
(`omnigent/inner/egress/proxy.py:137-155`; the check that resolves once, validates every address and
pins the connection is `proxy.py:1075-1186`). The one entry is Azure's WireServer, `168.63.129.16`,
the guest-agent, DNS and health-probe endpoint. Python reports it as `is_global == True`.

**pydeno today.** `python/pydeno/tools/http_fetch.py:105-198` has an extensive refusal table (CGNAT,
link-local, embedded IPv4 in NAT64/6to4/Teredo, metadata host names) and the same resolve-once-and-pin
design (`docs/guides/http-fetch.md:136-138`). A grep for `168.63` across `python/`, `tests/` and `docs/`
finds nothing, and `_ip_refused` falls through to `is_global`, so a name that resolves to
`168.63.129.16` is not refused. It needs an allow-listed host whose DNS an attacker influences, but
BMS workloads run on Azure, which is exactly where this address is live.

**Proposed change.** Add `168.63.129.16/32` to `_REFUSED_V4` with a comment naming it, and
parametrise the existing address-refusal tests with it (plain, as `::ffff:168.63.129.16`, and as a
NAT64 form). Mention it in `docs/guides/http-fetch.md` next to the metadata-address list.

**Priority:** high (small, defensive, relevant to our own deployment). **Risk:** very low; nothing
legitimate resolves there from outside Azure's fabric. Not a new sandbox escape: it widens a
deny-list that already exists.

### 2. A tool-call phase hook with ALLOW / DENY / ASK, and phase-aware failure

**Evidence.**
- `docs/POLICIES.md:3-9` defines three verdicts (ALLOW, DENY, ASK) evaluated in declaration order, DENY
  short-circuits. Enforcement points include the tool call and the tool result
  (`omnigent/policies/types.py:265-273`: a policy may return replacement `data`, such as redacted
  arguments or output).
- Failure is **per phase**. `omnigent/policies/types.py:63-80` fails the *tool call* and *request*
  phases closed (nothing else enforces them) and the *tool result* phase open ("the tool has already
  executed, so failing it closed would only block an already-incurred side effect").
  `omnigent/runner/policy_proxy.py:95-110` applies it.
- An ASK can park for a long time: `policy_proxy.py:22-32` sets a one-day read budget for the
  verdict, recording that a 30 s transport timeout earlier turned a parked approval into a DENY and
  produced duplicate approval cards.
- State written by a policy is withheld on ASK, so "a denied ASK must leave no trace"
  (`types.py:277-286`).

**pydeno today.** `gate=` (`python/pydeno/_gate.py`) checks *source* only: allow or deny, fail closed
on every error and timeout (`_gate.py:12-17`), with `all_of`/`any_of` (`_gate.py:879-887`). Approval of
an individual tool call is a hand-written `start()` / `resume()` loop
(`docs/guides/agent-sessions.md:268-285`). Nothing sits between "a tool is bound" and "the guest
gets its result"; there is no argument/result inspection or redaction in the library itself, and the
only host-error redaction is `redact_host_errors` (`_agent.py:1190`).

**Proposed change.** Do not copy the engine. Write a short design note, and prototype a thin
optional `tool_gate=` on `AgentSandbox` that is called with `(name, args, context)` and returns the
existing `Verdict` (plus an `ask` outcome that simply leaves the session paused at the `ToolCall`,
which `start()` already does). Constraints taken from Omnigent's mistakes and choices:
- Before the call both timeout and error fail closed. After the call, because pydeno has not yet
  handed the value to the guest, a result inspector should also fail closed; this differs from
  Omnigent, whose result phase runs after the model already saw the side effect.
- An ASK wait is governed by `max_pause`, never by `gate_timeout` (default 10 s,
  `_gate.py:DEFAULT_GATE_TIMEOUT`). Reusing the short timeout is the bug Omnigent fixed.
- The verdict and any replacement value must be journaled, or `load()` replay diverges
  (`docs/guides/agent-sessions.md:340-350`). A denial is a recorded error answer, like
  `resume(error=...)`.
- Pin by test whether a call refused by the host counts against `max_tool_calls`
  (`_agent.py:882`). I did not verify the current behaviour; Omnigent's rule is that a refused
  or pending call leaves no counter change.

**Priority:** medium. **Risk:** medium; it touches the journal format and the hot call path. Ship the
recipe in the guide first and the hook only if two real users hand-roll the same loop.

### 3. Opt-in detection of repeated identical tool calls

**Evidence.** `omnigent/policies/builtins/safety.py:158-235` hashes `(tool, args)` into a sliding
window of 10 and asks a human when the same call appears 3 times. Its docstring: this catches "an
agent retrying the exact same failing tool call", which a total-call counter cannot
(`safety.py:100-145` is that counter). The state is stored in the session, not process memory.

**pydeno today.** One session-wide budget, `max_tool_calls` (`_agent.py:794-884`), raising
`ToolBudgetError`. A model that retries one failing call 900 times spends the budget and the host
learns of it at the end. The guide notes the counter lives in the host process and is not restored
by `load()` (`agent-sessions.md:262-264`), though lost runs do count (`agent-sessions.md:340-348`).

**Proposed change.** An opt-in `max_identical_calls=(window, threshold)` that throws a catchable
error with a stable `error_type` (say `ToolLoopError`) into the guest, so the model sees it and can
change approach; also a recipe for `start()` loops. Derive the window from the journal on `load()` so
that a restored session does not forget. Hash the canonical JSON of the arguments, not `repr`.

**Priority:** low to medium. **Risk:** low if opt-in; false positives for polling tools, so allow a
per-tool exemption. It is waste control, not a security boundary, and the docs should say so.

### 4. Session-level "risk accrual" as a documented recipe

**Evidence.** `omnigent/policies/builtins/risk_score.py:1-55`: tool calls and sensitive result
labels add points to a persisted score; once past a threshold, named "guarded" tools change from
ALLOW to ASK or DENY. The aim is human review after a session has touched untrusted or sensitive
material, without enumerating every sequence.

**pydeno today.** The `start()` loop can do this, but there is no example, and the guide's security
notes only say to treat arguments as data (`agent-sessions.md:356-361`).

**Proposed change.** Add a ~20 line example to `docs/guides/agent-sessions.md`: count calls to
`fetch_url`, and once any fetch has happened, `send_email` requires `reviewer_approves`. No new API.
State plainly that it is a heuristic; the boundary is still the bound-tool set.

**Priority:** low. **Risk:** none (docs). Skip if takeaway 2 lands, where it becomes the example.

## What pydeno already does as well or better

| Omnigent | pydeno |
|---|---|
| Resolve once, check every address, pin the IP against DNS rebinding (`proxy.py:1075-1186`) | Same design, with more forms (embedded IPv4, canonical-host rule): `http_fetch.py:105-198`, `http-fetch.md:116-152`. |
| Host grammar allow-list rejecting NUL, `%`, CR/LF, `@` (`omnigent/inner/egress/rules.py:22-57`) | `_authority` / `_check_path` reject encoded separators, `%25`, `%00`, `;`, dot segments, credentials in URLs, non-canonical IPv4 (`http_fetch.py:284-335`), with tests in `tests/test_http_fetch.py:347-519`. |
| Credentials kept out of the sandbox via placeholder swap at a proxy (`designs/SANDBOX_CREDENTIAL_PROXY.md:22-65`) | The guest never holds a secret: fixed host-side headers it can neither set nor read, not sent across origins (`http-fetch.md:13, 80, 152`). The problem the placeholder solves does not arise. |
| Seccomp baseline taken from containerd `RuntimeDefault`, compat architectures registered so `int $0x80` cannot bypass (`omnigent/inner/_seccomp.py:1-45`) | Default-deny tail, and the filter kills a mismatched `AUDIT_ARCH` outright (`python/pydeno/_sandbox.py:532-576`), which covers the same bypass. Landlock and an empty root on top (`docs/security-report.md:13-16`). |
| Fail-closed policy errors | `Verdict` strictness and fail-closed gate (`_gate.py:12-17`). |
| Deterministic counters: policy `SET` to snapshot+1 so repeated instances do not double count (`safety.py:131-136`) | Lost runs keep their calls counted in the journal so a crash cannot refund budget (`agent-sessions.md:340-348`). |
| Bounded outputs (`omnigent/inner/os_env.py:167`, 100,000 characters per tool output) | Separate stdout/stderr, result and journal byte caps, with a `truncated` flag and `ResultTooLarge` (`agent-sessions.md:60-80`). |

## Not worth copying

- **bwrap, Seatbelt, Windows job object, copy-on-write workspaces, cwd dotfile masking**
  (`omnigent/inner/bwrap_sandbox.py`, `omnigent/sandbox/copy_on_write.py`,
  `omnigent/inner/_cwd_scan.py`). They confine a CLI that needs a real working directory. pydeno's
  worker has an empty root and no ambient files to mask.
- **MITM egress proxy with a private CA** (`omnigent/inner/egress/`, about 2,500 lines). The guest has
  no network; `http_fetch` is a single audited tool. A general proxy would be a larger attack surface
  than the thing it replaced. Its useful content is the address list in takeaway 1.
- **Policy engine layering by persona** (session, spec, server; `docs/POLICIES.md:11-21`), labels,
  workspace-scoped stored policies, CEL conditions, LLM-judged policies. Product features of a
  multi-tenant server, not of an embeddable library.
- **`srt` wrapping of local tools** (`omnigent/tools/_srt.py:1-30`). Its own header records that
  sandboxing stdio MCP servers was removed because default-deny network broke every useful server;
  the lesson is only that confinement must be designed with the tool's needs, which pydeno's
  tool-in-host model already sidesteps.
- **Session compaction and transcript import** (`docs/session-compaction.md`). It concerns the
  *model's* context, which pydeno leaves to the host. pydeno's journal records tool answers for
  replay, a different thing.
- **Kubernetes warm pools, release automation, contributor PR security gate**
  (`designs/AGENT_SANDBOX_WARM_POOLS.md`, `SECURITY.md:66-128`). pydeno already pools workers and has
  its own CI gates.

## Verification and limits of this note

- Read-only review of the Omnigent tarball for `main` at the commit above; nothing was executed.
- That `168.63.129.16` passes `ipaddress.ip_address(...).is_global` was checked in Python. That
  `_ip_refused` therefore accepts it is derived from reading `http_fetch.py:105-198`; the module was
  not imported against a native build, so confirm with a failing test before the fix.
- Behaviour claims about pydeno's budget accounting under a host-refused call (takeaway 2) are
  explicitly unverified.
