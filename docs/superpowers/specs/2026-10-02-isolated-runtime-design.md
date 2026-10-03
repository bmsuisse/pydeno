# Isolated runtime: crash and hang containment for guest JavaScript

Status: approved by the standing `/goal` ("make pydeno as safe as Monty, do what it takes").

## Problem

pydeno runs V8 in the host's own process. Measured against pydantic/monty's security
suite, three classes escape every in-process guard
(`tests/test_monty_parity_security.py`):

| Class | Example | Result |
|---|---|---|
| V8 fatal OOM | `new Array(2**32-1).fill(0)` | host process aborts |
| Uninterruptible native loop | `a[2**32-2]=1; a.sort()` | `timeout=` is ignored |
| Page-allocator memory | `new WebAssembly.Memory(...)` | uncapped host RAM |

`TerminateExecution` is honoured only at V8 interrupt points, so no in-process guard closes the
class. Monty is immune because every session runs in a worker **subprocess** that the parent can
kill. This spec adopts that boundary.

## Decision

Add `pydeno.IsolatedRuntime`: the same guest, run in a worker process the parent supervises.
It is opt-in; `Runtime` is unchanged.

Rejected alternatives:

- **Reuse `monty-pool`.** Hard-wired to Monty's wire format and interpreter.
- **A new Rust worker binary.** `RuntimeCommand` carries live `Py<PyAny>` handlers (ops, module
  resolver/loader, task locals), so the V8 core is inseparable from PyO3. A Rust worker would
  mean rewriting the whole core. A Python worker that hosts the existing `Runtime` keeps full
  parity and moves only the trust boundary, which is what the goal needs.

## Design (what is copied from Monty, what is not)

Copied in spirit from `monty-proto` / `monty-pool` (MIT; no source is vendored, the pieces are
re-expressed in Python):

- 4-byte little-endian length-prefixed frames with a hard frame cap.
- The parent treats every worker frame as **untrusted**: a bounded decoder (frame size, depth,
  node count, string bytes), strict message schema, unknown ids or tags discard the worker.
- Worker started with an **empty environment**.
- A parent-side hard deadline kills the worker (`request_timeout`); the deadline clock is
  paused while the parent runs a host callback, as in Monty.
