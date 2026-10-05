# Isolated runtime (running code you do not trust)

`Runtime` runs V8 inside your Python process. That is fast, and it is enough when guest code
is merely buggy. It is **not** enough for code you do not trust, for three reasons:

1. Some JavaScript makes V8 abort the whole process, or sit in a native builtin that `timeout=`
   cannot interrupt:

    ```python
    rt = Runtime(RuntimeConfig(timeout=1.0, max_heap_size=64 * 1024 * 1024))
    rt.eval("new Array(2 ** 32 - 1).fill(0)")                  # host process aborts
    rt.eval("const a = []; a[2 ** 32 - 2] = 1; a.sort()")      # ignores timeout=
    ```

2. V8 is millions of lines of C++ with a steady record of sandbox-escape bugs, and a guest that
   escapes it gets your process's full authority.
3. Several memory sinks (`WebAssembly.Memory`, resizable buffers) sit outside every in-process
   limit.

`IsolatedRuntime` answers all three. The guest runs in a worker **process** that the parent
supervises and can kill, with the same layered thinking as
[pydantic/monty](https://github.com/pydantic/monty), plus an OS sandbox and a JIT-free V8 that
Monty has no counterpart for:

```python
from pydeno import IsolatedRuntime, RuntimeConfig, RuntimeTimeout, WorkerCrashed

with IsolatedRuntime(RuntimeConfig(timeout=2.0), sandbox="require") as rt:
    rt.bind_function("lookup", lambda sku: {"A1": 3.5}[sku])
    print(rt.eval("lookup('A1') * 2"))        # 7.0
    print(rt.sandbox)                         # "seatbelt" or "landlock+seccomp"

    try:
        rt.eval("new Array(2 ** 32 - 1).fill(0)")
    except (WorkerCrashed, RuntimeTimeout):
        ...                                    # your process is fine; start a new runtime
```

It is safe by default: a 1 GiB memory ceiling, a 60 s hard deadline, the OS sandbox and
`--jitless` are all active unless you remove them. **Pass `sandbox="require"` in production**, so
a kernel that cannot apply the sandbox refuses to start instead of quietly running without it.

## Make it the default

If your code uses the module-level helpers (`pydeno.eval()`, `pydeno.bind_function()`, ...), switch
them to the isolated runtime once, at start-up, and the easy path becomes the safe one:

```python
import pydeno

pydeno.configure_default_runtime(isolated=True, sandbox="require")
pydeno.eval("1 + 1")        # runs in a sandboxed worker; per task or thread, as before
```

`isolated_options` are the keyword arguments of `IsolatedRuntime`. A crash closes that task's
runtime and the next call quietly gets a fresh one. Only runtimes created after the call are
affected.

## The layers

| Layer | What it stops |
|---|---|
| **Process boundary** | A V8 abort, OOM or uninterruptible loop kills the worker, not you |
| **Hard deadline** (`request_timeout`) | A wedged worker is `SIGKILL`ed; host callbacks do not count against it |
| **Memory ceiling** (`max_memory`) | A runaway worker is killed: by the worker itself every ~20 ms *and* by the parent every ~50 ms |
| **OS sandbox** (`sandbox=`) | A guest that escapes V8 still has no filesystem, network, new processes, or other process to touch |
| **No privileges** | A worker started as root becomes `nobody` with an empty capability set |
| **`--jitless` V8** (`jitless=`) | No JIT compiler (the source of most V8 exploits) and no WebAssembly |
| **Strict eval** (`strict_eval=True`, opt-in) | No code created at run time from strings: `eval` and `new Function` throw |
| **Empty environment, UTC, no core dumps** | No host secrets, no host timezone/locale, no memory dumps |
| **Untrusted-frame decoder** | A compromised worker cannot crash, stall or exhaust the parent |
| **`max_host_calls`** | An endless stream of cheap host calls cannot dodge the deadline |
| **Frozen clock, seeded random** (`clock=`, `random_seed=`) | No wall-clock timing source; reproducible runs |

### The OS sandbox

Applied inside the worker **before** the isolate exists, so every thread V8 and tokio spawn
inherits it. It cannot be lifted from inside the process.

- **macOS**: a deny-by-default Seatbelt profile. No files, no network, no fork/exec, signals only
  to itself.
- **Linux**: first, where unprivileged user namespaces are allowed, a private mount namespace whose
  root is an **empty tmpfs** (what bubblewrap does), with empty network and IPC namespaces too.
  Then Landlock (no filesystem access, no TCP, no abstract unix sockets or signals outside itself
  on kernels that support those scopes) plus a seccomp-bpf filter. Read `rt.sandbox_extras` to see
  whether the empty root took effect (`["emptyroot"]`) and pass `empty_root=False` to skip it. The
  seccomp filter denies, with `EPERM`:
    - new processes and other-process access: `execve`, `fork`, non-thread `clone`, `ptrace`,
      `process_vm_*`, `pidfd_*`, `kcmp`;
    - the network: `socket`, `connect`, `bind`, `listen`, `accept` (asyncio's `socketpair` stays);
    - mounts and namespaces: `mount`, `pivot_root`, `setns`, `unshare`, the new mount API;
    - the kernel: `bpf`, `perf_event_open`, `userfaultfd`, `io_uring_*`, `keyctl`, modules,
      `kexec`, `personality`, `quotactl`;
    - IPC with the host user's other processes: SysV queues, semaphores and shared memory, POSIX
      message queues, `inotify`, `fanotify`;
    - file *metadata*, which Landlock does not govern: `chmod`, `chown`, `utime`, extended
      attributes;
    - identity and time: `setuid` and friends, `capset`, `settimeofday`, `clock_settime`,
      `sethostname`, `sync`;
    - acting on another process by pid: `kill`, `tgkill`, `sched_set*`, `prlimit64`, `setpriority`
      and `migrate_pages` are allowed on the worker itself and denied on anything else.
- **Elsewhere, or where the kernel lacks the feature**: nothing is applied. `sandbox="auto"`
  (the default) carries on and `rt.sandbox` says `"none"`; `sandbox="require"` refuses to start.

The seccomp filter is test-fired in a throwaway child first and skipped (not applied, not
fatal) if it would kill the process, which is what happens under architecture emulation such as an
x86_64 image on Apple silicon.

**What is still visible:**

- **Linux with the empty root** (`sandbox_extras == ["emptyroot"]`; needs unprivileged user
  namespaces): nothing. Every host path answers `ENOENT`, so the worker cannot even tell what is
  installed.
- **Linux without it** (user namespaces disabled, as in many hardened or nested-container setups):
  a sandboxed worker can learn that a path *exists* and `stat` it (size, times, owner), because
  Landlock does not govern metadata.
- **macOS**: existence only. `stat` of an existing path is refused (`EPERM`), but a missing path
  answers `ENOENT` before any sandbox check, which still tells the two apart.

No platform lets it read, write, create, delete, rename, `chmod` or `chown` anything, or watch it.
The exact behaviour is pinned by tests, so this cannot drift from reality.

### `--jitless`

V8's JIT compilers are where most of its exploitable bugs live, and an interpreter-only V8 is a
much smaller target. The cost is speed on hot compute loops (about 3x on a recursion
microbenchmark) and **no WebAssembly**. If you need either, pass `jitless=False`; the other layers
still apply, and `max_memory` still bounds WebAssembly memory. To run a trusted compiled module
next to the guest, `rt.load_wasm(...)` loads it from the host (only with `jitless=False`; see
[WebAssembly](webassembly.md)).

`v8_flags=[...]` passes extra flags to the worker. Unknown flags are an error, not ignored.

### Strict eval: no code from strings

```python
IsolatedRuntime(config, strict_eval=True)       # also AsyncIsolatedRuntime, SandboxPool,
Pydeno(strict_eval=True)                        # AgentSandbox, AsyncPydeno, ...
```

`strict_eval=True` forbids code generation from strings in the guest. `eval("...")`, indirect
`(0, eval)(...)`, `new Function(...)` and the async, generator and async-generator function
constructors all throw `EvalError: Code generation from strings disallowed for this context`,
however the guest reaches them: through `(function(){}).constructor`, `[].constructor.constructor`,
`Reflect.construct(Function, ...)`, a subclass of `Function`, or an alias of `eval` taken before
shadowing it. The host's own `eval` / `execute` of a script, the bootstrap, and modules the host
registers still run. It is off by default.

It is V8's `--disallow-code-generation-from-strings`, appended after the hardening flags and frozen
with them (`--freeze-flags-after-init`), so the guest cannot switch it back on. With
`strict_eval=True`, `v8_flags` that switch either flag off (`--no-disallow-code-generation-from-strings`,
`--no-freeze-flags-after-init`) are refused; `rt.strict_eval` says whether it is in force.

**What it buys.** Code you trust cannot be made to turn data into code at run time: a string
from a tool result, a user, or a model's output that reaches an `eval` or `new Function` in that
code (yours, or a library's) throws instead of running. It is a guard for trusted code against
injection, a hardening of the guest's behaviour, and **not a boundary against hostile guest code**:
a guest that wants to run code it builds from data can ship its own interpreter written in
JavaScript, which needs no `eval`. Against hostile code the boundary is the worker process and the
OS sandbox, as everywhere else on this page.

**What it does not buy.**

- It removes **no engine code**. The parser, interpreter and every builtin are still there, and the
  guest's own script is compiled as before. The OS sandbox and the process boundary stay the
  containment.
- **WebAssembly is not covered.** With `jitless=False` the guest can still compile and run Wasm
  bytes in strict mode. With the default `jitless=True` there is no `WebAssembly` at all.
- **`import()`** is not code generation to V8. It is the module loader's decision in either mode:
  pydeno refuses every specifier the host did not register. A host resolver/loader that returns
  source text chosen by the guest would create code from data despite strict mode.
- **`setTimeout("code")`** never compiles its string anyway: `WEB_POLYFILLS` ignores a string
  argument, and a bare isolate has no `setTimeout`.

**It is a worker-spawn option.** A pool gives it to every worker it starts (including the cold
starts) and refuses it per checkout. A session records it in its journal (only when it is on, so a
default journal is unchanged), and loading a journal into a session with the other setting raises
`JournalError` (`PydenoError` at the front door) before any guest code runs.

**Libraries.** Measured on the vendored set: ECharts server-side rendering, d3, turf, three.js
(with `GLTFExporter`), dagre and pptxgenjs work unchanged. Vega and Vega-Lite compile their
expressions with `Function` and fail with `EvalError`; with Vega's CSP-safe expression interpreter
(`vendor/libs/vega-interpreter-2.3.2.bundle.js`, loaded after Vega) they work:

```javascript
// load vega, vega-lite, then the interpreter bundle (it sets vega.expressionInterpreter)
const runtime = vega.parse(vegaLite.compile(spec).spec, null, {ast: true});
const view = new vega.View(runtime, {renderer: 'none', expr: vega.expressionInterpreter});
const svg = await view.toSVG();
```

The interpreter needs `setTimeout` to exist when it loads (use `WEB_POLYFILLS`).

### Frozen clock and seeded random

```python
IsolatedRuntime(config, clock=datetime(2026, 1, 1, tzinfo=timezone.utc), random_seed=7)
```

`Date.now()`, `new Date()` and `Intl.DateTimeFormat#format()` then never advance, and
`Math.random` is reproducible. The original `Date` is not reachable from the guest. A guest can
still *count* loop iterations, so this narrows the timing channel rather than closing it.

## Failure semantics

| Event | Result |
|---|---|
| JS exception, soft `timeout=`, heap limit | the same exceptions as `Runtime` |
| V8 abort, OOM, fatal signal | `WorkerCrashed`; the runtime is closed |
| Uninterruptible native loop | `SIGKILL` at the hard deadline: `RuntimeTimeout` |
| Over `max_memory` | worker exits with a dedicated code: `WorkerCrashed` (names `max_memory`) |
| Over `max_host_calls` | worker killed: `WorkerCrashed` |
| Malformed, oversized or stalled frame from the worker | worker killed: `WorkerCrashed` / `RuntimeTimeout` |

A crashed, killed or timed-out runtime is **closed**; create a new one, since its heap cannot be
trusted. Ordinary JavaScript errors and soft timeouts leave it usable.

### Deadlines

- `request_timeout=` is a hard wall-clock limit per command (default: the command's `timeout`
  plus `timeout_grace`, or 60 s when it has none; `None` removes it).
- Time spent running *your* host functions is not charged, so a slow tool does not trip it.
  The guest's own time still accumulates, and `max_host_calls` caps the number of callbacks.
- The limits are checked on a clock, not only when the pipe goes quiet, so a guest that hammers a
  host function in a tight loop cannot switch them off.

### Memory

`max_memory` (default 1 GiB, `None` removes it) bounds the worker's resident memory: V8 heap,
`ArrayBuffer`s, WebAssembly, resizable buffers, everything. `max_heap_size` and
`max_buffer_bytes` on the `RuntimeConfig` give earlier, catchable limits inside it.

| Limit | Bounds | Effect |
|---|---|---|
| `max_heap_size` | the JS heap | the runtime is terminated |
| `max_buffer_bytes` | live `ArrayBuffer` / `SharedArrayBuffer` bytes, resizable ones included (their committed size) | a catchable `RangeError` |
| `max_memory` | worker RSS | the worker is killed |

The resizable-buffer charge wraps the built-ins when the runtime starts. A host `SnapshotBuilder`
bootstrap runs before that, so a native `ArrayBuffer` constructor or `resize`/`grow`/`transfer` it keeps a
reference to (and hands to the guest) is not charged. Snapshot code is host code; do not expose such a
reference to guest code. (`IsolatedRuntime` refuses snapshots.)

## What works across the boundary

`eval`, `eval_async`, `bind_function`, `bind_object`, `revoke_op`, `add_static_module`,
`set_module_resolver`, `set_module_loader`, `eval_module`, `eval_module_async`, `on_console`,
`ToolBridge`, async host functions (running concurrently, like in-process), and
`None`/`bool`/`int` (incl. BigInt)/`float`/`str`/`bytes`/`list`/`dict`/`set`/`datetime`/`undefined`.

Not supported yet: streams, snapshots, the inspector, function handles, Windows.

### One result instead of an exception

`execute(code)` (and `await execute_async(code)`) evaluates like `eval` but returns an
`ExecutionResult(status, stdout, stderr, result, error, error_type, truncated)` and never raises
for the run's own failure, a crash or a timeout included. Create the runtime with
`capture_console=True` (or an `on_console`) to collect `console.*` into `stdout`/`stderr`; each
stream is capped at `max_output_bytes` (default 64 KiB) and ends with a `[truncated]` line past
it. A result larger than `max_result_bytes` (default 1 MiB of JSON) is a `Failed` result with
`error_type="ResultTooLarge"`. See [Agent sessions](../agent-sessions.md#results-and-console-output)
for the exact rules.

## Trust model

Copied from Monty: the worker starts with an **empty environment**, every frame it sends is
validated (size, nesting depth, node count, a closed set of value tags; nothing is ever
unpickled; error text is length-capped; an unknown exception class cannot be named), and a worker
that breaks the protocol, drips a frame, floods host calls, or asks for a host function it was never
given is killed. The decoder is fuzzed (`tests/test_isolated_runtime.py::TestWireFuzz`).

Beyond Monty: the OS sandbox, privilege drop and JIT-free V8 above, which exist because pydeno
cannot argue that a V8 escape is impossible the way a memory-safe interpreter with no I/O can.
Your own host functions still run with your full authority; validate their arguments, as with
Monty.

## How it is tested

- Behaviour, containment, leaks, limits, determinism:
  `tests/test_isolated_runtime.py`, `test_isolated_lifecycle.py`, `test_isolated_determinism.py`.
- **Assume-breach, from the inside:** `tests/test_redteam_syscalls.py` plays a compromised worker
  and fires every dangerous syscall from a sandboxed process, requiring `EPERM`;
  `scripts/redteam_syscalls.py` sweeps all ~350 syscalls and is how most of the filter was found.
  Run it only inside a container (`--network none --cap-drop all`).
- **Every number in the filter** is checked against the kernel's own tables
  (`tests/test_sandbox_syscall_tables.py`), so the x86_64 half is verified without x86_64 hardware.
- **Many Linuxes:** `scripts/linux_matrix.sh WHEELS IMAGE [PROFILE]` (podman or docker) runs the
  suites in any distro image; CI runs it on x86_64 and aarch64 across Debian, Ubuntu, Fedora,
  AlmaLinux and Amazon Linux, and under simulated kernels that lack Landlock or seccomp, where the
  sandbox must degrade and `sandbox="require"` must refuse to start.

## Differences from `Runtime`

- Values cross as plain data. A JS function or stream cannot be returned.
- A host function cannot call back into its own `IsolatedRuntime`.
- Start-up costs a process (about 0.1 s). Reuse one runtime for many evaluations.


## When the worker crashes

A crash is not a vulnerability; it is the containment doing its job. If the worker dies (a V8 abort, an
out-of-memory kill, a limit tripping, a sandbox violation), the runtime is **closed and abandoned**:
the next call raises `WorkerCrashed` (or `RuntimeTimeout`), and the host process carries on. Nothing the
dead worker left behind is trusted again, because a runtime is never reused after a crash. The cost of a
replacement is about 15 ms with the prewarmed spare.

What to do about it in your own code:

- **Make a new runtime** for the next request. Do not catch the error and retry on the same one.
- **Make host tools idempotent where you can.** A crash can land between a tool starting and its answer
  arriving, so the call may have happened without the guest ever hearing back. Record what a tool
  already did, as `AgentSandbox` journals do, rather than assuming it did nothing.
- **Count crashes per tenant.** One crash is the cost of doing business; a tenant whose runs crash over
  and over is probing, or broken. Stop accepting its runs after a few in a window, the same way you
  would rate-limit any other abuse.

## Limits against a hostile worker or guest

| Parameter | Default | Effect |
|---|---|---|
| `max_host_wait` | 600 s | Total time a run may spend waiting on host callbacks; exceeding it raises `RuntimeTimeout` and kills the worker. `None` disables it (the CPU cap still applies) |
| `max_inflight_host_calls` | 64 | Concurrent async host calls; extra calls get an error reply and never reach your function |
| `write_stall_timeout` | 10 s | A worker that stops reading its pipe is killed after this long (`None` disables) |
| `redact_host_errors` | `True` | Guest sees the exception class but only `"host function failed"` as message |

A worker CPU-time cap of twice the hard deadline also applies, because wall-clock pauses
during host calls cannot pause CPU.

Also in force without any setting:

- **A buffer cap.** With `max_memory` set (the default), `max_buffer_bytes` defaults to a quarter of
  it, so `new Uint8Array(2 ** 31)` is a catchable `RangeError` instead of the memory poll killing the
  whole worker. Set `RuntimeConfig(max_buffer_bytes=...)` to change it.
- **A thread cap.** A worker has about 13 threads (17 on macOS); the parent kills one with more than
  64, because a thread bomb stays under a memory ceiling.
- **Supervision while a host function runs.** Memory and thread limits keep being enforced while the
  runtime waits on a slow host function.
- **A runtime-wide call cap.** `max_inflight_host_calls` counts calls still running across commands,
  not just within one.
- **V8 flags.** Besides `--jitless`: a linear-time regex fallback after excessive backtracking (a
  catastrophic regular expression returns instead of running to the deadline) and
  `--freeze-flags-after-init`.

## The worker checks its own sandbox

Before any guest code exists, the worker *tries* the things the sandbox exists to stop: reading a
file, writing one, spawning a process, connecting out, signalling its parent, and on macOS reading the
parent's argv/environment and the machine's hardware ID. If a complete sandbox lets one through, the
worker refuses to start and `IsolatedRuntime` raises `WorkerCrashed("... sandbox self-test failed ...")`.

This is why a gap in a deny-list (which is only as good as its last review) becomes a failed start
rather than a finding. It costs a handful of syscalls. A *degraded* sandbox (a kernel without Landlock,
say) is expected to leak and is not tested this way; `sandbox="require"` refuses to start there, and
`sandbox="auto"` emits a `RuntimeWarning` saying which layers are missing.

The same fail-closed rule applies to limits: if the worker's memory or CPU cannot be read on this
system, `max_memory` and the CPU cap could never fire, so the runtime warns (`auto`) or refuses to
start (`require`).

## Running real libraries

Security that breaks the code people run gets switched off, so the sandbox is tested against real
libraries (`tests/test_isolated_libraries.py`, bytes pinned under `vendor/libs/`): pptxgenjs, three.js
with `GLTFExporter`, Vega-Lite, dagre, d3, turf, ECharts (server-side SVG), with the same results as
the plain `Runtime`, and again under `strict_eval=True` (Vega with its expression interpreter). A wider hand check (not vendored) also passed for lodash, date-fns, mathjs, KaTeX, Handlebars, zod, Ajv, yaml, jsPDF, pdf-lib, docx, JSZip, fflate, crypto-js,
decimal.js, luxon, Prettier, Terser, Cytoscape, Tailwind CSS v4's `compile`, and more. Libraries that
need a real DOM or canvas (mermaid rendering, Chart.js drawing) load but cannot draw.

Many libraries assume browser basics a bare isolate lacks (`setTimeout`, `TextEncoder`, `btoa`,
`Blob`). Opt in to a small, pure-JavaScript set:

```python
from pydeno import IsolatedRuntime, RuntimeConfig, WEB_POLYFILLS

rt = IsolatedRuntime(RuntimeConfig(bootstrap=WEB_POLYFILLS))
```

It adds timers on **virtual time** (nothing sleeps; `performance.now()` is a counter, not a clock, and
a runaway `setInterval` is cut off), `TextEncoder`/`TextDecoder`, `btoa`/`atob`, `Blob`,
`FileReader`, `EventTarget`, `AbortController` and `structuredClone` (each one a library in the
tests needed). It never defines `window` or
`document`, which would push libraries onto DOM code paths.

!!! warning "Do not write your own `setTimeout`"
    A library that loops on `setTimeout` / `requestAnimationFrame` needs a timer that honours the
    delay. A shim such as `setTimeout = (fn) => queueMicrotask(fn)` ignores the delay, so such a
    loop re-queues itself on the microtask queue forever and starves the isolate until the deadline
    (ECharts server-side rendering is an example). Use `WEB_POLYFILLS`: its timers run on virtual
    time and a runaway `setInterval` is cut off. Libraries that animate also usually have an option
    to switch it off (`animation: false`).

## Start-up cost

A new `IsolatedRuntime` costs about 55 ms before its first call (macOS arm64, median). Nearly all of
it is the worker: starting Python, importing (`asyncio` and what it pulls in is about half), creating
V8, applying the OS sandbox and running its self-test. The parent's share is under a millisecond.

`IsolatedRuntime` keeps **one spare worker** started in the background: it has loaded everything and
is waiting for its configuration, holds no data, is handed to exactly one runtime, and exits with its
parent. It helps when runtimes are created with pauses in between (about 15 ms instead of 55 when the
spare had time to finish importing); created back to back, each one still pays most of a cold start.
Pass `prewarm=False` to turn it off. Jitless V8 (the default) makes compute-heavy code about
1.5-2x slower; `jitless=False` trades that back for a larger attack surface.

### A pool of ready workers: `SandboxPool`

When you create a sandbox per request, use a pool. `SandboxPool` keeps `size` runtimes fully started:
the worker has received its configuration, created V8, applied the OS sandbox and passed its self-test.
`checkout()` hands one over in about 0.04 ms.

```python
from pydeno import RuntimeConfig, SandboxPool

pool = SandboxPool(RuntimeConfig(timeout=5), size=4, sandbox="require")

def handle(code: str) -> object:
    with pool.checkout(max_host_calls=50) as rt:   # this runtime is yours alone
        return rt.eval(code)                       # closing it kills its worker

pool.close()   # or `with SandboxPool(...) as pool:`
```

The rules, which are what keep a pool as safe as a fresh runtime:

- **Single use.** A checked-out runtime is never returned to the pool; closing it kills its worker.
  No worker ever serves two sessions, so nothing one guest leaves behind reaches the next. A pooled
  worker that dies while it waits (killed, out of memory) is discarded, never handed out.
- **Same construction.** Each pooled runtime is an ordinary `IsolatedRuntime` built with the pool's
  options: the same handshake, `sandbox="require"` check, self-test and limits, only earlier. The
  constructor starts the first one itself, so invalid options, or a platform that cannot satisfy
  `sandbox="require"`, fail there and not in the background.
- **Exhaustion is a cold start, never an error.** If every pooled runtime is taken, `checkout()`
  starts one on the spot. Replacements start in the background as soon as a runtime is handed out
  (`max_concurrent_starts`, default 2, at a time); a replacement that fails to start is retried with
  a backoff and shown in `stats()["last_error"]`.
- **Options split in two.** What the worker receives when it starts (the `RuntimeConfig`, `sandbox`,
  `jitless`, `v8_flags`, `strict_eval`, `clock`, `random_seed`, `max_memory`, console routing) is fixed per pool;
  use one pool per such configuration. What only the parent enforces (`SandboxPool.SESSION_OPTIONS`:
  `request_timeout`, `timeout_grace`, `max_host_calls`, `max_host_wait`, `max_inflight_host_calls`,
  `write_stall_timeout`, `redact_host_errors`) can be set per checkout.

Each pooled worker is a live process (tens of MB), so size the pool for your burst, not your peak:
a burst larger than the pool degrades to cold starts until the refill catches up. `stats()` reports
`ready`, `starting`, `checkouts` and `cold_starts`; `wait_ready()` blocks until the pool is full.

A forked child never receives the parent's pooled workers: its copy of the pool forgets them and
refills on its first checkout.

For asyncio, `AsyncSandboxPool` does the same with `AsyncIsolatedRuntime` (it also accepts
`handler_executor` per checkout). Pooled runtimes are bound to the loop the pool was started on:

```python
from pydeno import AsyncSandboxPool

async with AsyncSandboxPool(size=4, sandbox="require") as pool:
    async with pool.checkout() as rt:           # or: rt = await pool.checkout()
        print(await rt.eval("Promise.resolve(42)"))
```

Measured on macOS arm64 (`benches_py/alternatives_bench.py pydeno` and `pydeno-pool`, median):

| | Cold `IsolatedRuntime` | `SandboxPool` checkout |
|---|---:|---:|
| Hand-over | about 53 ms | 0.04 ms |
| Hand-over and first `1 + 1` | about 53 ms | 0.4 to 1.8 ms |
| Fresh sandbox and 10 small commands | about 55 ms | 7 ms |

The first command after a checkout can cost up to about 1.7 ms rather than the 0.1 ms of a warm
call: a process that has sat idle takes that long to be woken (the same happens to any idle runtime).

**Not done, deliberately: forking workers from a template.** A pre-initialised template process that
forks each worker would start one in a few milliseconds, but every worker would then share the
template's address-space layout, so one leaked pointer would defeat ASLR in all of them. If it is ever
added it will be opt-in. A worker written in Rust, without Python, is the other way to a faster cold
start.

## Smaller global scope

`SharedArrayBuffer`, `Atomics`, `WeakRef` and `FinalizationRegistry` are removed from the guest by
default: shared memory and atomics are what high-resolution timers
are built from, and weak references make garbage collection observable.

### Opt-in: without the newest language features

```python
IsolatedRuntime(config, v8_flags=["--no-js-shipping"])
```

This switches off, in the engine (syntax included, not just the globals), the language features
V8 shipped most recently: `Temporal`, `Float16Array`, explicit resource management (`using`,
`DisposableStack`, `AsyncDisposableStack`, `SuppressedError`), `Promise.try`, `RegExp.escape`,
`Math.sumPrecise`, `Error.isError`, `Uint8Array.fromBase64` / `toBase64`, and regular-expression
modifiers. Older features (iterator helpers, `Set` methods, `Object.groupBy`, `findLast`...) stay.
Newer engine code has had the least scrutiny, and none of the vendored libraries needs any
of it (`tests/test_isolated_libraries.py` runs each one with this flag). It is blunt: code that
uses one of those features fails. `--no-harmony-shipping` has the same effect.

The flags for the individual features (`--no-harmony-temporal`, `--no-js-float16array`,
`--no-js-explicit-resource-management`, ...) do not work: the engine's own start-up switches those
features back on after `v8_flags` are applied, so `IsolatedRuntime` refuses them with a `ValueError`
instead of reporting a restriction that never applied. A flag V8 does not recognise at all (a typo,
`--flag=false` on a boolean flag, upper case, a stray space) stops the worker from starting: that
surfaces as `WorkerCrashed` naming the flag.

## Proxies crossing the boundary

A Proxy in a result, a stream chunk or a host-function argument crosses as its innermost target,
found natively, and **none of that Proxy’s traps runs**. Running those traps would let guest code
act in the middle
of a conversion (grow a buffer after its size was checked, or answer `ownKeys` with `[]` while the
engine walks the whole target). So `new Proxy({a: 1}, {get: () => 'x'})` arrives as `{"a": 1}`, a
Proxy around an Array arrives as a list, and a revoked Proxy or one behind more than 64 others is
refused. A Proxy on an argument’s prototype chain can still run a `getPrototypeOf` trap
during the bridge’s type checks.

A Proxy around a function crosses as the underlying function; invoking its Python wrapper
skips the Proxy’s `apply` trap.

## Console callback deadlines

In the synchronous isolated runtime, deadline checks happen between console callbacks. A
single slow `on_console` or `print_callback` can therefore delay enforcement until it returns;
the twice-deadline bound for console floods applies between calls. The async supervisor checks
the deadline independently. Buffer output in host callbacks instead of blocking on a sink.

## Duration type validation

`RuntimeConfig.timeout` still follows the Rust binding’s numeric conversion rules, which can
coerce booleans and float-like objects. The shared Python limit validators cover the other
public duration/count arguments; they do not imply identical type validation for this field.
