# Changelog

## Unreleased

- Bound the async parent's queued frame count as well as payload bytes (#65). Empty or tiny
  frames from a compromised worker now trigger backpressure while the consumer is idle.

### Added

- **`SandboxPool`** and **`AsyncSandboxPool`**: isolated runtimes started ahead of time and handed out once.
  `checkout()` returns a runtime whose worker has already passed its handshake and sandbox self-test in about
  0.04 ms (a cold `IsolatedRuntime` is about 53 ms). A checked-out runtime is never returned to the pool;
  replacements start in the background; an empty pool falls back to a cold start, never an error. Options the
  worker receives at start-up are fixed per pool; parent-side ones (`SandboxPool.SESSION_OPTIONS`) can be set
  per checkout. `benches_py/alternatives_bench.py pydeno-pool` measures it.

### Changed

- Faster cold start of the isolation worker (about 59 to 55 ms on macOS arm64): the worker runs with `-S`
  (no `site`, so no `.pth` file runs in it) and imports `pydeno` from the parent's own package directory, so
  parent and worker always run the same code; the sandbox module no longer imports `ctypes.util` and
  `platform` (`sandbox_init` and `proc_pidinfo` are looked up in the already loaded libSystem,
  `os.uname()` replaces `platform.machine()`). A worker for a custom `python=` is started as before.
- Lower warm-call overhead of `IsolatedRuntime` (#47): a warm `eval("1 + 1")` went from about 112 to 67 µs
  (interleaved A/B, 30 rounds, medians; debug build of the extension on a loaded macOS arm64 machine, so
  release numbers will differ). Where it came from:
  - Reading the worker's CPU time on macOS no longer opens libSystem through a fresh `ctypes.CDLL` per call
    (about 30 µs, done twice per command); `proc_pidinfo` and the timebase are resolved once, and
    `_sandbox.usage(pid)` returns memory, CPU time and thread count from one kernel read (about 2 µs).
  - A command's CPU baseline is the latest cached reading (the previous command's final check, or the idle
    watchdog's, re-read if older than 0.5 s) instead of a fresh one, as `AsyncIsolatedRuntime` already did.
    CPU time only grows, so an older baseline can only charge a command more, never less. The end-of-command
    check is one reading for the memory ceiling, the thread cap and the idle baseline.
  - The worker reads the next command on its main thread instead of handing it over from a reader thread.
    The helper thread reads only while a command waits on host calls; a parent killed while a command runs
    without one is caught by the worker's watchdog thread (it now always runs, and exits the worker when its
    parent pid changes).
  No limit, wire check or sandbox requirement changed.

## 0.7.0 — 2026-10-04

Async, results, diagnostics. See [`docs/guides/upgrading.md`](docs/guides/upgrading.md) for what can change
behaviour you have today, and [`docs/roadmap.md`](docs/roadmap.md) for where this is going.

### Added

- **`AsyncIsolatedRuntime`**: an asyncio-native isolated runtime. Pipes on the event loop, one shared
  supervisor task per loop, no thread per runtime; cancelling a call kills the worker. On macOS (1 to 64
  runtimes) it was 1.4 to 2 times faster and the worst event-loop stall fell from 116 to 335 ms to 1 to 12 ms.
- **`AsyncAgentSandbox`** and **`SessionPool`**: async agent sessions, and a pool with a pluggable journal
  store, TTL, per-owner cap, LRU and a rollback counter (a stale journal is refused). `InMemoryJournalStore`
  is included; a Redis-protocol store is shown in the docs.
- **`ExecutionResult`** from `AgentSandbox.execute()` and `IsolatedRuntime.execute()`: status, stdout, stderr,
  result, error, error type, with ordered console capture capped by `max_output_bytes` and a result cap that
  fails the run with `ResultTooLarge` while the session stays usable.
- **Crash-safe journals**: `dump()` after a crash, timeout or kill returns the last good journal plus a `lost`
  record; `load()` charges the lost run's tool calls, so a crash cannot refund a tool budget.
- **JSON-Schema tools and a lazy tool catalog** (`SchemaTool`, `tools_catalog=`): only `search_tools` and
  `describe_tool` are declared up front, the declared surface is constant-size, and an undiscovered tool is
  refused with a typed error.
