# Pydeno: the front door

`Pydeno` is the one entry point for running AI-generated JavaScript securely and fast. It has the
same shape as [Monty](https://github.com/pydantic/monty)'s `Monty`: if you know Monty, you already
know it.

```python
from pydeno import Pydeno

with Pydeno() as pool:                        # pre-started, OS-sandboxed workers
    with pool.checkout() as session:          # one single-use worker, checked out by `with`
        session.feed_run("const prices = [3, 4, 5]")
        total = session.feed_run(
            "prices.reduce((a, b) => a + b) * await rate('EUR')",
            external_lookup={"rate": lambda currency: 1.1},   # your Python, called on demand
        )
```

The same with asyncio (external functions may then be coroutine functions):

```python
from pydeno import AsyncPydeno

async with AsyncPydeno() as pool:
    async with pool.checkout() as session:
        await session.feed_run("1 + 1")       # 2
```

## What you get by default

Nothing below needs an argument. Relaxing any of it is an explicit argument, documented as a risk.

| Default | Value |
|---|---|
| OS sandbox | `sandbox="require"`: every layer the platform has (macOS Seatbelt; Linux Landlock + seccomp), or `Pydeno()` refuses to start with a `PydenoCrashedError` that points at `pydeno.sandbox_status()`. Never a silent downgrade |
| V8 | `--jitless` (no JIT, no WebAssembly), the linear-time regexp fallback, `--freeze-flags-after-init` |
| Host errors | Redacted: an exception in your function reaches the guest as an `Error` named after its class, with the message `host function failed` |
| `max_feed_duration_secs` | 30 s of guest running time per feed (waiting on your functions does not count); the worker's CPU is capped at twice that |
| `max_memory` | 512 MiB of worker memory; `ArrayBuffer` storage capped at a quarter of it (a catchable `RangeError`) |
| `max_suspensions` | 1000 external calls per session |
| `max_host_wait_secs` | 60 s of waiting on external calls per feed (600 s before 0.11.1) |
| `tool_timeout_secs` | off: no per-call deadline on external calls (see below) |
| Workers | Single-use: a worker never serves a second session; it is killed when the `with` block exits |
| Guest globals | No `Deno`, `process`, `require`, filesystem, network, `SharedArrayBuffer`, `Atomics`, `WeakRef`; a frozen clock and a seeded `Math.random` of the session's own |
| Pool | `min_processes=2` workers started ahead of time (about 30 to 40 MB each); the first starts in the constructor, so a machine that cannot sandbox fails there |

## Monty to pydeno

The same names, mapped onto pydeno's building blocks (`SandboxPool` underneath, every session an
`AgentSandbox` on a checked-out `IsolatedRuntime`).

