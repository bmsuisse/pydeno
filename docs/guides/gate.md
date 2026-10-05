# A gate in front of the sandbox

A **gate** is a check that runs on the host, in front of the sandbox. It sees the exact source that is
about to run and answers with a `Verdict`. If the gate refuses the code, it never reaches a worker.

```python
from pydeno import GateContext, Pydeno, SourcePolicy, Verdict, all_of, static_gate

policy = SourcePolicy(
    forbid_eval=True,
    forbid_function=True,
    forbid_dynamic_import=True,
    forbid_webassembly=True,
    max_source_bytes=64 * 1024,
)

def classifier(source: str, context: GateContext) -> Verdict:
    # Your own check: a model, a rule engine, an allow-list of hashes...
    if "rm -rf" in source:
        return Verdict(False, "Do not try to run shell commands.", ("shell",))
    return Verdict(True, "")

gate = all_of(static_gate(policy), classifier)

with Pydeno(gate=gate, strict_eval=True) as pool:
    with pool.checkout() as session:
        session.feed_run("[1, 2, 3].map(x => x * 2)")     # runs
        session.feed_run("eval('1 + 1')")                  # raises GateDenied
```

## Layers, from cheapest to strongest

Each layer catches what the one before it missed. A later layer never relies on an earlier one.

1. **Static policy** (`static_gate(SourcePolicy(...))`): a pure, deterministic scan in Python that
   runs in linear time (well under a second for the default 1 MiB cap). It refuses forbidden names,
   `eval`, the `Function` constructor, `import()`, `WebAssembly` and oversized programs, and by
   default it fails closed: it reads the whole text, strings and comments included. Its messages are
   fixed, actionable sentences, so a model can use them to fix the code.
2. **An optional classifier that you supply**: any callable, sync or async (a model call, a policy
   service). Combine it with the static layer in `all_of(...)`. The static layer runs first, and a
   classifier is not called for code that the static layer already refused.
3. **`strict_eval=True`**: V8 refuses to compile code from strings (`eval`, `new Function`, the
   other function constructors), however the guest reaches them. Strings assembled at run time,
   which no gate can see, are covered here.
4. **The isolated worker** (`Pydeno`, `AgentSandbox`, `IsolatedRuntime`): a separate process with an
   OS sandbox (Seatbelt or Landlock with seccomp), `--jitless` V8, memory, CPU and time limits, and
   only the host functions you bound. **This is the boundary.**

## Honest limits

A gate is defence in depth, not the boundary. A static scan reads text, not behaviour. It cannot see
a name built at run time (`globalThis['ev' + 'al']`), code returned by a tool, or an interpreter
written in plain JavaScript, and it errs towards false positives (a local variable that happens to
have a forbidden name is a finding). A classifier can be wrong or fooled. Treat a gate as a way to
refuse obvious misuse early and cheaply, to give the author a precise reason, and to keep a record
of what was refused and why. What contains code that gets past it is the isolated worker. Keep
`strict_eval=True` and the default `sandbox="require"`, and bind only what the guest needs.

## The contract

A gate is `gate(source: str, context: GateContext) -> Verdict`, or a coroutine function that returns
one. A gate that takes exactly one positional argument, such as `async def classify(source)`, is called
with the source alone.

The signature is checked when the gate is configured (`gate=`, `all_of` / `any_of`) or passed to
`gate_check`. It is checked by binding the signature, never by calling the gate. A gate that can take
neither `(source, context)` nor `(source)` raises `TypeError` there, as a programming error, not as an
outage. A `TypeError` raised inside the gate's own body still makes the gate unavailable. A callable
whose signature cannot be read, such as some builtins, is trusted to take `(source, context)`.

- **`Verdict(allow, reason, labels=())`**: `allow` must be a real `bool`. `labels` is a tuple of
  strings, and on a denial the first one is the *top label* (`GateDenied.top_label`).
