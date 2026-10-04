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
| `max_host_wait_secs` | 600 s of waiting on external calls per feed |
| Workers | Single-use: a worker never serves a second session; it is killed when the `with` block exits |
| Guest globals | No `Deno`, `process`, `require`, filesystem, network, `SharedArrayBuffer`, `Atomics`, `WeakRef`; a frozen clock and a seeded `Math.random` of the session's own |
| Pool | `min_processes=2` workers started ahead of time (about 30 to 40 MB each); the first starts in the constructor, so a machine that cannot sandbox fails there |

## Monty to pydeno

The same names, mapped onto pydeno's building blocks (`SandboxPool` underneath, every session an
`AgentSandbox` on a checked-out `IsolatedRuntime`).

| Monty | pydeno | What differs |
|---|---|---|
| `Monty()` / `AsyncMonty()` | `Pydeno()` / `AsyncPydeno()` | Workers are single-use, so there is no `max_processes`, `max_checkouts_per_worker` or `checkout_timeout`: an empty pool starts a worker on the spot (a cold start, never an error, never a wait). `min_processes` is the number kept ready. `Pydeno()` starts its first worker in the constructor; `AsyncPydeno` on `async with` |
| `pool.checkout(script_name=, limits=)` | `pool.checkout(script_name=, limits=)` | No type checking, `os_policy` or `print_flush_interval` (see below) |
| `MontySession` / `AsyncMontySession` | `PydenoSession` / `AsyncPydenoSession` | `session_id` is always `None` (as for Monty's local workers) |
| `session.feed_run(code, inputs=, external_lookup=, print_callback=)` | the same | JavaScript: a feed's result is its trailing expression (or what it `return`s); a top-level assignment (`x = 1`) has none, as in Monty. Feeds may `await`. A name must be in `external_lookup` to be callable (JavaScript cannot intercept undefined names), and external calls return promises: `await fetch(1)`. `inputs` are plain data (None, bool, int, float, str, list, dict) |
| Sync session refuses async externals | the same (`RuntimeError`) | |
| `print_callback(stream, text)` | the same | `console.log/info/debug` is `"stdout"`, `warn/error/trace` is `"stderr"`. The default prints to this process with control characters replaced. No `CollectStreams` / `CollectString`: pass a callable |
| `session.feed_start(...)` -> `FunctionSnapshot` / `MontyComplete` | `PydenoSnapshot` / `PydenoComplete` (`AsyncPydenoSnapshot` for asyncio) | No name-lookup or future snapshots: a JavaScript call is always a function call. `kwargs` is always `{}` |
| `snapshot.resume({'return_value': v})` / `({'exception': e})` / `({'exc_type': ..., 'message': ...})` | the same, plus `resume(value=v)` / `resume(error=e)` | `{'future': ...}` is refused: answer with a value or an error |
| `snapshot.resume_auto()` | the same | |
| `snapshot.dump()` / `session.dump()` | the same | The bytes are the agent sandbox's journal, **HMAC-signed** with the pool's `dump_key` (random per pool by default; pass your own to load in another process). Monty's dumps are unauthenticated |
| `session.load_session(state)` / `load_snapshot(state, external_lookup=, print_callback=)` | the same | Restored by **deterministic replay** on a fresh worker (V8 cannot serialise a live heap): recorded external answers are replayed, your functions are not called again. Works on a session whose worker died, and `dump()` after a crash returns the state as of the last good feed |
| `session.worker_pid` | the same | |
| `ResourceLimits` | `PydenoLimits` | See the table below |
| `MontyError`, `MontyRuntimeError`, `MontySyntaxError`, `MontyCrashedError` | `PydenoError`, `PydenoRuntimeError`, `PydenoSyntaxError`, `PydenoCrashedError` | `exception()` returns the pydeno exception underneath, and `classify_error()` classifies it. `display('traceback' \| 'type-msg' \| 'msg')` and `traceback()` exist; the worker reports no JavaScript stack yet, so `'traceback'` is usually `'type-msg'` |
| Timeouts | `PydenoTimeoutError` | A `TimeoutError` and a `PydenoCrashedError` (`timed_out=True`): pydeno's deadline kills the worker, so the session is over. Monty raises inside the sandbox and keeps the session; pydeno chose the kill because a V8 that is told to stop is not always able to (see the security report) |

### Limits

| `PydenoLimits` key | Maps onto | Notes |
|---|---|---|
| `max_feed_duration_secs` | the session's hard deadline (`AgentSandbox(timeout=)`) | Time suspended at an external call does not count. Default 30 |
| `max_turn_duration_secs` | the same deadline, `min` with the above | One V8 command runs a whole feed, so it is enforced over the feed: never weaker than Monty's |
| `max_memory` | the worker's `max_memory` | Fixed when a worker starts: a session asking for another value than its pool's gets a fresh worker (a cold start). Default 512 MiB |
| `max_suspensions` | the session's tool budget (`max_tool_calls`) | Default 1000; `None` keeps it, as in Monty. The call over budget throws a catchable `ToolBudgetError` in the guest (Monty's is uncatchable) |
| `max_host_wait_secs` | `max_pause` | pydeno only. Default 600 |
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

## Performance

`benches_py/alternatives_bench.py` on an Apple-silicon laptop (macOS, Python 3.14, medians with p95
in brackets, milliseconds, warm pools, a machine that was not idle):

| | `SandboxPool` (raw) | `Pydeno` | Monty |
|---|---|---|---|
| checkout | 0.044 (0.061) | 2.65 (3.70) | |
| checkout + first `feed_run("1 + 1")` | 1.58 (1.95) | 4.07 (5.68) | 0.04 (23.7) |
| one more `feed_run("1 + 1")` | 0.09 (0.14) | 0.50 (0.60) | 0.01 (0.02) |
| checkout + 10 feeds + exit | 4.62 (7.67) | 10.3 (11.5) | 0.18 (0.83) |
| session exit | | 0.42 (0.55) | |

What the front door adds over a raw pool checkout is the agent session (`AgentSandbox`) that
gives it journals, replay, budgets and console capture: a binding and the session prelude (two
round trips to the worker, which also freeze the clock at checkout), and per feed the session's run
wrapper. The front door's own work per feed (preparing the code, finding its trailing expression)
is about 8 µs for a small feed. A session's exit SIGKILLs its worker at once and leaves the reaping
to one background thread per pool. Monty is faster still: its workers are reused between sessions
(pydeno's are single-use, by design) and run an interpreter rather than V8.

Run `python benches_py/alternatives_bench.py pydeno-front` for numbers on your machine.

## Advanced

`Pydeno` is built from the classes below, which stay available, unchanged, for what the front door
does not expose: [`AgentSandbox`](agent-sessions.md) (named tools, schema tools, a lazy tool
catalog, `execute()` results), [`SessionPool`](advanced/async-agent-sessions.md) (persistent
multi-tenant sessions), [`IsolatedRuntime` and `SandboxPool`](advanced/isolation.md) (the raw
sandboxed runtime) and the in-process `Runtime` (trusted code only).
