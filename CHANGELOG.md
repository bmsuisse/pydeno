# Changelog

## Unreleased

No breaking changes: every new limit is opt-in and `Runtime` is unchanged.

### Changed

- **`deno_core` 0.409 -> 0.412** (still V8 150.4, the newest V8 any `deno_core` supports; see
  `scripts/check_engine.py`). The debug-build overflow workaround in `Cargo.toml` is gone: the
  upstream `source_map` bug it covered is fixed in this release.
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
    keys and re-entering Landlock/seccomp are denied. Checked against pptxgenjs, three.js,
    vega-lite and dagre bundles under the full Linux sandbox.
  - **`prewarm`** (default on): one ready spare worker is kept so the next runtime starts in
    about 15-45 ms instead of about 100 ms (macOS; the low end when the spare is used soon after
    it started). **`strip_globals`** removes `SharedArrayBuffer`, `Atomics`,
    `WeakRef` and `FinalizationRegistry` from the guest by default.
  - **`pydeno.WEB_POLYFILLS`**: opt-in, pure-JS browser basics (virtual-time timers, `TextEncoder`,
    `btoa`, `Blob`, `EventTarget`) so real libraries run in the sandbox. Tested with pptxgenjs,
    three.js, Vega-Lite, dagre, marked, dayjs and PapaParse (`vendor/libs/`), identical to `Runtime`.
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