- A dead worker is observed and reported (`WorkerCrashed`); the session is lost, never reused.
- `max_memory` enforced from outside the child (Monty enforces in-allocator; pydeno polls
  worker RSS because the core allocator is V8's).

Not copied: Monty's value arena and interpreter state machine. Values cross as a small tagged
JSON codec over exactly the types pydeno already converts (None, bool, int incl. BigInt, float
incl. NaN/inf, str, bytes, list, dict, set, datetime, `undefined`). The parent never unpickles.

### Components

- `python/pydeno/_wire.py`: framing, tagged codec, decode budget.
- `python/pydeno/_worker.py`: `python -I -m pydeno._worker`. Reads commands, runs them on the real
  `Runtime`, turns every bound host function into a stub that sends a `call` frame and waits for
  the parent's `reply`. Binary stdout is the protocol channel; fd 1 is redirected so stray
  output cannot corrupt it.
- `python/pydeno/_isolated.py`: `IsolatedRuntime` and `WorkerCrashed`. API: `eval`,
  `eval_async`, `bind_function`, `bind_object`, `revoke_op`, `add_static_module`, `close`,
  `is_closed`, context manager. `ToolBridge.attach` accepts it.

### Protocol

Parent to worker: `init`, `eval`, `eval_async`, `bind_function`, `bind_object`, `revoke`,
`add_module`, `reply`, `close`. Worker to parent: `ready`, `result`, `error`, `call`.
Host calls are concurrent promises in V8, so every `call` and `reply` carries an id; a
`result`/`error` carries the command id.

### Failure semantics

| Event | Outcome |
|---|---|
| JS exception, soft timeout, heap limit | same exception types as `Runtime`, session usable (heap-limit termination closes the worker's runtime as today) |
| Worker abort / OOM / signal / EOF | `WorkerCrashed`, worker reaped, runtime closed |
| Hard deadline exceeded | worker SIGKILLed, `RuntimeTimeout`, runtime closed |
| RSS above `max_memory` | worker SIGKILLed, `WorkerCrashed`, runtime closed |
| Protocol violation from worker | worker killed, `WorkerCrashed` |

## Hardening layers added after the first cut

Goal raised to "match or surpass Monty". Monty relies on being a memory-safe interpreter with no
I/O; pydeno cannot make that argument for V8, so the worker adds layers Monty has no equivalent
for:

- **OS sandbox** (`_sandbox.py`), applied inside the worker *before* the isolate exists so every
  thread V8 and tokio spawn inherits it: macOS Seatbelt (deny default); Linux Landlock plus a
  seccomp-bpf deny list with `no_new_privs`. Everything the worker imports is imported first.
  The seccomp filter is test-fired in a forked child because it kills on an architecture
  mismatch (emulation reports a different `platform.machine()` than the kernel).
- **Empty root** (Linux, best effort): `unshare` of user, mount, network, IPC and UTS namespaces,
  then `pivot_root` into an empty tmpfs, then all capabilities dropped. Hides every host path (the
  one thing neither Landlock nor seccomp can) and leaves no interface or IPC object to talk to
  even if the seccomp filter were bypassed. `CLONE_NEWUSER` needs a single-threaded caller, so the
  worker now reads `init` and applies the sandbox *before* it starts its reader thread.
  Reported separately (`sandbox_extras`) so the core layer names mean the same everywhere.
- **Default-deny for the future**: syscall numbers above the reviewed range (and the x32 flag bit)
  answer ENOSYS; a test fails when the kernel tables grow past it, forcing a review.
- **`--jitless` V8** (default), via a guarded `_set_v8_flags`. Removes the JIT and WebAssembly.
- **Memory**: the worker's own 20 ms RSS watchdog exits with a dedicated code (like Monty's
  allocator exit), the parent polls every 50 ms as a backstop, and the RSS probe holds its
  `/proc` fd open from before the sandbox. Found by running the suite on Linux: Landlock had
  silently blinded the first version.
- **Safe defaults**: 1 GiB `max_memory`, 60 s hard deadline, `max_host_calls` available.
- **Process hygiene**: `TZ=UTC`, no core dumps, bounded file size, empty environment, and a
  worker started as root drops to `nobody` with an empty capability and bounding set.
- **Assume-breach red team** (`scripts/redteam_syscalls.py`, `tests/test_redteam_syscalls.py`):
  a sweep that fires every syscall from a sandboxed process and reads the answer. EPERM with junk
  arguments means "denied by the filter" because seccomp decides at syscall entry. It found the
  reachable SysV/POSIX IPC, `inotify`, new-mount-API, clock-changing, `personality`, xattr and
  cross-process `setpriority`/`sched_*`/`prlimit64` calls, and the missing signal restriction
  (`kill(parent)`), now denied. The filter went from 59 to well over a hundred denied syscalls.
  Every number is checked against the kernel's own tables (`tests/data/syscalls.json`).
- **Supervision on a clock, not on silence.** Found by reading the pump: the hard deadline and
  memory check ran only when the pipe went idle, so a guest looping on a cheap host function
  switched both off. Now checked every 100 ms regardless of traffic. Also: error text from the
  worker is length-capped, an unknown exception class cannot be named, a dripped frame cannot stall
  the parent, and an orphaned worker exits when its parent dies.
- **Determinism**: `clock=` freezes `Date`/`Intl` (the original `Date` is unreachable) and
  `random_seed=` seeds `Math.random`.
- **Parity features**: module resolver/loader, `eval_module(_async)`, `on_console`.
- **Signed snapshots**: `sign_snapshot`/`verify_snapshot` (HMAC-SHA256), because V8
  deserialises snapshot bytes without validating them.
- **Opt-in safe default**: `configure_default_runtime(isolated=True)` routes `pydeno.eval()` and
  friends through the worker.
- **Verification**: macOS arm64; Linux aarch64 in podman across Python 3.10-3.14, Debian 12/13,
  Ubuntu 22.04/24.04, Fedora, AlmaLinux 9 and Amazon Linux 2023, and under simulated kernels that
  lack Landlock or seccomp (`scripts/linux_matrix.sh`). CI repeats the matrix on native x86_64 and
  aarch64 runners. The x86_64 half of the filter has so far been verified by the kernel-table
  tests, not on native hardware.

### Review round (hostile worker / guest)

An independent review found: blocking writes to a worker that stops reading; a deadline kept
paused by overlapping async host calls; hash-collision and oversized-int decode cost; raw
exceptions escaping the kill path on malformed fields; stale descriptor reuse; `select` on fds
>= 1024; `fcntl`/`ioctl` signal-owner commands and `setpriority`/`ioprio_set` selectors that
reached other processes; path `truncate`; `sandbox="require"` accepting a partial sandbox.
Each is fixed and pinned by `tests/test_isolated_review_findings.py`, the BPF-interpreter tests
and the Linux red-team probes. New knobs: `write_stall_timeout`, `max_host_wait`, CPU cap,
`max_inflight_host_calls`, `redact_host_errors`.

## Known residuals

- Where the empty root cannot apply (no unprivileged user namespaces; always on macOS), a
  sandboxed worker can still see that a path *exists* (ENOENT precedes the sandbox check). On
  Linux it can then also `stat` it (Landlock does not govern metadata); macOS refuses the `stat`.
- ~220 syscalls remain reachable (file-descriptor, memory, time and thread plumbing V8 needs). A
  default-deny allowlist would shrink that, at the price of brittleness across libcs and kernels.
- A V8 bug that corrupts the worker is contained to the worker; it is not prevented.

## Out of scope for this cut (follow-ups)

Streams, snapshots, inspector, function handles, Windows, pre-warmed pools.

## Success criteria

1. All 8 uncontained native-sink probes, run inside `IsolatedRuntime`, end in a catchable error
   in under the hard deadline, and the host process is unaffected.
2. A guest allocating past `max_memory` is killed.
3. Host functions, `bind_object`, `ToolBridge`, async handlers and BigInt/bytes/datetime
   round-trip identically to `Runtime`.
4. A malformed or oversized worker frame never crashes or exhausts the parent.
5. The existing suite still passes; `Runtime` behaviour is unchanged.
