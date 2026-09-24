# Changelog

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