- **`GateContext`** is frozen and has these fields:
    - `language`: `"javascript"`.
    - `mode`: the method, such as `"feed_run"`, `"run"`, `"eval"` or `"add_static_module"`.
    - `entry_point`: for example `"PydenoSession.feed_run"`.
    - `tools`: the host function names the code can call.
    - `source_length`: the size in UTF-8 bytes.
    - `source_sha256`: hex, handy as a cache key.
    - `specifier`: the module name for module sources, otherwise `None`.

**The gate fails closed.** The two outcomes that block a run:

| The gate... | Raises | `classify_error` kind | Retryable |
|---|---|---|---|
| returns `Verdict(allow=False)` or raises `GateDenied` | `GateDenied` (`.reason`, `.labels`, `.top_label`) | `gate_denied` | no |
| raises any other `Exception`, returns anything but an exact `Verdict`, or answers after `gate_timeout`, or raises `GateUnavailable` itself ("retry shortly") | `GateUnavailable` (`.reason`) | `gate_unavailable` | yes (the run is still blocked) |

Both are `PydenoError` subclasses. The message of a gate's own exception never appears in
`GateUnavailable`'s text. That exception is the `__cause__`, so classifier errors with internal
detail are not shown to a model.

**Cancellation is not a verdict.** `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and
any other `BaseException` propagate unchanged, and nothing runs. An async session cancelled while its
gate runs is left exactly as it was.

**Timeouts.** `gate_timeout` defaults to 10 s.

- **Async gates** are cancelled at the deadline.
- **Sync gates** cannot be interrupted, so they run to the end, but a verdict that arrives after
  the deadline is discarded, also from a gate that swallowed its cancellation.
- **Sync gates in the async classes** (and in `async_gate_check`) run on a gate thread, never on the
  event loop, so a slow one stalls nothing else. At the deadline its result is abandoned and the
  thread finishes on its own. There are 32 gate threads; when gates that never return hold them all,
  further checks wait and time out as `GateUnavailable`.

In the async classes and `async_gate_check` the deadline is required: `gate_timeout=None` is a
`ValueError` there. The sync classes accept `None`, meaning a verdict is never discarded for being
late. Keep sync gates fast.

**No gap between check and use.**

- The source is first made an exact `str`, with `str.__str__`, so a `str` subclass's own `__str__`,
  `__eq__` or slicing never runs.
- That one string is what the gate sees and what then runs. The gate sees the same Unicode the engine
  compiles: escapes, bidi controls and zero-width characters arrive unchanged.
- A source over 16 MiB (UTF-8) is denied with the label `source-too-large` before the gate is
  called.
- In `Pydeno` the gate sees the feed's code. The host then adds only its own fixed setup: stubs for
  the external functions, and `inputs` as JSON data. It also rewrites the trailing expression to
  `return`. `inputs` and `external_lookup` are copied before the gate runs, so changing the caller's
  dicts from inside the gate changes nothing.

**A denied call has no side effects.** No command reaches the worker, and nothing is charged or
recorded: no tool budget, external-call budget, in-flight slot, journal record or guest state is
used. The session stays usable.

**A closed session does not call the gate.** A run on a closed session, or an eval on a closed
runtime, raises its usual error before the gate is consulted, so a classifier is never paid for code
that could not run.

**Thread safety and re-entrancy.** In the sync classes, a sync gate runs on the thread that called
the entry point. If an external function runs on a tool thread and calls another session's
`feed_run`, the gate runs on that tool thread. In the async classes it runs on a gate thread. One
gate object may therefore run on several threads at once, so make it thread-safe. `static_gate` is.
A gate that calls back into the session it is gating finds that session busy, and the gate becomes
unavailable. A gate may use other sessions.

## Where gates attach

| Entry point | Gated |
|---|---|
| `Pydeno(gate=...)`, `AsyncPydeno(gate=...)` | every `feed_run` and `feed_start` of every session |
| `AgentSandbox(gate=...)`, `AsyncAgentSandbox(gate=...)` | `start`, `run`, `execute` |
| `IsolatedRuntime(gate=...)`, `AsyncIsolatedRuntime(gate=...)` | `eval`, `eval_async`, `execute`, `execute_async`; the source given to `add_static_module`; every source a `set_module_loader` loader returns (a refusal fails the import, and the command raises it); `RuntimeConfig.bootstrap`, checked before the worker starts. `eval_module*` compiles only sources that have already passed. |

Sync classes take sync gates only, and an async gate is refused at construction. Async classes take
either kind. `AgentSandbox(runtime=...)` refuses a runtime that has its own gate: put the gate on the
session.

**Pools pass it through.** `SandboxPool(gate=...)` and `AsyncSandboxPool(gate=...)` give every
runtime they hand out the gate. `SessionPool(..., gate=...)` gives it to every session it builds.
Sessions it restores from their journals replay without it, like any load.

**Module loaders.**

- A loader's refusal belongs to the command that started the import: only that command raises it,
  even with other commands queued on the same async runtime.
- It keeps its `__cause__`.
- A guest can catch the failed import (`import(...).catch(...)`), but the host still raises the
  refusal when the command ends.
- In that case the rest of the command did run: only the refused module did not. That is the one
  place where `GateDenied` does not mean "nothing ran".

**Not gated.**

- `load_wasm` takes a binary from the host, not source text.
- The in-process `Runtime` has no `gate=`, because it is not a boundary for hostile code anyway.
- The command line (`pydeno ...`), the `llm` plugin and the pydantic-ai integration (`JSCodeMode`)
  take no `gate=` either.

In these cases call `gate_check(gate, code)` yourself first, or build the runtime they use with a
gate where they accept one.

## Replay is not re-gated

`load_session`, `load_snapshot`, `AgentSandbox.load` and `AsyncAgentSandbox.load` restore state by
replaying a journal, and they do not consult the gate:

- the journal is HMAC-signed with the host's key, so it holds only runs this host already accepted;
- every run in it passed the gate when it first ran;
- replay must be deterministic. A classifier that answers differently today would make recovery fail
  halfway, or the replay diverge.

The gate applies again to every new feed or run after the load. If you tighten a policy and need old
state re-checked, check the journal's code yourself before loading it, or start a fresh session.

**Use one `dump_key` per gate configuration.** Replay trusts whatever the key signed. A dump from a
pool with a lenient gate, or none, loads into a pool with a strict gate if both share a `dump_key`, and
it replays code the strict gate would refuse. Give each gate configuration its own key, or put the
configuration in `associated_data`.

## Standalone use

Run the same gate in your own process before you start a worker at all, and pass the same callable as
`gate=` too:

```python
from pydeno import GateDenied, GateUnavailable, gate_check

