# Async isolated runtime (asyncio services)

`AsyncIsolatedRuntime` is [`IsolatedRuntime`](isolation.md) for code that lives on an asyncio event
loop: a web service, a job runner, an agent host with hundreds or thousands of concurrent sessions.
It runs the guest in the same supervised, sandboxed worker process, with the same limits and the
same trust model, but the parent side is driven by the event loop instead of by threads.

```python
from pydeno import RuntimeConfig, WorkerCrashed, RuntimeTimeout
from pydeno import AsyncIsolatedRuntime

async def handle(request):
    async with AsyncIsolatedRuntime(RuntimeConfig(timeout=2.0), sandbox="require") as rt:
        await rt.bind_function("lookup", lookup_price)      # sync or async
        try:
            return await rt.eval(request.code)
        except (WorkerCrashed, RuntimeTimeout):
            return "the guest was stopped"                  # your process is fine
```

## Why not one thread per runtime

`IsolatedRuntime` was built for a thread. Each runtime has an idle-watchdog thread; `eval_async`
runs the command on a thread the runtime owns (so a slow tool cannot starve the application's
default executor); and the constructor, `bind_function`, `eval` and `close` block the calling thread.
Used from an event loop, that means either a blocked loop or `asyncio.to_thread` everywhere, and
one to three threads per session. A thousand sessions is a thousand-plus threads, each with its own
stack, all contending for the GIL.

`AsyncIsolatedRuntime` keeps none of that per runtime:

| | `IsolatedRuntime` | `AsyncIsolatedRuntime` |
|---|---|---|
| Worker pipes | blocking reads/writes on a thread | asyncio transports on the caller's loop |
| A command | runs on the runtime's own thread | a coroutine on the caller's loop |
| Limits (deadline, CPU, memory, threads, idle CPU) | the command thread + one watchdog thread per runtime | **one supervisor task per event loop**, for every runtime on it |
| Resource reads (`/proc`, `proc_pidinfo`) | on those threads | batched on **one** shared metrics thread |
| Worker start (`fork`/`exec`) | on the caller's thread | a small shared pool (2 threads), at most `cpu_count // 2` start-ups in flight per loop |
| Sync host functions | inline on the command thread | a shared handler pool (32 threads by default; `handler_executor=` to bring your own) |
| Async host functions | scheduled onto the caller's loop from the command thread | awaited directly on the loop |

The shared pools do not grow with the number of runtimes: 50 runtimes add fewer than five threads
to the parent (tested), and the thread count stays flat from there.

## Usage

```python
rt = await AsyncIsolatedRuntime.create(config, max_memory=256 * 2**20)   # for pools
try:
    await rt.bind_function("search", search)          # -> capability token
    await rt.bind_object("api", {"version": "1", "get": get})
    await rt.add_static_module("util", "export const twice = x => 2 * x;")
    await rt.set_module_resolver(resolve)              # (specifier, referrer) -> str | None
    await rt.set_module_loader(load)                   # (specifier) -> str, sync or async
    value = await rt.eval("search('x').then(r => r.length)")
    ns = await rt.eval_module("static:util")
    await rt.revoke_op(token)
finally:
    await rt.close()                                   # idempotent; kills after 1 s
```

- **Constructing** the object validates the options and starts nothing. The worker is started by
  `async with` or `await AsyncIsolatedRuntime.create(...)`, off the loop, and the runtime is then
  bound to that event loop.
- **Every option** of `IsolatedRuntime` is accepted with the same meaning and default
  (`max_memory=1 GiB`, `request_timeout`, `timeout_grace`, `max_host_calls`, `max_host_wait`,
  `max_inflight_host_calls`, `write_stall_timeout`, `redact_host_errors=True`, `sandbox`,
  `empty_root`, `jitless`, `v8_flags`, `strict_eval`, `clock`, `random_seed`, `python`, `prewarm`), plus
  `handler_executor`. `rt.sandbox`, `rt.sandbox_extras` and `rt.v8_flags` report what is in force.
- **`eval` and `eval_module` await promises** (they are `IsolatedRuntime.eval_async` and
  `eval_module_async`; `rt.eval_async` / `rt.eval_module_async` are aliases, so code written for
  `IsolatedRuntime` keeps working). Each takes `timeout=` for a soft per-call limit.