- **`sandbox_status()`**, **`classify_error()`** (25 stable error kinds, only worker crashes are retryable) and
  **`check_source()`** (an advisory pre-check, never a security boundary).
- Docs: upgrade guide, error kinds, async guides, roadmap to 1.0, pinned size and SHA-256 for every vendored bundle.
- A Platforms CI workflow: native Ubuntu (x86_64 and arm64), macOS (arm64 and Intel) and Windows, Python 3.10 to 3.14.

### Changed

- A JS `Map`, `WeakMap`, `WeakSet` or `Error` result now raises instead of becoming an empty dict.
- `dump()` after a crash returns the last good journal instead of raising.
- The worker's seccomp filter denies `memfd_create` (memory the RSS poll could not see).
- The sandbox self-test refuses to run in an orphaned worker.

### Fixed

- A guest that caught a public pydeno error (wrong arity, unknown catalog tool) made its own session
  unrestorable (`ReplayDivergence` on `load()`); replay no longer redacts a recorded error twice.
- A tool call buffered from a worker that had already died could still run its tool.
- Flaky memory-limit and loop-stall tests.

## 0.6.1 — 2026-10-04

Hotfix.

### Fixed

- **macOS: the sandboxed worker aborted at start on Python 3.10, 3.11 and 3.12** (`LowLevelAlloc
  arithmetic overflow`, SIGABRT). V8's allocator reads the page size through a sysctl, which the
  Seatbelt profile denied; Python 3.13+ happens to have read it already. The profile now allows exactly one
  read-only name, `hw.pagesize_compat`. Nothing else about the sandbox changes.
- When the idle watchdog killed a worker for exceeding `max_memory` or the thread cap, the caller
  could see a bare `killed by SIGKILL` instead of the reason. The reason is now kept.

## 0.6.0 — 2026-10-03

Sandbox hardening round 2 (independent review by three models and Copilot, plus prior-art research),
agent sessions, and a pydantic-ai integration. See [`docs/security-report.md`](docs/security-report.md)
for every finding and its status.

### Changed (behaviour you may notice)

- **`redact_host_errors` now defaults to `True`.** A host function's exception text no longer reaches
  the guest unless you opt out (`redact_host_errors=False`).
- **`max_buffer_bytes` defaults to `max_memory // 4`** when you do not set it, so a typed-array bomb is a
  catchable `RangeError` instead of killing the worker.
- **Signed snapshots are bound to the pydeno release** that made them (format `pydeno-snap2`). A snapshot
  signed by another release is refused before V8 sees it; sign it again with the release you run.
- **`RuntimeConfig(snapshot=...)` is refused by `IsolatedRuntime`** instead of being silently dropped
  (which also dropped its bootstrap).
- Workers get two more V8 flags: a linear-time regex fallback and `--freeze-flags-after-init`.
- `sandbox="auto"` emits a `RuntimeWarning` when the platform's full set of layers did not apply.
- Bind names must be plain identifiers; `IsolatedRuntime` checks them.

### Security

- **macOS:** a sandboxed worker could read its parent's argv **and environment** (and the machine's hardware
  ID, other processes' details and host statistics). The profile no longer allows `sysctl-read` and denies
  the process-info, IOKit, hardware-ID, host-statistics and `F_GETPATH` routes.
- **Startup self-test:** the worker tries the forbidden operations before any guest code exists and
  refuses to start if one works.
- **Bridge:** a guest that replaced `Date.prototype.valueOf`, `Array.prototype.map`, `Object.entries`
  and similar could make the bridge hand a Symbol to the Rust converter, which aborts: a lost worker, or
  a dead host process in a plain `Runtime`. Intrinsics are captured before guest code runs, the bridge is
  strict mode, and host-call arguments are capped (1M values, depth 128) before they are copied.
- **Linux seccomp:** stream-only `socketpair`; `prctl` allow-list; no executable mappings when jitless;
  no `sysinfo`/`getpriority`/`ioprio_get`; the socket ioctl block and `F_SETPIPE_SZ` denied;
  `get_robust_list`/`getpgid`/`getsid` only on ourselves; not dumpable; `RLIMIT_RTPRIO`/`NICE` 0. `uname`
  deliberately stays allowed: V8's x86_64 build calls it while starting.
