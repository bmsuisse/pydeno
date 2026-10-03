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
still apply, and `max_memory` still bounds WebAssembly memory.

`v8_flags=[...]` passes extra flags to the worker (for example
`--disallow-code-generation-from-strings`). Unknown flags are an error, not ignored.

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
| `max_buffer_bytes` | live `ArrayBuffer` / `SharedArrayBuffer` bytes | a catchable `RangeError` |
| `max_memory` | worker RSS | the worker is killed |

## What works across the boundary

`eval`, `eval_async`, `bind_function`, `bind_object`, `revoke_op`, `add_static_module`,
`set_module_resolver`, `set_module_loader`, `eval_module`, `eval_module_async`, `on_console`,
`ToolBridge`, async host functions (running concurrently, like in-process), and
`None`/`bool`/`int` (incl. BigInt)/`float`/`str`/`bytes`/`list`/`dict`/`set`/`datetime`/`undefined`.

Not supported yet: streams, snapshots, the inspector, function handles, Windows.

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
with `GLTFExporter`, Vega-Lite, dagre, with the same results as the plain
`Runtime`. A wider hand check (not vendored) also passed for lodash, date-fns, d3, ECharts (server-side
SVG), mathjs, KaTeX, Handlebars, zod, Ajv, yaml, jsPDF, pdf-lib, docx, JSZip, fflate, crypto-js,
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

## Start-up cost

A worker costs about 55 ms to start (Python plus the imports), of which the sandbox is about 4 ms.
`IsolatedRuntime` therefore keeps **one spare worker** started in the background: it has loaded
everything and is waiting for its configuration, holds no data, is handed to exactly one runtime, and
exits with its parent. Create-and-eval takes about 100 ms without it and about 15 ms with it when the
spare is used soon after it started. On macOS it measured 30-45 ms after the spare had sat idle for
more than ~0.2 s (the operating system is slow to wake an idle process; Linux was not measured).
Pass `prewarm=False` to turn it off. Jitless V8 (the default) makes compute-heavy code about
1.5-2x slower; `jitless=False` trades that back for a larger attack surface.

## Smaller global scope

`SharedArrayBuffer`, `Atomics`, `WeakRef` and `FinalizationRegistry` are removed from the guest by
default: shared memory and atomics are what high-resolution timers
are built from, and weak references make garbage collection observable.