| Monty | pydeno | What differs |
|---|---|---|
| `Monty()` / `AsyncMonty()` | `Pydeno()` / `AsyncPydeno()` | Workers are single-use, so there is no `max_checkouts_per_worker`. By default an empty pool starts a worker on the spot (a cold start, never an error, never a wait). The opt-in `max_workers=` caps the pool's live worker processes, checked-out sessions included (Monty's `max_processes`); at the cap a checkout waits up to `checkout_timeout=` (default 30 s) and then raises `CheckoutTimeout`. `min_processes` is the number kept ready. `Pydeno()` starts its first worker in the constructor; `AsyncPydeno` on `async with` |
| Speed, warm pool (checkout / with first feed / next feed) | the same calls | pydeno 0.11 / about 1.5 / 0.4 ms (the first feed also freezes the clock in a round trip of its own); Monty about 0.04 / 0.04 to 0.15 / 0.01 ms on the same machine. A pydeno checkout does no worker round trip (the session's setup is pre-installed on each pooled worker); the per-feed floor is the worker's async evaluation. Monty reuses workers, pydeno never does (see [Performance](#performance)) |
| `pool.checkout(script_name=, limits=)` | `pool.checkout(script_name=, limits=)` | No type checking, `os_policy` or `print_flush_interval` (see below) |
| `MontySession` / `AsyncMontySession` | `PydenoSession` / `AsyncPydenoSession` | `session_id` is always `None` (as for Monty's local workers) |
| `session.feed_run(code, inputs=, external_lookup=, print_callback=)` | the same | JavaScript: a feed's result is its trailing expression (or what it `return`s); a top-level assignment (`x = 1`) has none, as in Monty. Feeds may `await`. A name must be in `external_lookup` to be callable (JavaScript cannot intercept undefined names), and external calls return promises: `await fetch(1)`. `inputs` are plain data (None, bool, int, float, str, list, dict) |
| Sync session refuses async externals | the same (`RuntimeError`) | |
| `print_callback(stream, text)` | the same | `console.log/info/debug` is `"stdout"`, `warn/error/trace` is `"stderr"`. The default prints to this process with control characters, bidirectional overrides and zero-width spaces replaced; a callable of your own gets the text exactly as the guest wrote it (clean it before it reaches a terminal). No `CollectStreams` / `CollectString`: pass a callable |
| `session.feed_start(...)` -> `FunctionSnapshot` / `MontyComplete` | `PydenoSnapshot` / `PydenoComplete` (`AsyncPydenoSnapshot` for asyncio) | No name-lookup or future snapshots: a JavaScript call is always a function call. `kwargs` is always `{}`. A snapshot is only ever for a function in this feed's `external_lookup`: a call to any other name (a stub an earlier feed installed, or a name the guest made up) throws a `ReferenceError` in the guest and never reaches you. After `load_snapshot(state)` without `external_lookup` the names are not known and every call is surfaced; pass `external_lookup=` to keep the restriction |
| `snapshot.resume({'return_value': v})` / `({'exception': e})` / `({'exc_type': ..., 'message': ...})` | the same, plus `resume(value=v)` / `resume(error=e)` | `{'future': ...}` is refused: answer with a value or an error |
| `snapshot.resume_auto()` | the same | |
| `snapshot.dump()` / `session.dump()` | the same, plus `associated_data=` | The bytes are the agent sandbox's journal, **HMAC-signed** with the pool's `dump_key` (random per pool by default; pass your own to load in another process). Monty's dumps are unauthenticated. Signing proves the pool made the state, not whose it is or that it is the newest: pass `dump(associated_data=b"tenant-42:chat-7:3")` (and the same bytes to `load_session` / `load_snapshot`) to bind it to a tenant and a counter you keep, or any state the pool dumped (another tenant's, or an older one of the same session, with the external-call budget it had then) loads into any session |
| `session.load_session(state)` / `load_snapshot(state, external_lookup=, print_callback=)` | the same | Restored by **deterministic replay** on a fresh worker (V8 cannot serialise a live heap): recorded external answers are replayed, your functions are not called again. Works on a session whose worker died, and `dump()` after a crash returns the state as of the last good feed |
| `session.worker_pid` | the same | |
| `ResourceLimits` | `PydenoLimits` | See the table below |
| `MontyError`, `MontyRuntimeError`, `MontySyntaxError`, `MontyCrashedError` | `PydenoError`, `PydenoRuntimeError`, `PydenoSyntaxError`, `PydenoCrashedError` | `exception()` returns the pydeno exception underneath, and `classify_error()` classifies it. `display('traceback' \| 'type-msg' \| 'msg')` and `traceback()` exist; the worker reports no JavaScript stack yet, so `'traceback'` is usually `'type-msg'` |
| Timeouts | `PydenoTimeoutError` | A `TimeoutError` and a `PydenoCrashedError` (`timed_out=True`): pydeno's deadline kills the worker, so the session is over. Monty raises inside the sandbox and keeps the session; pydeno chose the kill because a V8 that is told to stop is not always able to (see the security report) |

The default printer shares a 1 MiB UTF-8 payload budget across stdout and stderr per feed,
then writes one additional `[truncated]` line (12 bytes). An explicit `print_callback` is
not capped; keep it fast or buffer its output.

### Limits

| `PydenoLimits` key | Maps onto | Notes |
|---|---|---|
| `max_feed_duration_secs` | the session's hard deadline (`AgentSandbox(timeout=)`) | Time suspended at an external call does not count. Default 30 |
| `max_turn_duration_secs` | the same deadline, `min` with the above | One V8 command runs a whole feed, so it is enforced over the feed: never weaker than Monty's |
| `max_memory` | the worker's `max_memory` | Fixed when a worker starts: a session asking for another value than its pool's gets a fresh worker (a cold start). Default 512 MiB |
| `max_suspensions` | the session's tool budget (`max_tool_calls`) | Default 1000; `None` keeps it, as in Monty. The call over budget throws a catchable `ToolBudgetError` in the guest (Monty's is uncatchable) |
| `max_host_wait_secs` | `max_pause` | pydeno only. Default 60 |
| `tool_timeout_secs` | `tool_timeout` | pydeno only. Default off. One external call that takes longer fails in the guest with a catchable `TimeoutError` (`host function timed out`) and the feed goes on; see [Per-call tool deadline](advanced/isolation.md#per-call-tool-deadline) |
| `max_total_sleep_secs` | nothing | Always satisfied: guest timers run on virtual time, a guest cannot sleep |
| `max_recursion_depth` | refused (`ValueError`) | V8 bounds recursion by stack size (a catchable `RangeError`); a flag cannot raise that limit safely |
| `gc_interval` | refused (`ValueError`) | V8 decides when to collect |

### What pydeno has that Monty does not

- Full JavaScript, on the engine behind Chrome and Node, so model-written code behaves like
  JavaScript, including `async`/`await`, classes, `BigInt`, typed arrays and regular expressions.
- Vendored libraries that run in the sandbox (Vega-Lite, ECharts, D3, three.js, Turf, dagre, a
  slide-deck writer): see [Vendored npm libraries](advanced/vendored-npm-libraries.md).
- An OS sandbox under the interpreter (Seatbelt; Landlock + seccomp), required by default, with a
  start-up self-test.
- Signed session state, and a crash or timeout that cannot refund an external-call budget.

### What Monty has that pydeno does not

- Type checking of each snippet (`type_check=`, stubs, formats).
- Mounts, an `os=` handler and `os_policy` (pydeno's guest has no filesystem at all; give it data
  through `inputs` or functions through `external_lookup`).
- A WebSocket transport (`AsyncMontyWebsocket`), `install_dependencies`, `ClassInstance` proxies,
  OpenTelemetry instrumentation.

## Errors

| Error | When | The session |
|---|---|---|
| `PydenoRuntimeError` | The feed threw (`.name`, `.message`), or its result could not cross the boundary | Survives |
| `PydenoSyntaxError` | The feed does not parse; nothing of it ran | Survives |
| `PydenoTimeoutError` | A deadline (`max_feed_duration_secs`, the CPU cap, `max_host_wait_secs`) killed the worker | Over |
| `PydenoCrashedError` | The worker is gone (over `max_memory`, a crash, a protocol violation), or could not start | Over |
| `PydenoError` | Base class; also state that does not load (`load_session` with a tampered or foreign dump) | |

```python
from pydeno import PydenoError, classify_error

try:
    session.feed_run(code)
except PydenoError as exc:
    info = classify_error(exc)          # kind="memory_limit", retryable=False, ...
```

## Relaxing the defaults

Each of these is a risk you take explicitly:

```python
Pydeno(sandbox="auto")                  # run with whatever OS sandbox the platform offers
Pydeno(jitless=False)                   # V8's JIT and WebAssembly: faster, a larger attack surface
Pydeno(limits={"max_memory": None})     # remove a limit
```

And one that tightens them: `Pydeno(strict_eval=True)` makes `eval` and `new Function` throw in the
guest (no code from strings at run time). Dumps record it and load only into a pool with the same
setting. See [strict eval](advanced/isolation.md#strict-eval-no-code-from-strings).

## Performance

`benches_py/alternatives_bench.py` on an Apple-silicon laptop (macOS, Python 3.14, medians with p95
in brackets, milliseconds, warm pools, a machine that was not idle, so numbers vary by about 20%
between runs). Since the security review's fixes the first feed also sends the clock freeze as a
command of its own (one round trip); every other feed costs what it did (measured A/B on one
machine: a feed is about 0.12 ms over the worker's own `eval_async` before and after):

| | `SandboxPool` (raw) | `Pydeno` | `AsyncPydeno` | Monty |
|---|---|---|---|---|
| checkout | 0.044 (0.07) | 0.11 (0.61) | 0.10 (0.61) | |
| checkout + first `feed_run("1 + 1")` | 1.8 (2.5) | 1.35 (1.7) + one plain `eval` round trip (about 0.1 to 0.3 ms) for the clock freeze | 1.45 (2.6) + the same | 0.04 to 0.15 (24) |
| one more `feed_run("1 + 1")` | 0.10 to 0.26 | 0.38 to 0.46 | 0.53 | 0.01 to 0.02 |
| checkout + 10 feeds + exit | 4.6 to 8.3 | 5.9 to 6.2 | 11.2 | 0.18 to 0.27 |
| session exit | | 0.05 | | |

How `Pydeno` gets there:

- **Nothing session-independent happens at checkout.** A pooled worker arrives with the session's
  dispatcher bound, the session prelude installed, the clock-freezing script compiled and its first
  runs done, all on the pool's background filler. A checkout adopts it and applies the session's
  limits and console routing in Python, with no round trip to the worker. The clock is frozen
  to the checkout's instant by a command of its own sent just before the first feed (one round
  trip, on the first feed only), so it holds even when that feed fails before any of it runs.
- **Feeds are driven from your thread.** `feed_run` runs the worker's command loop on the calling
  thread, which enforces every limit the whole time. External calls are answered on the
  **session's own** threads, never shared with another session: plain functions on the session's
  tool thread, coroutine functions on its event-loop thread (sync API), both started at the
  session's first external call (about 0.2 ms, once) and never reused by another session. Calls
  are answered one at a time, in the order the guest made them, each in a fresh copy of your
  context (contextvars set by one call are not seen by the next). A feed that calls nothing
  touches no other thread. An external that outlives `max_host_wait_secs` (or a guest that burns
  the CPU cap meanwhile) gets the worker killed and your thread released within about 0.1 s; the
  external is left to finish on its session's thread and its answer is discarded.
- **Thread-locals:** an external runs on the session's thread, not on yours, so it does **not**
  see your thread-local state (`threading.local()`), and nothing it leaves in thread-locals can
  reach another session. Pass per-request state through contextvars (copied per call) or the
  function's closure.
- **The refill waits.** Replacing a checked-out worker starts 50 ms after the checkout (at once if
  the pool is empty), because starting a process stalls the parent for about a millisecond, which
  would otherwise land on the session's first feed. This is why a `Pydeno` checkout plus its first
  feed is faster than a raw `SandboxPool` checkout plus one `eval`.
- **Exit kills the worker at once.** A background thread per pool reaps it.

What limits it:

- **The per-feed floor is the worker's async evaluation (about 0.3 ms).** A feed may `await`, so it
  is evaluated as an async command, and the worker starts a fresh event loop for every async
  command (`asyncio.run` in `_worker.py`). A plain `eval` takes about 0.1 ms; the front door's own
  Python costs about 0.05 ms per feed. A persistent event loop in the worker would close most of
  that gap.
- **An external function that never returns keeps its session's thread.** Its run is ended,
  your thread released and the session is over, so it cannot start another: one session leaves
  at most one wedged thread behind. Threads are capped **per pool**: `Pydeno(max_tool_threads=128)`
  (the default) counts, exactly:
  - a `PydenoSession`'s tool thread, from the first external call that needs it (a `feed_run`
    call, or `resume_auto`);
  - a `PydenoSession`'s loop thread, from the first external it serves in a `feed_run` (it is
    started uncounted by `feed_start`/`resume`, where no external runs on it);
  - an `AsyncPydenoSession`'s tool thread, from its first plain external.

  A session's threads end with it, also when it is dropped without `close()`. Past the cap, an
  external call that needs a thread is refused: the guest sees its call fail exactly like a tool
  raising a redacted `RuntimeError` (`host function failed`, nothing about the host), the call is
  journaled and charged like any failed tool call (so `dump()` / `load_session()` round-trip and
  `max_suspensions` holds), the host gets one log record per session (logger `pydeno`; the rest are
  counted and reported when the session closes) and, if the feed then fails,
  `ToolThreadLimitError` (a `PydenoError`). A process-wide ceiling (`pydeno._agent.MAX_TOOL_THREADS`,
  512) stays behind every pool, and a pool's cap is clamped to it. **Caps are upper bounds, not
  reservations:** pools draw from the shared ceiling first come, first served, so when the open
  pools' caps add up to more than the ceiling (`Pydeno()` warns), a pool can be refused before
  reaching its own cap while others hold the threads. A fork()ed child starts from zero. Give your
  externals their own timeouts.
- **Console output (`print_callback`)** runs on the session's own thread too: in a `PydenoSession`
  on your thread (the feed's own), in an `AsyncPydenoSession` on a console thread of the session's
  (started at its first console call, uncounted: one per session), so one tenant's slow sink holds
  up only its own session.
- **`AsyncAgentSandbox` (the older class) still runs plain tools and its console sink on a thread
  pool shared by every session in the process** (32 threads, or your `handler_executor`): tools or
  sinks that block there can starve other sessions. `AsyncPydeno` does not have this limit; pass a
  `handler_executor` per tenant if you use `AsyncAgentSandbox` directly.
- **The first command after a worker has sat idle is slower** (0.3 to 1 ms on macOS) whatever
  sends it.
- Monty is faster again: its workers are reused between sessions (pydeno's are single-use, by
  design) and it runs an interpreter rather than V8.

`min_processes=2` is deliberate. One worker serves the next checkout while its replacement starts
in the background. Each worker holds about 30 to 40 MB. Raise it for bursts of concurrent checkouts.

Run `python benches_py/alternatives_bench.py pydeno-front` for numbers on your machine.

## Advanced

`Pydeno` is built from the classes below, which stay available, unchanged, for what the front door
does not expose: [`AgentSandbox`](agent-sessions.md) (named tools, schema tools, a lazy tool
catalog, `execute()` results), [`SessionPool`](advanced/async-agent-sessions.md) (persistent
multi-tenant sessions), [`IsolatedRuntime` and `SandboxPool`](advanced/isolation.md) (the raw
sandboxed runtime) and the in-process `Runtime` (trusted code only).
