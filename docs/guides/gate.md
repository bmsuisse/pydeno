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

1. **Static policy** (`static_gate(SourcePolicy(...))`): a pure, deterministic scan in Python. It
   costs microseconds and refuses forbidden names, `eval`, the `Function` constructor, `import()`,
   `WebAssembly` and oversized programs. Its messages are fixed, actionable sentences, so a model can
   use them to fix the code.
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
one.

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

**Timeouts.** `gate_timeout` defaults to 10 s. An async gate is cancelled at the deadline. A sync
gate cannot be interrupted, so it runs to the end, but a verdict that arrives after the deadline is
discarded, also from a gate that swallowed its cancellation. Keep sync gates fast.

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

**Thread safety and re-entrancy.** A sync gate runs on the thread that called the entry point. If an
external function runs on a tool thread and calls another session's `feed_run`, the gate runs on that
tool thread. One gate object may therefore run on several threads at once, so make it thread-safe.
`static_gate` is. A gate that calls back into the session it is gating finds that session busy, and
the gate becomes unavailable. A gate may use other sessions.

## Where gates attach

| Entry point | Gated |
|---|---|
| `Pydeno(gate=...)`, `AsyncPydeno(gate=...)` | every `feed_run` and `feed_start` of every session |
| `AgentSandbox(gate=...)`, `AsyncAgentSandbox(gate=...)` | `start`, `run`, `execute` |
| `IsolatedRuntime(gate=...)`, `AsyncIsolatedRuntime(gate=...)` | `eval`, `eval_async`, `execute`, `execute_async`; the source given to `add_static_module`; every source a `set_module_loader` loader returns (a refusal fails the import, and the command raises it); `RuntimeConfig.bootstrap`, checked before the worker starts. `eval_module*` compiles only sources that have already passed. |

Sync classes take sync gates only, and an async gate is refused at construction. Async classes take
either kind. `AgentSandbox(runtime=...)` refuses a runtime that has its own gate: put the gate on the
session.

`load_wasm` takes a binary from the host, not source text, and is not gated. The in-process `Runtime`
has no `gate=`, because it is not a boundary for hostile code anyway. Call `gate_check(gate, code)`
yourself before `Runtime.eval` if you want the same check.

## Replay is not re-gated

`load_session`, `load_snapshot`, `AgentSandbox.load` and `AsyncAgentSandbox.load` restore state by
replaying a journal, and they do not consult the gate:

- the journal is HMAC-signed with the host's key, so it holds only runs this host already accepted;
- every run in it passed the gate when it first ran;
- replay must be deterministic. A classifier that answers differently today would make recovery fail
  halfway, or the replay diverge.

The gate applies again to every new feed or run after the load. If you tighten a policy and need old
state re-checked, check the journal's code yourself before loading it, or start a fresh session.

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
| `forbidden_identifiers` | the names as identifiers, properties (`x.name`) or string keys (`x["name"]`) |
| `forbidden_globals` | the names bare, or on a global object (`globalThis.name`, `self["name"]`) |
| `forbid_dynamic_import` | `import(...)` |
| `forbid_eval` | `eval`, called or just named (`(0, eval)`); `setTimeout` / `setInterval` with a string |
| `forbid_function` | the `Function` constructor, by name or as `.constructor(...)` |
| `forbid_webassembly` | `WebAssembly` |
| `max_source_bytes` | longer code (checked first; nothing else is scanned then) |
| `include_preflight_rules` | also `check_source`'s usability rules (`require`, `fetch`, ...), off by default under a policy |

The scanner is aware of comments, strings, templates and regular expressions. Under a policy it
reads the code as the engine does:

- `eval` and `\u{65}val` are `eval`, and so are `globalThis["\x65val"]` and
  ``globalThis[`eval`]``;
- U+2028 and U+2029 end a line comment;
- every Unicode space separates tokens.

It also tells a regular expression from a division sign the way the engine does in the cases that
matter: after an object literal, a postfix `++`, a keyword used as a property, or a control statement's
condition. A scanner that got this wrong would read code as a regex or a string. It is not a parser,
though, and code written to confuse it may still succeed, which is one more reason the later layers
exist. Fullwidth letters and zero-width joiners make *different* identifiers in JavaScript, and the
scanner treats them that way too. Without a policy, `check_source(code)` behaves exactly as it always
has.

A denial from `static_gate` lists up to 20 findings as `line:column [rule] message`, one per line.
The labels are the rules, in order. The message templates are a public contract (see the
[gate reference](../reference/gate.md#policy-messages)).