- A worker with more than 64 threads is killed; memory and threads are supervised while a host function
  is running; the in-flight call cap is runtime-wide.
- A reused or mismatched capability token from the worker ends the session; revoking drops the host
  handler first; resolver, loader and console handlers validate what the worker sends them.
- Forked children forget the parent's workers and never touch them; a command waits for the runtime
  at most its own deadline; `eval_async` uses a thread of its own.
- A `BigInt` result past 4300 digits no longer ends the session; `Temporal.Now` follows the frozen
  clock; a guest can no longer kill the worker's reply-reading thread by not awaiting an async host call.

### Added

- **`AgentSandbox`** (`pydeno.AgentSandbox`): an AI-agent layer on `IsolatedRuntime`. State persists across
  runs; `start()`/`resume()` pause at every tool call (approval flows); a signed deterministic-replay
  journal (`dump()`/`load()`); `describe_tools()` and `typescript_stubs()` generate the prompt and `.d.ts`.
  See `docs/guides/agent-sessions.md`.
- **pydantic-ai integration** (`pydeno.integrations.pydantic_ai`): `JSCodeMode`, the JavaScript
  counterpart of the Monty-based code mode. See `docs/guides/pydantic-ai.md`.
- **Examples:** Monty prepares data and pydeno builds the result: three.js terrain, orbits and a city
  sun analysis; a d3 network; an ECharts dashboard; turf geospatial; a SQL question to a Vega-Lite chart;
  a spreadsheet to a PowerPoint deck. Vendored d3, ECharts and turf bundles (SHA-256 pinned).
- `docs/security-report.md`; `security.yml` (cargo-deny, cargo-audit, pip-audit, OSV).

## 0.5.0 — 2026-10-03

No breaking changes: every new limit is opt-in and `Runtime` is unchanged.

### Changed

- **`deno_core` 0.409 -> 0.412** (still V8 150.4, the newest V8 any `deno_core` supports; see
  `scripts/check_engine.py`). The debug-build overflow workaround in `Cargo.toml` stays: 0.412 still has
  the `source_map` subtraction bug (now at line 221), pinned by a test.
- **Lighter**: the release extension is stripped and link-time optimised (macOS: 60 MB -> 43 MB;
  nearly all of the rest is V8 and its built-in Intl data). `import pydeno` no longer loads the
  parent-side machinery (`asyncio`, `subprocess`, `tempfile`, the tool bridge, snapshot auth):
  those exports are lazy, so a plain `Runtime` user's import dropped from ~51 ms to ~19 ms.
- **Fused native JSON** for the isolation wire: results and call arguments are parsed and decoded
  (and encoded and written) in one native pass, with the GIL released while parsing. A 2 MB frame
  takes ~18 ms each way instead of ~60-80 ms; moving 50k small objects costs ~35 ms of overhead
  (it was ~144 ms). The Python codec remains the reference and fallback, and the two are tested
  against each other, including lone surrogates (valid in JavaScript, refused by `serde_json`).
  The native parser is deliberately stricter than `json.loads` about encodings (no BOM, no
  UTF-16/32, no raw surrogates).
- `scripts/gen_syscall_tables.py` regenerates `tests/data/syscalls.json` from a pinned Linux tag
  (v7.0), so the kernel tables the seccomp numbers are checked against are reproducible.
- **Native wire codec** (`src/runtime/wire.rs`): `IsolatedRuntime` encodes and decodes values in
  Rust. Structured results cost far less to move across the boundary (50k small objects: overhead
  144 ms -> 59 ms). The Python codec stays as the reference and fallback, and
  `tests/test_wire_native.py` checks the two agree on results *and* error messages, including on
  hostile input and with hypothesis-generated trees.

### Fixed

- Dropping a `SnapshotBuilder` without calling `build()` no longer leaks its isolate
  (it is now released on drop).

### Added

