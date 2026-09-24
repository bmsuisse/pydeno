# Upstream provenance and the three founding fixes

`pydeno` is a fork. This page records where it came from and the three fixes
that made forking worth doing, because that is the part of the history that is
still load-bearing: each one is a bug class this codebase now has permanent
tests against, and knowing *why* those tests exist is what keeps someone from
deleting the guard along with the bug.

It replaces `PATCH.md`, `PATCH_LARGE_SCRIPT_ABORT.md` and
`PATCH_SNAPSHOT_ABORT.md` (281 lines at the repo root). Those were written as
patch proposals against an upstream that this repo no longer tracks — they
carried re-sync instructions, extraction notes and before/after narratives for
work that has long since landed, and they read as open items. The substance
that survived the fixes is below; the rest was provenance about a workflow that
ended.

## Provenance

| | |
|---|---|
| Upstream | [imfing/jsrun](https://github.com/imfing/jsrun) |
| Upstream base commit | `34b786d2db410cdd264e9c8607ad7cb57e873a64` (tag-less `main`, fetched 2026-09-20) |
| Licence | MIT, retained with the original copyright — see [`LICENSE`](https://github.com/bmsuisse/pydeno/blob/main/LICENSE) |
| This repo | [bmsuisse/pydeno](https://github.com/bmsuisse/pydeno) — standalone, not a tracking fork |

The name is `pydeno` = **py**thon + **deno**. The package was renamed on the
fork; `jsrun` no longer appears in this codebase.

## 1. Cross-thread termination (`TerminationHandle`)

**The bug.** `Runtime` is `#[pyclass(unsendable)]`, which makes *any* method
call from a thread other than the creating one raise
`pyo3_runtime.PanicException: Runtime is unsendable, but sent to another
thread` — including `terminate()`. So the obvious kill switch for a runaway
synchronous `eval()` (spawn a watchdog thread, call `runtime.terminate()`)
panicked in the watchdog instead of stopping the JS, and the loop ran until the
process was killed from outside. `unsendable` is correct for `Runtime`, which
drives a `!Send` V8 isolate in place; the mistake was that the one safe
cross-thread operation was only reachable *through* it.

**The fix.** Split the PyO3 surface along the boundary `deno_core` itself
draws. `TerminationController` owns only an `AtomicU8`, a
`Mutex<Option<String>>` and a `v8::IsolateHandle` — and `IsolateHandle` is
documented `Clone + Send + Sync` precisely so a host can call
`terminate_execution()` from another thread. It is now exposed as a separate,
deliberately *not* `unsendable` pyclass, handed out by
`Runtime.termination_handle()` before the blocking call starts.

**What guards it now.** `tests/test_termination_handle.py`, plus
`test_pathological_regexes_are_interruptible`, and everything in
`tests/test_parked_termination.py` — which covers the harder case this fix did
not reach: a runtime parked on a *pending promise*, where
`terminate_execution()` is a no-op because V8 only acts on a termination when
it next enters JavaScript. See `BENCHMARKS.md` for the kill latencies.

## 2. Uncatchable `SIGABRT` on large JS source

**The bug.** Evaluating more than roughly 148 KB of JS source (binary search
found the threshold at 147,654 bytes; 147,303 still worked) aborted the whole
Python process:

```
V8 posted a delayed task, but this isolate was created outside of a tokio
runtime context and the delay cannot be honored.
```

Not a panic and not catchable from Python: `deno_core` calls
`std::process::abort()` from a frame Rust cannot unwind. It reproduced through
every entry point (`eval`, `eval_async`, module-level `pydeno.eval_async`,
`RuntimeConfig(bootstrap=...)`) and independently under tight heap limits,
because the trigger is source size or GC pressure, not the call path.

**The root cause.** `deno_core`'s `register_isolate` snapshots
`tokio::runtime::Handle::try_current()` at `JsRuntime::new` time. V8 later
posts a *delayed* foreground task — streaming/background compilation for a
large script, or a GC memory-reducer timer — `deno_core` looks up that stored
handle, finds `None`, and aborts. `spawn_runtime_thread` built its tokio
runtime and then created the isolate *before* `block_on`, so
`try_current()` always failed and every isolate was registered one delayed
task away from an abort.

**The fix.** Enter the thread's own tokio runtime around
`RuntimeCoreState::new` (`let _tokio_enter = tokio_rt.enter();`), which is
exactly what `deno_core`'s abort message recommends. The guard is dropped
before `block_on` takes over. The comment at that line in
`src/runtime/runner/mod.rs` is deliberately long; it is the only thing standing
between a future refactor and a reintroduced process abort.

**What guards it now.** `tests/test_large_script_eval.py` — 200 KB to 5 MB
across all four call paths, the exact pre/post-threshold sizes from triage, and
one case that calls a bound host function from inside a large script, plus
`test_source_around_the_streaming_compile_threshold`.

## 3. The same bug class in `SnapshotBuilder`, closed pre-emptively

`SnapshotBuilder::new` had the identical precondition: it builds
`JsRuntimeForSnapshot` on whatever thread Python calls it from, with no tokio
runtime entered. It does **not** crash, and considerable effort went into
trying: 20 MB filler bootstraps, snapshots with 300 MB+ of heap data, 50 rounds
of ~500 MB GC churn, 60,000 top-level function declarations. None aborted. The
reason is that `will_snapshot = true` routes isolate creation through V8's
`SnapshotCreator`, which appears to force fully synchronous compilation for
determinism, so the `handle: None` is never consulted.

The fix was applied anyway: `create_runtime()` enters a minimal current-thread
tokio runtime around isolate creation. It costs nothing, removes the last
`register_isolate` call site in this codebase that could register
`handle: None`, and means a future V8 or `deno_core` that *does* schedule
background work during snapshotting finds a handle instead of aborting. This is
defence in depth, and it is recorded as such rather than as a fixed crash.

**What guards it now.** The `SnapshotBuilder` half of
`tests/test_large_script_eval.py`: large bootstraps through both the
constructor and `execute_script()`, and an end-to-end check that a snapshot
built from a large bootstrap still starts a working `Runtime`.
