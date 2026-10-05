# Gate reference

See the [gate guide](../guides/gate.md) for how gates fit together with the sandbox. Every name below
is importable from `pydeno`.

## Types

| Name | What it is |
|---|---|
| `Gate` | `Callable[[str, GateContext], Verdict \| Awaitable[Verdict]]`, or a one-argument `Callable[[str], ...]`; an incompatible signature is a `TypeError` when configured |
| `Verdict(allow: bool, reason: str, labels: tuple[str, ...] = ())` | A gate's answer (frozen). A denial's first label is its top label. |
| `GateContext` | Frozen: `language`, `mode`, `entry_point`, `tools`, `source_length` (UTF-8 bytes), `source_sha256` (hex), `specifier`. `GateContext.for_source(source, *, mode="check", entry_point="gate_check", tools=(), specifier=None)` builds one. |
| `GateDenied(reason, labels=())` | `PydenoError`: the gate refused. `.reason`, `.labels`, `.top_label`. Kind `gate_denied`, not retryable. |
| `GateUnavailable(reason)` | `PydenoError`: the gate could not decide. `.reason`. Kind `gate_unavailable`, retryable; the run is still blocked. |
| `SourcePolicy(...)` | The static policy (frozen); see the fields in the [guide](../guides/gate.md#static-policy). Default mode reads the whole decoded text and fails closed; `ignore_strings_and_comments=True` is the best-effort precise mode. `max_source_bytes` defaults to 1 MiB. |
| `StaticGate` | What `static_gate(policy)` returns: `.policy`, and `.check(source)`, which returns a `PreflightResult` with the findings. |
| `Finding` | `rule`, `line`, `column`, `severity`, `message`; `.text` is the bare message without a location (the model-facing next step), `.template` is `POLICY_MESSAGES[rule]` (None for rules without a policy template). |

## Functions

| Function | Returns |
|---|---|
| `gate_check(gate, source, context=None, *, timeout=10.0)` | The allowing `Verdict`, or raises `GateDenied` / `GateUnavailable`. Sync only: an async gate is unavailable here. |
| `await async_gate_check(gate, source, context=None, *, timeout=10.0)` | The same, for a coroutine. An async gate is awaited and cancelled at `timeout`; a sync gate runs on a gate thread and is abandoned at `timeout`. `timeout=None` is a `ValueError`. |
| `static_gate(policy)` | A sync gate that denies on any `check_source(source, policy=policy)` error. |
| `all_of(*gates)` | Allows when every gate allows. Stops at the first denial, which leads with its own reason and labels, followed by the labels of earlier gates that allowed. Unavailable if any gate is. |
| `any_of(*gates)` | Allows at the first gate that allows. Otherwise it denies with every reason and label merged, or is unavailable if a gate could not decide. |
| `set_gate_threads(n)` | How many threads run sync gates for async callers, process-wide (default 32, or `PYDENO_GATE_THREADS`). They are daemon threads; gates that never return can hold them all, and later checks then end as `GateUnavailable`. |
| `check_source(code, *, policy=None, ...)` | A `PreflightResult`. With a `policy`, the result holds the policy's findings, each an error. |

`all_of` and `any_of` are async when any gate in them is async.

## Hooks

The hooks take `gate=` and `gate_timeout=`; the timeout defaults to 10 seconds. `None` means no
limit and is accepted by the sync classes only. In the async classes a sync gate runs on a gate
thread. `SandboxPool`, `AsyncSandboxPool` and `SessionPool` pass `gate=` through to what they build.

| Hook | Gate kinds | Modes (`context.mode`) |
|---|---|---|
| `Pydeno` | sync | `feed_run`, `feed_start` |
| `AsyncPydeno` | sync or async | `feed_run`, `feed_start` |
| `AgentSandbox` | sync | `start`, `run`, `execute` |
| `AsyncAgentSandbox` | sync or async | `start`, `run`, `execute` |
| `IsolatedRuntime` | sync | `eval`, `eval_async`, `execute`, `execute_async`, `add_static_module`, `module_loader`, `bootstrap` |
| `AsyncIsolatedRuntime` | sync or async | `eval`, `add_static_module`, `module_loader`, `bootstrap` |

Sources over 16 MiB of UTF-8 (`pydeno._gate.MAX_GATE_SOURCE_BYTES`) are denied before the gate runs,
with the label `source-too-large`.

## Policy messages

Every `SourcePolicy` finding carries one of these messages, filled in only with values the host
chose: a name from the policy, `setTimeout` or `setInterval`, or numbers. Text from the code never
appears in a message. The templates are a **public contract**, because a model may see them as its
next step. New rules may be added, and any change to existing wording is noted in the changelog. The
same table is `pydeno.POLICY_MESSAGES`.

| Rule (label) | Message |
|---|---|
| `source-too-large` | The code is {size} bytes, over the limit of {limit} bytes. Send a shorter program. |
| `forbidden-identifier` | \`{name}\` is not allowed here. Rewrite the code without it. |
| `forbidden-global` | The global \`{name}\` is not allowed here. Rewrite the code without it. |
| `forbidden-dynamic-import` | import(...) is not allowed here. Use only the functions you were given. |
| `forbidden-eval` | eval is not allowed here. Write the code directly instead of building it from strings. |
| `forbidden-string-timer` | {name} with a string argument compiles that string, which is not allowed here. Pass a function instead. |
| `forbidden-function-constructor` | The Function constructor is not allowed here. Write the code directly instead of building it from strings. |
| `forbidden-webassembly` | WebAssembly is not allowed here. Write the computation in JavaScript. |
| `forbidden-computed-global-access` | Looking up a global by a computed name is not allowed here. Use the name directly. |

`{size}` is a number, or "more than N" when the text is too large to be worth measuring exactly.