- **Host functions** may be plain or `async def`. A coroutine function is awaited on the loop and can
  run concurrently with others; a plain function runs on the handler pool, so it may block (a
  database driver, `requests`) without stalling the loop. Either way it sees the **caller's
  contextvars** (request ids, tracing spans), as with `asyncio.to_thread`.
- A host function may not call back into **its own** runtime (that would wait on the command it is
  part of); it raises `RuntimeError` in the guest. Calling a *different* runtime is fine.
- **A sandbox per request?** `AsyncSandboxPool` keeps started, single-use runtimes ready, so a
  checkout costs microseconds instead of a cold start (see
  [A pool of ready workers](isolation.md#a-pool-of-ready-workers-sandboxpool)).

## Cancellation

Cancelling a command that has been sent (`task.cancel()`, `asyncio.timeout(...)`, a client that
disconnects) **kills the worker and closes the runtime**; the `CancelledError` propagates as usual.

That is deliberate. V8 cannot be interrupted from outside in the middle of a command, and a worker
left running a command nobody waits for would answer the *next* command with the previous one's
frames. `IsolatedRuntime` does the same for `eval_async`. A command that was only waiting for its
turn (another command on the same runtime was running) is cancelled without harm.

**If you want a limit that keeps the runtime alive, pass `timeout=`** (or `RuntimeConfig(timeout=)`):
the guest gets a catchable timeout inside the worker and the session continues. Use cancellation
for "stop it, whatever it costs".

## Security: what is the same

Everything in [the isolation guide](isolation.md) applies unchanged, and is tested again for this
class (`tests/test_aio_isolated_runtime.py`, with the same hostile fake workers):

- every frame is untrusted; the 16 MiB frame cap is checked as soon as the length header arrives,
  **before** any of the payload is buffered; malformed, oversized, out-of-protocol or dripped frames
  kill the worker and raise `WorkerCrashed` (or `RuntimeTimeout` for the drip);
- a capability token the worker reuses, or a token map that does not match what was bound, ends the
  session; a call for a handler id it was never given ends the session; a call that was already in
  flight when the host revoked its capability gets an error, not a kill;
- the hard deadline (paused while a host function runs; console output pauses it only within one
  deadline per command, and not at all beyond that), `max_host_wait`, the per-command CPU cap
  (twice the hard deadline), the memory ceiling, the thread cap (64), idle-CPU supervision,
  `max_host_calls`, the runtime-wide `max_inflight_host_calls`, the write-stall timeout, error-text
  sanitising, `redact_host_errors`, argument checks on module resolvers/loaders and `on_console`,
  `sandbox="require"` (including refusing to start where memory/CPU/threads cannot be measured);
- a worker of a runtime that is dropped without `close()` is killed by a finalizer; workers of a loop
  that shuts down are killed and reaped with it; anything left at interpreter exit is killed;
- after `fork()` the child forgets the parent's runtimes: it cannot signal, sample, talk to or kill
  them, and using one raises `RuntimeError`.

Two places where this class is *stricter* than `IsolatedRuntime`:

- `max_host_wait` and the memory ceiling also fire while a **synchronous** host function is still
  running. (`IsolatedRuntime`'s command thread is inside that function and cannot notice; its
  watchdog covers memory but not the wait cap.)
- The CPU baseline for a command is the latest reading (at most 250 ms old) rather than one taken
  right before sending, so a command can only be charged for slightly *more* CPU, never less.

## Limits and caveats

- **Loop latency under load is the loop's, not a block.** Nothing here blocks the loop: worker
  start-up, resource reads, reaping and large-frame codec work happen off it, and writes wait with
  `drain()` under the write-stall timeout. But a single loop serving 64 sessions that are all
  evaluating as fast as they can has ~64 events to handle per iteration, and on an 8-core machine 64
  busy workers compete with the loop for CPU. Measured on an M-series Mac: worst heartbeat lag
  ~3-4 ms at 32 busy sessions, ~10-20 ms at 64 (an idle asyncio process next to 64 unrelated
  CPU-bound processes shows 10-16 ms on the same machine). The thread-based path measured 116-335 ms
  for the same scenarios. See `benches_py/async_core_bench.py`.
- **Spawning can hold the GIL briefly.** Workers are started on a thread, but CPython holds the
  GIL across `fork()`, and on Python 3.10 and 3.11.0 to 3.11.5 also while a `vfork()`ed child
  execs. Each spawn can delay the loop by a few milliseconds. On Linux, with the parent and two
  CPU hogs pinned to one CPU, 40 spawns measured 5 to 15 ms worst heartbeat lag on 3.10, the same
  as with plain `fork()`. Start-ups are limited per loop (`os.cpu_count() // 2` in flight, which
  counts the machine's CPUs, not a container's CPU quota). The prewarmed spare (shared with
  `IsolatedRuntime`) takes one spawn off the critical path.
- **Bursts on a small or busy machine are as slow as the CPU allows.** A worker start-up costs
  about 55 ms of CPU, and a shutdown costs CPU too. Starting or stopping 16 to 64 at once on 2
  CPUs keeps the loop thread waiting for a CPU, as any burst of process start-ups would. This was
  measured with `benches_py/loop_stall_bench.py` (#83) in Linux aarch64 containers with
  `--cpus 2` and `--cpus 4` on a busy 4-vCPU VM, Python 3.10 and 3.14. The bursts were creating,
  closing, timing out and crashing runtimes, `AsyncSandboxPool` cold checkouts and `AsyncPydeno`
  sessions. Over 262 bursts, the heartbeat's p99 was 2 to 190 ms and the worst beat 3 ms to
  1.1 s. With nothing running, the same loop measured p99 2 to 15 ms and worst 7 to 33 ms. The
  same runs' no-pydeno control was 16 to 64 plain processes burning 0.1 s of CPU each. It measured
  p99 6 ms to 1.6 s and worst 33 ms to 5.3 s. In beats later than 20 ms, the loop thread spent a
  median 65% of the lost time runnable but without a CPU (its run-queue wait) and 8% on average
  running Python. The rest was GIL waits or time the hypervisor took. Stack samples taken during
  those beats were 80 to 90% in the selector's `select()`, and no pydeno function recurred.
  0.8.0 and 0.9.0 measured the same. Nothing blocking runs on the
  loop: `tests/test_aio_isolated_runtime.py` checks that spawns, blocking waits, resource reads,
  stderr reads and sleeps all happen on other threads. If a latency budget matters, keep a pool
  warm (`AsyncSandboxPool` / `AsyncPydeno`) so that bursts are checkouts and not start-ups, and
  give the process the CPUs that its bursts need.
- **Encoding a multi-MiB value holds the GIL.** Frames over 64 KiB are encoded/decoded on a thread,
  but the native codec holds the GIL while it builds Python objects (decoding) or serialises them
  (encoding, ~8 ms per MiB of string), so a 16 MiB `eval` source still costs the loop that long.
  Keep large payloads out of the hot path (a static module, loaded once, is better than a huge
  `eval`).
- **A wedged synchronous host function keeps a handler thread.** It cannot be interrupted; the
  guest's command ends at `max_host_wait` and the worker is killed, but the thread stays busy until
  the function returns. Size `handler_executor` for your slowest tools, or make them `async`.
- **One loop per runtime.** A runtime is bound to the loop it was started on; using it from another
  loop raises `RuntimeError`.
- **`ToolBridge.attach` is synchronous** and does not accept this class yet; bind the bridge's tools
  with `await rt.bind_object(namespace, tools)` instead.
- Like `IsolatedRuntime`: POSIX only, values cross as plain data, no streams, snapshots or
  inspector.

## Agent sessions

`AsyncAgentSandbox` and `SessionPool` build pausable, durable agent sessions on this runtime: see
[Async agent sessions and the session pool](async-agent-sessions.md).

## Import

```python
from pydeno import AsyncIsolatedRuntime
```

## Host reply backpressure

Concurrent host replies wait before encoding and writing to the worker. The transport buffer
can grow by at most one frame above its high watermark. A host call stays in flight until its
reply drains, so the default `max_inflight_host_calls=64` also bounds waiting reply producers.
Setting that limit to `None` explicitly removes the producer cap; a trusted host callback’s
returned Python value can still consume arbitrary memory.

Each reply gets its own `write_stall_timeout` window, which starts when the reply is written: a
worker that keeps reading through a burst is not killed because the whole burst takes longer than
the timeout, and a worker that does not drain one reply (at most that frame plus the 64 KiB high
watermark) within the timeout is killed, however slowly it is still reading.