try:
    verdict = gate_check(gate, code)             # or: await async_gate_check(gate, code)
except GateDenied as denied:
    reply_to_model(denied.reason)                # the next step for the author
except GateUnavailable:
    retry_later()
```

`gate_check` applies exactly the rules above, including the exact-`str` normalisation, the size cap
and the timeout. It returns the allowing `Verdict`. `static_gate(policy)` is pure Python: no I/O, no
imports when it is called, and deterministic. You can import it and run it anywhere, a sandboxed
process included.

## Static policy

`check_source(code, policy=SourcePolicy(...))` returns the policy's findings, and
`static_gate(policy)` turns them into a gate. The fields:

| Field | Refuses |
|---|---|
| `forbidden_identifiers` | the names, wherever they appear |
| `forbidden_globals` | the names, wherever they appear (an alias such as `g.name` may be the global); precise mode: bare or on a global object only |
| `forbid_dynamic_import` | `import` followed by anything a static `import` or `import.meta` cannot start with, such as `(`, a comment or the end |
| `forbid_eval` | `eval`, called or just named (`(0, eval)`); `setTimeout` / `setInterval` with a string literal |
| `forbid_function` | the name `Function`, and `constructor` anywhere but a class's own `constructor(...)` method definition |
| `forbid_webassembly` | `WebAssembly` |
| `forbid_computed_global_access` | a global object used other than as `name.property` (`globalThis[k]`, `= globalThis`, `f(this)`, `...self`), `Reflect`, and `constructor[` / `Function[` (off by default) |
| `max_source_bytes` | longer code, checked first; nothing else is scanned then. Default **1 MiB** (`None` removes it): a 16 MiB source could not be scanned within the default 10 s gate timeout |
| `include_preflight_rules` | also `check_source`'s usability rules (`require`, `fetch`, ...), off by default under a policy |
| `ignore_strings_and_comments` | selects the precise mode (below); off by default |

**The default mode fails closed.** The policy's checks must not depend on telling a regular
expression from a division sign, or a comment from code. A scanner that guesses wrong reads code as
text: `await /`/`, an HTML-like `<!--` comment, a hashbang line, or `function(){} / ...` have each
hidden a call that then ran. So the default scan works differently:

- it decodes every escape wherever it appears: `\u0065`, `\u{0065}` with any number of leading
  zeros, `\x65`, legacy octal `\145`, identity escapes such as `\e`, and line continuations;
- it reads the whole decoded text: code, comments, strings, templates, regular expressions, HTML-like
  comments and hashbang lines;
- it reports every forbidden name it finds there.

A name in a string or a comment is therefore reported too. That over-reporting is the safe default.
Findings still point at the original line and column. Fullwidth letters and zero-width joiners make
*different* identifiers in JavaScript, and they are not reported as `eval`. The scan is a few compiled
patterns in one linear pass, with bounded look-arounds.

**The precise mode** (`ignore_strings_and_comments=True`) is opt-in and best effort. A tokenizer skips
strings, comments, templates and regular expressions, so it reports fewer false positives. It decodes
`\u` and `\x` escapes in identifiers and string keys, and treats every Unicode space as a space.
**Known bypasses**, pinned by `tests/test_gate_scanner.py`, which the default mode catches:

- a regular expression or object literal the tokenizer mistakes for division, or the reverse:
  after `await`, `yield` or `of`, after a function or class expression, after `await {}` or `...{}`;
- HTML-like comments (`<!--`) and hashbang lines;
- legacy octal escapes (`"\145val"`) and `\u{...}` with more than eight digits;
- names reached without being written: `Reflect.get(globalThis, k)`, `const {[k]: e} = globalThis`,
  and `const {constructor: F} = function(){}`.

Use the precise mode only where false positives in strings and comments are a real problem, and
never as the only layer. Without a policy, `check_source(code)` behaves as it always has.

`forbid_computed_global_access` closes the most common way around a name list:
`globalThis['ev' + 'al']` is not a name any scan can read. It is best effort and a heuristic, not a
boundary:

- it does not follow an alias made in a way it cannot see;
- it reports `this` in methods too (`f(this)`, `this[k]`), where `this` is not the global object.

`obj[k]` on any other object and `globalThis.name` are allowed. `strict_eval=True` is what stops a
computed `eval`.

A denial from `static_gate` lists up to 20 findings as `line:column [rule] message`, one per line,
for people and logs. Its labels are the rules, in order of first appearance, so `top_label` is the
rule of the first finding.

**The model-facing text.** Show the author the bare message without the location:

- `POLICY_MESSAGES[rule]` is the clean template for a rule;
- for the text filled in with its name, `static_gate(policy).check(source)` (or
  `check_source(source, policy=policy)`) returns each `Finding` with `rule`, `line`, `column`, `text`
  (the filled-in message, no location) and `template` (`POLICY_MESSAGES[rule]`).

The templates are a public contract (see the [gate reference](../reference/gate.md#policy-messages)).
