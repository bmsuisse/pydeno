# Changelog

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
