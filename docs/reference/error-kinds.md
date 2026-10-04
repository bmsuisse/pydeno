# Error kinds

`pydeno.classify_error(exc)` turns any error pydeno can raise into a stable vocabulary, so a service
can decide what to do without matching exception text.

```python
from pydeno import classify_error

try:
    rt.eval(code)
except Exception as exc:
    info = classify_error(exc)   # ErrorInfo(kind, retryable, summary, retry_with_larger_limits)
    if info.retryable:
        ...                      # run the same work again on a FRESH runtime
    log.warning("sandbox run failed: %s", info.to_dict())
```

For errors that came out of an `AgentSandbox` run pass `via_agent=True`: its `max_pause` is the
worker's `max_host_wait`, so the same timeout is reported as `max_pause`. `classify_error` never
raises. The `kind` strings are a public contract: they are only ever added to.

## The retry rule

`retryable` is true **only when the failure was environmental**: the worker process died or failed to
start, so the same work on a fresh runtime could plausibly succeed. A deadline, a memory overrun, a
CPU cap, a spent budget, a guest bug or a failed signature is a property of the work under the limits
it was given; the same code fails the same way again, so those are `retryable=False`.

`retry_with_larger_limits` is true where a bigger limit (`timeout=`, `max_memory=`, `max_calls=`,
`max_host_wait=`, ...) could let the work finish. Whether to grant it is the caller's decision, which
is why it is a separate field and not folded into `retryable`.

## Table

| kind | retryable | with larger limits | raised as | meaning |
|---|---|---|---|---|
| `js_error` | no | no | `JavaScriptError` (including a host tool's error the guest did not catch, unless it names a `Tool*Error`) | The guest code threw or failed to compile. |
| `timeout` | no | yes | `RuntimeTimeout`; `TimeoutError` / `asyncio.TimeoutError` | A deadline passed before the work finished. |
| `cpu_limit` | no | yes | `RuntimeTimeout` carrying the supervisor's CPU-cap message | The worker used more CPU in one command than its cap allows. |
| `memory_limit` | no | yes | `WorkerCrashed`: `worker used N bytes, over max_memory=M; killed`, or the worker's own memory exit | The worker went over max_memory and was stopped. |
| `thread_limit` | no | no | `WorkerCrashed`: `worker started N threads (limit 64); killed` | The worker started more threads than a worker may. |
| `worker_crashed` | yes | no | any other `WorkerCrashed`: died, killed by a signal, hung, would not start | The worker process died, hung or failed to start. |
| `sandbox_violation` | no | no | `WorkerCrashed`: `<worker process died / worker is gone / ...>: sandbox violation: the worker made a forbidden system call` (the kernel killed it with SIGSYS: a never-legitimate syscall such as `ptrace` or `mount`, Linux) | The worker made a system call the OS sandbox forbids and was killed for it. |
| `terminated` | no | no | `RuntimeTerminated` | The runtime was terminated on request. |
| `force_killed` | no | no | `RuntimeForceKilled` | A termination was never acknowledged; the runtime was abandoned. |
| `host_wait` | no | yes | `RuntimeTimeout` from `max_host_wait` | Host callbacks kept the guest waiting longer than max_host_wait. |
| `max_pause` | no | yes | the same timeout with `via_agent=True` (an `AgentSandbox` run) | Tool answers kept an agent run paused longer than max_pause. |
| `host_call_budget` | no | yes | `WorkerCrashed`: `guest made more than max_host_calls=N host calls` | The guest made more host calls than max_host_calls. |
| `inflight_limit` | no | yes | `JavaScriptError` / `RuntimeError`: `more than N host calls in flight`; `too many abandoned tool calls` | Too many host calls were outstanding at once. |
| `tool_budget` | no | yes | `ToolBudgetError`; a `JavaScriptError` that names or starts with `ToolBudgetError` | The tool-call budget (max_calls / max_tool_calls) is spent. |
| `tool_not_found` | no | no | `ToolNotFoundError`; a `JavaScriptError` that names `ToolNotFoundError` | A tool reported that what it was asked for is missing. |
| `tool_failed` | no | no | `ToolError`; a `JavaScriptError` that names `ToolError` | A host tool raised an error. |
| `protocol_violation` | no | no | `WorkerCrashed`: `worker broke protocol`, `malformed frame`, a reused or malformed capability token | The worker sent something the host refuses; it was discarded. |
| `sandbox_unavailable` | no | no | `WorkerCrashed`: `worker failed to start: an OS sandbox is required but ...`, or a failed startup self-test | The worker refused to start without a complete OS sandbox. |
| `limits_unmeasurable` | no | no | `WorkerCrashed`: `max_memory ... cannot be enforced on this system` under `sandbox='require'` | sandbox='require' but the worker's resource usage cannot be read here. |
| `closed` | no | no | `RuntimeError`: `runtime is closed`, `Runtime/Function/Stream has been closed`, `the session is closed` / `was closed` (exact phrases only) | The runtime, function, stream or session is already closed. |
| `journal_invalid` | no | no | `JournalError` | An agent journal is malformed, too large or not authentic. |
| `replay_divergence` | no | no | `ReplayDivergence` | Replaying a journal produced a different outcome. |
| `snapshot_invalid` | no | no | `SnapshotAuthenticationError` | A signed snapshot failed authentication. |
| `invalid_input` | no | no | `TypeError` / `ValueError` (wire errors, bad arguments) | A value or argument was refused (wire or API misuse). |
| `cancelled` | no | no | `asyncio.CancelledError` | The surrounding asyncio task was cancelled. |
| `unknown` | no | no | anything else | An error pydeno does not classify. |

## How the kind is chosen, and what a guest can influence

Kinds come from exception **types** wherever the type is enough. `WorkerCrashed` is one class for many
causes, so its cause is read from the message, but only from the part pydeno wrote: every pattern is
anchored to the start of the message (and to its end where the host's text is the whole message). The
text a worker or guest controls (the last line of its stderr, the message of an error it reports) only
ever appears *after* that prefix. Consequences, all pinned by `tests/test_errors_taxonomy.py`:

- A guest that prints `max_memory` or `runtime is closed` to stderr and dies is still `worker_crashed`.
- The one shape a guest can still influence is a worker that exits cleanly with a whole-message forgery
  as its last stderr line. That can only turn `worker_crashed` into a kind that is *not* retryable,
  never the reverse.
- From an `IsolatedRuntime` a host tool's error reaches you as a `JavaScriptError` rebuilt from its
  message (`Evaluation failed: ToolBudgetError: ...`, with no `.name`), so `tool_*` is read from that
  text. A guest can write such a message itself; all three kinds are non-retryable, like `js_error`,
  so nothing is gained. An error raised on the host (`ToolBudgetError(...)` itself) is classified by
  type and is authoritative.