- **`pydeno.IsolatedRuntime`**: the same guest in a supervised worker
  *process*, so guest code can no longer abort or wedge the host, and an
  escaped V8 lands somewhere that cannot do much. Measured against
  pydantic/monty's security suite, an in-process `Runtime` is taken down by
  `new Array(2**32-1).fill(0)` and `'a'.repeat(2**28).match(/a/g)` (V8 fatal
  OOM) and ignores `timeout=` for `sort`/`reverse`/`map` on a sparse array (an
  uninterruptible native builtin). Under `IsolatedRuntime` each ends in a
  catchable error. Layers:
  - Copied from Monty's design: length-prefixed frames with a hard cap, a
    bounded decoder that treats the worker as untrusted (fuzzed), an empty
    worker environment, parent-side hard-kill deadlines that pause during host
    callbacks, crash detection, and a `max_host_calls` budget.
  - **OS sandbox** applied in the worker before the isolate exists: macOS
    Seatbelt (deny by default); Linux Landlock plus a seccomp-bpf filter that
    denies new processes, the network, mounts, kernel interfaces, IPC with the
    host user's other processes, file-metadata changes, identity changes and
    acting on any other pid. `sandbox="auto" | "require" | "off"`; read
    `.sandbox` for what is active. Found by a systematic assume-breach sweep
    (`scripts/redteam_syscalls.py`); the filter denies well over a hundred
    syscalls and every one is checked against the kernel's own tables.
  - **No privileges**: a worker started as root drops to `nobody` with an empty
    capability set and bounding set.
  - **Empty root** (Linux, where unprivileged user namespaces are allowed): the
    worker gets private mount, network, IPC and UTS namespaces and an empty
    tmpfs root, so it cannot even tell which host paths exist (`sandbox_extras`,
    `empty_root=False` to skip). Anything newer than the reviewed syscall range
    is denied by default (`ENOSYS`).
  - **`--jitless` V8** by default (`jitless=False` to allow the JIT and
    WebAssembly): no JIT compiler, the source of most V8 exploits.
  - **`max_memory`** (default 1 GiB) enforced by the worker itself every
    ~20ms (dedicated exit code, like Monty's allocator) and by the parent every
    ~50ms; covers WebAssembly and resizable buffers, which no in-process limit
    can. A 60 s hard deadline is also on by default.
  - **Smaller syscall surface** (second pass): NUMA policy (`mbind`, `set_mempolicy`),
    filesystem mutation (`mkdirat`, `unlinkat`, `renameat`, `linkat`, `symlinkat`, `mknodat`),
    file-to-file copies (`splice`, `tee`, `sendfile`, `copy_file_range`), POSIX timers, protection
    keys and re-entering Landlock/seccomp are denied, including x86_64's older path-based spellings
    (`mkdir`, `unlink`, `rename`, ...) that aarch64 never had. Checked against pptxgenjs, three.js,
    Vega-Lite and dagre bundles under the full Linux sandbox.
  - **`prewarm`** (default on): one ready spare worker is kept so the next runtime starts in
    about 15-45 ms instead of about 100 ms (macOS; the low end when the spare is used soon after
    it started). The guest also loses `SharedArrayBuffer`, `Atomics`,
    `WeakRef` and `FinalizationRegistry` (shared-memory timers, observable GC).
  - **`pydeno.WEB_POLYFILLS`**: opt-in, pure-JS browser basics (virtual-time timers, `TextEncoder`,
    `btoa`, `Blob`, `EventTarget`) so real libraries run in the sandbox. Tested with pptxgenjs,
    three.js, Vega-Lite, dagre (`vendor/libs/`), identical to `Runtime`.
  - **Hostile-peer hardening** (from an independent review): the host waits
    on a worker with bounded writes (`write_stall_timeout`, default 10 s), so a
    worker that stops reading cannot freeze the caller; host-callback time no
    longer pauses the deadline forever (`max_host_wait`, default 600 s, plus a
    CPU-time cap of twice the deadline, and `max_inflight_host_calls`, default
    64); hash-flood sets and dicts, ints past 2^53 and oversized argument lists
    are refused by the decoder; an idle worker that grows or spins is killed;
    `fcntl`/`ioctl` signal-owner commands, path `truncate` and process-group or
    user selectors on `setpriority`/`ioprio_set` are denied;
    `sandbox="require"` fails unless *every* layer of the platform applied;
    `redact_host_errors=True` hides host exception text from the guest.
  - **`clock=` and `random_seed=`**: a frozen guest clock (`Date`, `Intl`) and a
    seeded `Math.random`, Monty's `os_policy` for time and entropy.
  - The worker runs with `TZ=UTC`, no core dumps and bounded file size, so the
    host timezone and locale no longer reach the guest.
  Supports `eval`, `eval_async`, `bind_function`, `bind_object`, `revoke_op`,
  `add_static_module`, `set_module_resolver`, `set_module_loader`,
  `eval_module`, `eval_module_async`, `on_console` and `ToolBridge`; streams,
  snapshots, the inspector and function handles are not supported yet.
  Verified on macOS arm64 and Linux (Debian, Ubuntu 22.04/24.04, Fedora,
  AlmaLinux, Amazon Linux; Python 3.10-3.14); CI runs the matrix on x86_64 and
  aarch64 and under simulated kernels without Landlock or seccomp. See
  `docs/guides/advanced/isolation.md`.
- **`pydeno.sign_snapshot` / `verify_snapshot`**: HMAC-authenticate a V8
  snapshot before loading it. V8 deserialises snapshot bytes without validating
  them, so a tampered snapshot is a crash or worse; `verify_snapshot` raises
  `SnapshotAuthenticationError` and never returns bytes that failed the check.
- `SECURITY.md`: how to report a vulnerability, what is in scope, the threat
  model, and the hardening to turn on.
- `pydeno._pydeno._set_v8_flags` (private): process-global V8 flags, refused
  once any `Runtime` exists. Used by the isolated worker.
- **`RuntimeConfig(max_buffer_bytes=...)`** caps live `ArrayBuffer` /
  `SharedArrayBuffer` bytes with a custom V8 allocator
  (`src/runtime/capped_allocator.rs`). V8 does not count that storage against
  `max_heap_size`, so a guest under `max_heap_size=64MB` could allocate
  gigabytes. An over-budget allocation throws a catchable `RangeError`. It is
  opt-in and independent of `max_heap_size`, whose meaning is unchanged.
  `WebAssembly.Memory` and resizable-buffer growth are not covered (V8 offers
  only process-global flags for them); use `IsolatedRuntime(max_memory=...)`.
- `tests/test_monty_parity_security.py` ports the attack categories from
  Monty's security suite. Its strict-`xfail` cases are the sinks an in-process
  `Runtime` cannot contain; `tests/test_isolated_runtime.py` shows each is
  contained by `IsolatedRuntime`.

## 0.4.5 — 2026-09-29

- Publish a manylinux_2_28 `aarch64` wheel, built and tested on a native ARM runner, so Linux ARM installs no longer fall back to the sdist (which needs Rust and a compiler).

## 0.4.4 — 2026-09-28

- Enable fat link-time optimization, a single codegen unit and symbol stripping for smaller release wheels.
- Limit Hyper dependencies to the HTTP/1 inspector server and Tokio adapter, removing unused HTTP/2 dependencies.
- Preserve panic unwinding and the 0.4.3 runtime fixes.

## 0.4.3 — 2026-09-27

- Propagate Python conversion errors to async eval, module and function callers instead of leaving their futures pending.
- Release the GIL during blocking runtime control operations, object binding and stream cleanup so Python callbacks cannot deadlock the caller.
- Preserve `__proto__` dictionary keys as own data properties when sending Python objects into V8.
- Track ToolBridge capabilities per runtime with weak references; detaching one runtime no longer loses revocation tokens for another.
- Reject trailing newlines in tool names and timeouts that cannot fit the command format or platform clock.
- Snapshot binding dictionary entries before releasing the GIL so concurrent mutation cannot panic.
- Replace timing-sensitive concurrency assertions with barrier checks and repair documentation references.
- Use the installed Linux wheel's interpreter for CI report checks and align local Ruff with CI.

## 0.4.2 — internal cleanup, no API changes

An internal refactor with no API or behaviour changes. The Python API, the
`_pydeno` stubs, error messages, and runtime semantics are all unchanged, and
the full test suite (589 tests) passes as it did on 0.4.1. Rust source is down
from 11,749 to 9,210 lines, and the Python package from 713 to 645.

### Changed (internal)

- **`src/runtime/runner.rs` (3,904 lines) is now a `runner/` module** split by
  responsibility: `termination.rs` (`TerminationController`, the deadline
  watchdog), `dispatcher.rs` (event loop, command handling), `jobs.rs` (async
  job state machines), `core.rs` (`RuntimeCoreState`, sync entry points),
  `convert.rs` (V8 ↔ `JSValue`), and `mod.rs` (commands, thread spawn).
  - Command handling shares one set of admission helpers instead of ~20
    copies of the terminated/inspector checks.
  - Sync and async function calls share one call path and error type.
  - A single `Converter` replaces the 4–6 arguments that were threaded
    through every value conversion.
  - Sync and async module evaluation share specifier parsing, loading and
    namespace extraction.
- **`src/runtime/python/runtime.rs`** is split into `runtime.rs`,
  `function.rs` and `stream.rs`. The stats pyclasses are generated from their
  source structs.
- **`handle.rs`** sends every command through generic request/response
  helpers.
- **`ops.rs`, `config.rs`, `loader.rs`, `error.rs`** share their OpState
  lookup, handler call, validation, and loader-call helpers.
- **`js_value.rs`, `stream.rs`, `inspector.rs`, `conversion.rs`, `stats.rs`**:
  repeated match arms and helpers are deduplicated, and call counters are
  indexed by call kind.
- **`python/pydeno`**: `ToolBridge` internals, default-runtime helpers, the
  CLI, and the awaitable adapter are simplified.
- Over-long internal comments are condensed to the reasoning that isn't
  obvious from the code. Python-visible docstrings and `SAFETY` comments are
  kept verbatim.

## 0.4.1

Follow-ups to the 0.4.0 review (`docs/reviews/2026-09-22-0.4.0-review.md`).
No breaking API changes, but three changes a caller can observe:

- A timeout's exception is now `RuntimeTimeout` rather than exactly
  `RuntimeError`, so `except RuntimeError` is unaffected but a check such as
  `type(exc) is RuntimeError` (or matching on `repr(exc)`) is not.
- A timed-out JS *function call* (`fn(...)`, `fn.call_async(...)`) used to
  raise `JavaScriptError: Uncaught null`, which is not a `RuntimeError` at
  all. It now raises `RuntimeTimeout`, so an `except JavaScriptError` that
  happened to catch it no longer does.
- Function calls that used to hang past their `timeout=` (see *Fixed*) now
  raise at the deadline, and an async function call's deadline can now stop
  an unrelated inline sync call (see *Documented*).

### Added

- **`pydeno.RuntimeTimeout`**, raised instead of a bare `RuntimeError` when an
  operation exceeds its `timeout`. Until now a timeout was indistinguishable
  from an internal failure except by matching `"timed out"` in the message,
  which is not an API — rewording the message would have broken every caller
  keying off it. `RuntimeTimeout` subclasses `RuntimeError`, so existing
  `except RuntimeError` handlers keep catching timeouts unchanged:

  ```python
  from pydeno import Runtime, RuntimeConfig, RuntimeTimeout

  with Runtime(RuntimeConfig(timeout=1.0)) as rt:
      try:
          rt.eval("while (true) {}")
      except RuntimeTimeout:
          ...  # only a timeout reaches here
  ```

  It is deliberately not called `TimeoutError`: Python's builtin of that name
  derives from `OSError`, and a same-named subclass of a different base would
  be a trap.

  Every timeout path raises it: `eval`, `eval_async`, `eval_module`,
  `eval_module_async`, `fn(...)`, `fn.call_async(...)`, and the awaited half
  of a `fn(...)` that returned a promise. `tests/test_timeout_every_path.py`
  has one row per path, each asserting the type, a wall-clock bound, and that
  the runtime is still usable and closes afterwards.

### Fixed

- **A JS function call whose JS was still running at its deadline hung, or
  raised the wrong error.** Pre-existing in 0.4.0.

  - `fn.call_async(...)`, and the awaited half of a `fn(...)` that returned a
    promise, armed no watchdog. Their deadline was only checked *between*
    event-loop steps, and the dispatcher's own step watchdog is armed only
    while no job is active, so `async () => { await 0; while (true) {} }`
    hung forever, `timeout=` and `RuntimeConfig.timeout` notwithstanding.
    Both now arm one for the job's lifetime, as `eval_async` does; a resumed
    call arms it for what is left of the original call's clock.
  - `fn(..., timeout=...)` on a runtime without `RuntimeConfig.timeout`
    armed nothing either: the synchronous call only consulted the
    runtime-wide timeout. It now uses the per-call one when given.
  - When a watchdog did stop a function call, the caller got `JavaScriptError:
    Uncaught null` — not `RuntimeTimeout`, not even a `RuntimeError` — because
    V8 reports a terminated `func.call` as a null exception, which the timeout
    mapping did not recognise. It is now reported as `execution terminated`,
    exactly as a terminated `eval` is, so it becomes `RuntimeTimeout` when the
    call's own deadline fired and `RuntimeTerminated` after `terminate()`.

  Because async function calls now have a real deadline, they take part in
  the cross-talk described under *Documented* below, exactly as `eval_async`
  already did: an async call's deadline can stop an unrelated synchronous
  call dispatched inline while it is parked. A function-call victim of that
  reports `execution terminated`, where it used to say `Uncaught null`.

- **An async job that timed out on a pending promise left the runtime
  permanently unusable.** After `await rt.eval_async("new Promise(() => {})",
  timeout=0.3)` raised its `RuntimeTimeout`, every later call on that runtime
  — a plain `rt.eval("1 + 1")` issued long afterwards, with nothing else in
  flight — failed with a bare `execution terminated`. The runtime did not
  error and recover; it stopped working while still reporting itself open.

  The job's own deadline check asked V8 to terminate and nothing cancelled
  that request. The cancel that exists, in `resolve_sync_watchdog`, runs only
  when the job's watchdog token comes back *fired* — and the in-job check
  routinely wins the race against the watchdog thread, since both wake on the
  same deadline and the dispatcher parks until exactly that instant, so
  `disarm` returned `false` and the isolate stayed latched. `JobCommon::expired`
  now records the request it made and `JobCommon::respond` clears it, the
  async counterpart of what the synchronous path already did. Pre-existing
  in 0.4.0.

  The timed-out promise itself stays pending, as before — nothing on the
  Python side awaits it, so nothing hangs.
  `tests/test_timeout_cross_talk.py` asserts reuse (sync and async, and
  across a second timeout).

- **A timeout that raced its own deadline could kill the next, unrelated
  call.** The watchdog thread marked an expired deadline as fired, released
  its lock, and only then asked V8 to terminate. A call that finished on its
  own just past its deadline could be disarmed in that gap — seeing `fired`,
  cancelling a termination that had not been requested yet — and the
  watchdog's late request then latched the isolate, so the *next* call failed
  with a bare `execution terminated`. The watchdog now holds its lock until
  the termination has been issued. Pre-existing in 0.4.0; the window is
  microseconds wide, so it was rare rather than impossible.

- **`Runtime.close()` could hang.** `Watchdog::drop` set the shutdown flag
  without holding the mutex the watchdog thread's condition variable is
  paired with, so the wake-up could be lost and the watchdog thread parked
  forever, blocking the `join` in `close()`. It now holds that mutex across
  the write.

- **The reason attached to a multi-expiry watchdog pass is no longer
  arbitrary.** When several deadlines expired in the same pass, the reason was
  taken from the last fired entry in a `Vec` whose order `swap_remove` makes
  meaningless. It is now the deadline that expired first.

- **`CLAUDE.md`'s streaming example called API that never existed.**
  `rt.create_js_stream_from_python(...)` and the guest global
  `__pydeno_get_stream__(id)` appear only in that example — `git log -S` puts
  both in the initial commit and nowhere else, and the `peno` → `pydeno`
  rename dutifully renamed a symbol that was never real. The example now uses
  `rt.stream_from_async_iterable(...)`, and `tests/test_claude_md_api_references.py`
  checks that every `Runtime` attribute and guest global the file names
  actually resolves.

### Documented

Known limitations are now stated where callers will meet them, and pinned
by tests so they cannot drift silently.

- **`SnapshotBuilder` input is not sandboxed.** Its docstring now says so:
  snapshot scripts run with the raw `Deno.core.ops` table, no timeout and no
  serialization limit, so they are host code, never untrusted JavaScript.

- **A debug build cannot reach the default `max_serialization_depth`.** The
  native stack-headroom backstop that keeps a deeply nested value from
  aborting the process (`Check failed: IsOnCentralStack()`) trips around depth
  22 in an unoptimized build, against ~743 in an optimized one, because an
  unoptimized serializer frame is ~33x larger. Released wheels are optimized
  builds and are unaffected; anyone working on pydeno itself is not. No single
  budget can serve both profiles, so the backstop is unchanged — but its error
  message now names the build profile as the cause instead of reading as a
  fault in the caller's data, `RuntimeConfig.max_serialization_depth`
  documents the ceiling, and `tests/test_serialization_headroom.py` covers the
  22–99 band at the default configuration that nothing exercised before.

- **A fired deadline terminates whatever the isolate is running, not the job
  that timed out.** `terminate_execution` is isolate-wide and the isolate is
  single-threaded, so a synchronous call dispatched while an async job is
  parked on a promise can be stopped by that async job's deadline, and reports
  a bare `execution terminated` error rather than a timeout. There is nothing
  finer to aim at, so the behaviour stands; it is described on
  `RuntimeConfig.timeout` and on `ArmedDeadline`, and pinned by
  `tests/test_timeout_cross_talk.py`. Use a runtime per concurrent job if a
  termination error has to be about the call that raised it.


## 0.4.0

The package was renamed from `peno` to `pydeno`. Import `pydeno`; there is no
compatibility shim under the old name.

### Breaking changes

- **`IsolatePool` and `PooledIsolate` are removed.** `from pydeno import
  IsolatePool` now raises `ImportError`. The pool drove a bare `v8::Isolate`
  with no serialization limits, no timeout and no heap cap, so guest code
  running in a pooled isolate could return an unbounded result and take the
  host process down — a second, unmetered path around every limit `Runtime`
  enforces. It also never supported tools (it had no op registry to bind a
  Python callable into), and it measured ~12x *slower* than simply retaining
  a warm `Runtime`.

  Replace a pooled isolate with a retained `Runtime`:

  ```python
  # before
  pool = IsolatePool(size=4)
  with pool.checkout() as iso:
      iso.eval("1 + 1")

  # after — keep the Runtime alive and reuse it
  rt = Runtime(RuntimeConfig(timeout=5.0))
  rt.eval("1 + 1")
  ```

- **`Deno`, `__bootstrap` and `__infra` are deleted from the guest global.**
  `globalThis.__bootstrap.core.ops` used to survive the bridge's `delete
  globalThis.Deno` and exposed the same raw op table; `op_print` wrote
  arbitrary bytes straight to the host process's stdout, with no timeout and
  no metering. Guest JS that reached for any of these now sees `undefined`.
  The exact guest-visible `Reflect.ownKeys(globalThis)` surface is pinned by
  `tests/test_guest_globals.py`, so a future `deno_core` bump that installs a
  new global fails loudly instead of silently reopening the hole.

  This does not apply to `SnapshotBuilder`, which by design runs host code in
  an unsandboxed isolate — see its docstring.

- **A runaway microtask queue now times out against the call that created
  it.** `eval()` and `eval_module()` drain the microtask queue inside their
  own timeout window. A script such as
  `const f = () => queueMicrotask(f); f();` previously *returned a value*
  immediately and left the queue to hang some later, unrelated, untimed
  operation (the next `eval`, or `close()`). It now raises
  `RuntimeError: ... timed out after Nms` from the call that queued it,
  whenever `RuntimeConfig(timeout=...)` is set. Callers that relied on such a
  script returning normally will now see an exception.

- **`execution_timeout` is enforced around the dispatcher's event-loop step.**
  Work that outlives the job that queued it (a fire-and-forget continuation
  that keeps re-queuing itself) previously ignored `execution_timeout`
  entirely and could wedge the runtime. It is now terminated.

### Fixes

- Recursion in the JS→Python converter is bounded by real native/V8 stack
  headroom, not only by `max_serialization_depth`. Raising that knob past
  what the isolate's stack can sustain used to abort the process; it now
  raises a catchable `RuntimeError`. Note that the check is unconditional and
  the per-frame cost differs by build profile: in an unoptimized build it
  trips around depth 22, in a release build around depth 743.
- Cycle detection walks the current traversal path (`strict_equals`) instead
  of a `get_identity_hash()` set, so two distinct acyclic objects sharing a
  V8 identity hash are no longer reported as a circular reference.

### Performance

- One persistent watchdog thread per runtime replaces the thread spawned and
  joined for every timed call: a timed `eval` costs ~9.5 µs against ~28.5 µs
  before, within noise of an untimed one.
