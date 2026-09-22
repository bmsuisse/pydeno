# Benchmarks

Two suites: Rust-level (Criterion, `benches/`, bypasses the Python API) and
Python-level (pytest-benchmark, `benches_py/`, the real user-facing surface).
Both run in CI on every push/PR (`.github/workflows/benchmarks.yml`),
informational only -- they report numbers, they don't gate merges.

Numbers below are one real measured run, not simulated. They are
environment-dependent -- re-run locally before trusting them for a regression
decision on different hardware.

## Measurement environment

- Apple M2, 8 cores, 16 GB RAM, macOS 25.5.0 (Darwin), arm64
- Rust 1.93.1, `cargo bench --features bench` (release/`bench` profile)
- Python 3.14.3, `pytest-benchmark` 5.3.0, `maturin develop --release`

## Rust (Criterion, `cargo bench --features bench`)

| Benchmark | Time |
|---|---|
| `isolate_creation_and_close` | 2.52 ms |
| `simple_eval_throughput` (`1 + 41`) | 4.20 µs |
| `host_callback_op_dispatch` (real Python callable via `register_op`) | 13.14 µs |
| `termination_handle/is_terminated_check` | 0.92 ns |
| `termination_handle/terminate_round_trip` (idle runtime) | 87.16 µs |

`is_terminated()` is a single atomic load -- effectively free to poll. The
206x-larger `terminate_round_trip` number is the cost of the full path:
requesting termination, calling `v8::IsolateHandle::terminate_execution()`,
and waiting for the runtime thread to acknowledge shutdown.

## Python (`pytest-benchmark`, `pytest benches_py/ --benchmark-only`)

| Benchmark | Mean |
|---|---|
| `test_cold_start` (new `Runtime()` + one eval) | 2.82 ms |
| `test_steady_state_100_evals` (100 sequential evals, warm runtime) | 363.25 µs total (~3.63 µs/eval) |
| `test_steady_state_1000_evals` (1000 sequential evals, warm runtime) | 3.46 ms total (~3.46 µs/eval) |
| `test_host_callback_round_trip` (`bind_function` + call from JS) | 13.98 µs |
| `test_normal_completing_eval_baseline` | 3.77 µs |
| `test_watchdog_termination_overhead` | 59.62 ms |

A retained warm `Runtime` does a full host tool call
(`bind_function` → JS → host → value) in **13.4 µs**, and a plain `eval`
(`1+41`) in 3.77 µs — retaining one `Runtime` per session is the fast path
for tool-calling workloads.

Per-eval cost at the Python layer (~3.5-3.8 µs) matches the Rust-level
`simple_eval_throughput` number closely -- the Python binding adds negligible
overhead over the raw `RuntimeHandle`.

### Watchdog-termination proof, timed precisely

`test_watchdog_termination_overhead` runs the exact scenario in
`tests/test_termination_handle.py`: a runaway `while(true){}` eval, killed
from a separate watchdog thread via `TerminationHandle.terminate()`, but with
the watchdog's sleep shortened to 50 ms (20 rounds) so the suite stays fast.
Measured mean: 59.62 ms. Subtracting the artificial 50 ms delay leaves
**~9.6 ms** of real overhead for the cross-thread termination path itself
(watchdog wakes up, calls into V8's isolate handle from a foreign thread,
the runtime thread unwinds and returns control to Python) -- consistent with
the Rust-level `terminate_round_trip` number once you add Python's own
call/exception overhead on top.

## Retained runtimes: idle cost and scaling (v0.2.0)

Until v0.2.0, `RuntimeDispatcher::run` (`src/runtime/runner.rs`) selected
between `cmd_rx.recv()` and `tokio::task::yield_now()`. `yield_now` is always
immediately ready, so the loop never actually blocked: **every live `Runtime`
burned CPU continuously for its whole lifetime, whether or not it had any work
to do.** v0.2.0 parks the dispatcher when the event loop is drained and no job
is queued, and waits on a real waker (plus the active job's exact deadline)
while async work is in flight.

This matters because retaining one warm `Runtime` per session is the fastest
thing this library does (see the warm-tool-call figures above), and the
busy-spin was what capped that pattern at roughly the core count. Measured
on the environment above, with K
retained runtimes each holding a bound host function, idle after one call:

| K retained | idle CPU, v0.1.0 | idle CPU, v0.2.0 | per-call, v0.1.0 | per-call, v0.2.0 |
|---|---|---|---|---|
| 1 | 12.9% | **0.0%** | 15.4 µs | **9.8 µs** |
| 4 | 65.3% | **0.0%** | 13.1 µs | **9.7 µs** |
| 8 | 169.9% | **0.0%** | 14.5 µs | **9.6 µs** |
| 16 | 514.3% | **0.0%** | 29.5 µs | **9.9 µs** |
| 32 | 668.3% | **0.0%** | 35.1 µs | **9.7 µs** |
| 64 | 684.1% | **0.0%** | 18.2 µs | **9.8 µs** |

Idle CPU is `getrusage(RUSAGE_SELF)` user+system over a 2 s window with every
runtime idle, as a percentage of one core (so 800% is this 8-core machine fully
saturated). Per-call is the median of the best of five 1000-call trials of a
warm `eval` that crosses into Python and back, run on one of the K live
runtimes; the earlier trials in each run are slower purely from warm-up, and
the full per-trial series is in the commit message for this change.

Three things to read off it:

- **Idle cost went from linear in K to zero.** v0.1.0 cost ~13% of a core per
  idle runtime and saturated all 8 cores somewhere between K=16 and K=32. In
  v0.2.0 idle runtimes are genuinely parked and measure 0.0% at every K.
- **Per-call latency no longer degrades with K.** v0.1.0 was flat to K=8 and
  then ~2.3x worse at K=16-32, where the spinning threads outnumbered the
  cores. v0.2.0 is flat at ~9.8 µs from K=1 to K=64. The K=64 v0.1.0 row
  reading *better* than K=32 is not a recovery: at that point the machine is
  saturated and the numbers are dominated by scheduling noise, which is the
  regime the change removes.
- **v0.2.0 is also faster at K=1** (9.8 µs vs 15.4 µs), because the old
  spinning dispatcher thread was competing with the calling Python thread even
  in the single-runtime case.

Async paths were re-measured to confirm parking costs them nothing
(medians, `eval_async`):

| Path | v0.1.0 | v0.2.0 |
|---|---|---|
| `eval_async` resolved promise | 56.3 µs | 53.6 µs |
| `eval_async` microtask chain | 55.8 µs | 47.5 µs |
| async `bind_function`, not awaited | 117.0 µs | 114.6 µs |
| async `bind_function`, awaited | 152.0 µs | 120.2 µs |

`tests/test_idle_cpu.py` is the permanent regression test for all of this. It
asserts an idle-CPU budget per runtime and that per-call latency at K = 3x the
core count stays within 2x of its own K=1 baseline; all four cases fail against
v0.1.0's dispatcher and pass against v0.2.0's.

## Bounded termination for parked promises (v0.2.0)

`TerminationHandle.terminate()` is the only kill switch callable from another
thread. It flips a shared flag and calls V8's `terminate_execution()` -- and
that second half does nothing to a runtime parked on a *pending promise*,
because V8 only trips a termination when it next **enters** JavaScript and a
drained-but-pending event loop never re-enters. Nothing in the dispatcher
consulted the flag, so such a request was never observed: the caller blocked
forever and the runtime could not be killed at all.

The dispatcher now checks that flag between polls. It costs one atomic load per
iteration and needs no new timer, because a runtime with a job in flight is
already waking at least every `PENDING_WORK_TICK`.

> **Update, v0.2.1.** Relying on the tick here made the tick *load-bearing*
> rather than the backstop it was intended to be -- with the tick raised, these
> tests failed outright. `TerminationController::request` now signals the
> dispatcher's waker directly, so the flag check is reached on the next loop
> iteration instead of on the next tick. The parked medians below improve from
> ~1.4-1.9 ms to ~0.3-0.4 ms, and the suite passes with the tick raised to an
> hour. See [Arming a timeout](#arming-a-timeout-v021).

Kill latency, measured from `terminate()` being called to the blocked caller
raising, 30 samples per shape:

| Stuck script | before | after (med / p95) | worst seen |
|---|---|---|---|
| `while(true){}` (V8 unwind) | 0.09 ms | **0.11 / 0.16 ms** | 2.55 ms |
| `new Promise(() => {})` | never | **1.71 / 2.71 ms** | 3.41 ms |
| `await` on a parked promise | never | **1.59 / 2.66 ms** | 4.02 ms |
| `.then` chain on a parked promise | never | **1.36 / 2.69 ms** | 2.84 ms |

Medians and p95 are from one representative 30-sample run per shape; the
"worst seen" column is the maximum observed across repeated runs, which is the
figure the grace period below is sized against.

The two tiers stay distinct, which is the point: `while(true){}` is still
killed by V8 unwinding JS directly and does **not** pay for the dispatcher's
poll interval. Nothing here turns a timeout into a runtime recreate -- a
politely killed runtime keeps its bound host functions and globals, asserted by
`test_polite_kill_does_not_recreate_the_runtime`.

`timeout=` was already honest on all three parked shapes (the parking work's
`RuntimeJob::deadline()` clamp) and is unchanged: 302 ms median for a 300 ms
timeout on every shape. `test_terminate_beats_a_long_timeout` guards the
difference -- with a 5 s timeout and a terminate at 100 ms, the termination path
is what has to fire.

### Escalation for a wedged runtime thread (`force_kill_grace`, opt-in)

Neither polite tier can reach a runtime whose *thread* is wedged inside a host
callback that never returns: the dispatcher is stuck below `eval_sync` in
Python, so it never reaches the flag check, and V8 never re-enters JS.

Setting `RuntimeConfig(force_kill_grace=...)` makes a blocked caller give up
after that grace period, mark the runtime dead and raise `RuntimeForceKilled`
(a subclass of `RuntimeTerminated`). Measured: a blocking sync host op with
`terminate()` at 100 ms and a 150 ms grace releases the caller at ~250 ms,
versus never.

This is opt-in because it is not free. Comparing both arms in one process
(interleaved, 7x2000 calls per arm, four repeats), slicing the blocking wait
costs about **+2..6% on `eval('1+1')` and +9..13% on a bound-function call**.
That is a bad trade to impose on every healthy call for a pathological case, so
`force_kill_grace` defaults to `None`, where the wait is a literal `recv()` --
the previous code path exactly. `pydeno.SUGGESTED_FORCE_KILL_GRACE` (0.1 s) is
~25x the slowest polite kill observed above (4.02 ms), so a runtime that would
have died politely always gets the chance to, with wide margin for a loaded
machine.

It also does not *reclaim* the wedged thread: a V8 isolate cannot be dropped
from another thread, so the thread is abandoned, holding its isolate until the
host call returns (if ever), at which point V8's latched termination unwinds it.
Deno's hosted sandbox has the same limit and answers it with SIGKILL on the
whole process, which a library cannot do. Recreating is left to the caller, at
the normal cost of a new runtime (~2.8 ms cold, ~1.3 ms from a snapshot).

`tests/test_parked_termination.py` is the permanent regression test: 15 cases
covering all three parked shapes, both polite tiers, the escalation, and the
"polite kill preserves state" guarantee. Five of them fail against the
unmodified dispatcher. Every blocking call in that file runs on a joined daemon
thread, so the old behaviour shows up as a readable failure in seconds instead
of hanging the suite -- and nothing in it is `skipif`-gated, since the absence
of exactly this test is what let the gap survive two releases.

## Known pre-existing environment flake (not caused by this release's work)

While re-running these benchmarks, `cargo bench --features bench` and the
full `pytest benches_py/`/`pytest tests/` runs intermittently abort the whole
process with a V8-internal panic ("`V8 posted a delayed task, but this
isolate was created outside of a tokio runtime context`" or a GC-time
`SIGABRT` under heap-limit/high-concurrency scenarios). This reproduces
identically on a clean checkout of `main` predating this release's own
changes, so it predates this work -- it looks like a `deno_core` 0.409.0 /
`v8` 150.4.0 interaction, not something introduced here.
Affected pre-existing tests: `TestRuntimeHeapLimits::test_*_eval_triggers_heap_termination`
and `TestRuntimeTimeout::test_concurrent_sync_operations_different_timeouts`
in `tests/test_runtime.py`, and the `simple_eval_throughput`/`host_callback_op_dispatch`/
`termination_handle` Criterion benches when run in the same process as
`isolate_creation_and_close`. Numbers in this document were captured by
running the unaffected benches individually
(`cargo bench --bench runtime_benches -- "isolate_creation_and_close|pooled_isolate_checkout_eval_release"`,
`pytest benches_py/test_bench_runtime.py::test_cold_start benches_py/test_bench_runtime.py::test_pooled_checkout_eval_release --benchmark-only`),
which avoids the flake and still gives a real, reproducible before/after
comparison. Investigating/fixing the underlying flake is separate follow-up
work, tracked as a known issue rather than silently worked around.

## Arming a timeout (v0.2.1)

Every tool-calling figure in this file above -- including the headline 13.4 µs
warm host call -- was measured **without `timeout=`**. That turned out to be
the only configuration in which those numbers were reachable.

Through v0.2.0, `SyncWatchdog` found out it had been cancelled by polling an
`AtomicBool` from a `thread::sleep(10ms)` loop, while the runtime thread
cancelled it and then *joined* it. The join therefore blocked for the
remainder of the watchdog's current sleep chunk. Every call with a deadline
armed paid a fixed ~13 ms (macOS overshoots a 10 ms sleep request) on top of
its real work, and the cost did not depend on the deadline's value at all:

| Host tool call | v0.2.0 | v0.2.1 |
|---|---|---|
| no deadline | 0.061 ms | 0.068 ms |
| `timeout=0.5` | **13.48 ms** | **0.092 ms** |
| `timeout=5` | **14.44 ms** | **0.083 ms** |
| `timeout=300` | **14.07 ms** | **0.072 ms** |

Medians of 60 samples on one warm runtime, release build. 0.5 s and 300 s
costing the same is the signature: this was a fixed polling interval, not a
deadline being approached.

This mattered more than a microbenchmark usually does, because a production
caller *must* arm a timeout -- it is the only kill switch for runaway guest
code -- so the library's advertised fast path was unreachable in any safe
configuration.

v0.2.1 replaces the poll loop with a `Condvar`: the cancel is signalled and the
join returns immediately. The armed path is now ~1.2x the unarmed one, the
residual being the watchdog thread spawn and join, which is real work. The
whole `pytest` suite also drops from ~118 s to ~54 s, since every timed test in
it was paying the same toll.

`PENDING_WORK_TICK` (1 ms) was **not** the cause and was never on this path;
`DispatcherWaker` already covers op completion. With this fix plus the
signalled termination request described above, the tick is finally the pure
backstop it was documented as: raised to one hour, the full 548-test suite
passes and host-call latency is unchanged.

`tests/test_timeout_overhead.py` is the permanent regression test. It asserts
the armed path's median stays within 15x the unarmed path's median, measured in
the same process on the same warm runtime -- a ratio, because absolute timings
are the part that moves between machines. Against v0.2.0 it fails at ~265x.

## The job-consolidation refactor costs nothing (v0.3.0)

0.3.0 replaced five near-identical `RuntimeJob` state machines with one
`PromiseJob` plus four closures (`src/runtime/runner.rs`, -224 net lines).
Everything that refactor touches is on a latency path, so it was measured
rather than asserted: the same script against `main` @ 11fa913 and against the
refactor, release builds, same machine and session.

| | main @ 11fa913 | 0.3.0 |
|---|---|---|
| Host tool call, no deadline | 0.019 ms | 0.018 / 0.016 ms |
| Host tool call, `timeout=0.5` | 0.017 ms | 0.017 / 0.016 ms |
| Host tool call, `timeout=5` | 0.017 ms | 0.016 / 0.016 ms |
| `eval_async` on a resolved promise | 0.049 ms | 0.049 ms |
| Idle CPU, 8 retained runtimes | 0.0% | 0.0% |
| Parked kill, `new Promise(() => {})` | 8.97 ms | 9.56 / 10.05 ms |
| Parked kill, `await` on a parked promise | 8.84 ms | 9.01 / 10.10 ms |
| Parked kill, `.then` on a parked promise | 9.99 ms | 8.52 / 10.15 ms |

Two columns for 0.3.0 because two runs were taken: the spread between repeats
of the *same* build is as large as the spread between builds, which is the
point. The armed/unarmed ratio stays ~1.0, so the 0.2.1 `Condvar` fix is
intact; idle CPU stays at zero, so the parking is intact; and the parked kills
are unchanged, so the termination-flag check between polls still runs for every
job.

The parked-kill figures here are ~9 ms rather than the ~0.3 ms in the section
above because this harness charges the killer thread's own setup to the
measurement (`asyncio.run` plus a `Runtime` construction between `t0` and the
`terminate()`). It is a *comparison* harness, not a replacement for the
absolute numbers; both columns pay the same overhead. The absolute figures are
the ones in [Bounded termination for parked promises](#bounded-termination-for-parked-promises-v020).

The suite's own permanent regression tests are the other half of this evidence
and all pass in both profiles: `test_timeout_overhead.py` (armed vs unarmed
ratio), `test_idle_cpu.py` (idle budget and latency flatness to K=3x cores),
`test_parked_termination.py` (15 cases over both kill tiers) and
`test_tool_bridge.py` (budgets across the op boundary).

## One watchdog thread per runtime, not one per call (0.4.0)

Every timed call -- sync eval, sync function call, sync module eval, and any
async job with a timeout -- used to spawn *and join* a whole OS thread just
to arm a single deadline. Measured on this checkout, release build:

| Benchmark | Before (thread per call) | After (persistent watchdog) |
|---|---|---|
| `timed_eval_throughput` (Criterion, 5s timeout armed) | ~28.5 us | **9.5 us** |
| `simple_eval_throughput` (Criterion, no timeout, for comparison) | 6.9-10.4 us | 6.9-10.4 us (unchanged) |
| `test_timed_eval_baseline` (pytest-benchmark) | not previously benched | **9.1 us** |
| `test_normal_completing_eval_baseline` (pytest-benchmark, for comparison) | 7.4 us | 7.4 us (unchanged) |

A timed eval now lands within noise of an untimed one, rather than ~3-6x it,
matching the "timed eval ~= untimed eval" target. `Watchdog::arm`/`disarm`
(one long-lived `pydeno-watchdog` thread per runtime, parked on a condvar over
a small set of armed deadlines) replace the old spawn-and-join
`SyncWatchdog`. See `bench_timed_eval_throughput` (Criterion) and
`test_timed_eval_baseline` (pytest-benchmark) for the standing regression
checks.

`RuntimeDispatcher::run` also now arms this same watchdog around each
`poll_event_loop` call whenever no job currently holds a deadline (S3b): a
fire-and-forget host call whose continuation starts a self-requeuing
microtask loop after the job that started it has already completed used to
ignore `execution_timeout` entirely, hanging the runtime. See
`tests/test_dispatcher_step_timeout.py`.

## P2 (code cache) and P3 (measured optimization) -- release notes

**P2 -- not implemented; blocked by the public API surface.** The plan called
for `execute_script_with_cache`, keyed by source hash, for sources over
64 KiB. That method exists only on `deno_core::JsRealm`
(`runtime/jsrealm.rs`), and the only way to obtain a `JsRealm` handle from a
`JsRuntime` is `JsRuntime::main_realm()`, which is `pub(crate)` inside
`deno_core` -- not reachable from an external crate. Implementing this would
mean hand-rolling V8 script compilation and caching directly against the
`v8` crate (bypassing `execute_script` entirely, including its source-map
and error-reporting behavior), which is a materially larger and riskier
change than the plan's "use `execute_script_with_cache`" framing describes.
Skipped rather than attempted as a from-scratch reimplementation; a real fix
needs either a `deno_core` upstream change exposing this, or a deliberate
follow-up scoped as its own review item.

**P3 -- measured, and the named candidate does not clear the 20% bar.**
`samply` and Linux `perf` are unavailable in this environment (macOS,
sandboxed, no `dtrace`/Instruments access), so this used direct timing
instead of a real profiler. Isolating the fixed per-object overhead
(`hostFn({})` vs `hostFn([])`, i.e. an empty object vs an empty array, so
neither pays for iterating properties/elements) gives a delta of ~1.85-2.0
us against a ~12.5 us baseline -- real, but well under 20%. Swapping the
named candidate, `is_readable_stream`'s `v8::Value::instance_of` call, for a
prototype-chain identity walk (avoiding `[[HasInstance]]`'s
`Symbol.hasInstance` lookup) was implemented and A/B measured directly
(same build, same benchmark, only that one function changed): the
object-vs-array delta was 1.851 us before and 2.006 us after -- no
measurable improvement, within noise. `instance_of` is not the dominant cost
of that fixed overhead (likely `get_own_property_names` and the `IndexMap`
allocation, paid even for zero keys); per the plan's own instruction to act
only on something confirmed and significant, this change was reverted rather
than landed. The other named candidate (the JS `prepare()` deep map) was not
separately investigated given the first candidate already failed the bar and
no profiler was available to attribute cost with confidence.

## Reproducing

```bash
cargo bench --features bench
uv sync --group all
uv run maturin develop --uv --release
uv run pytest benches_py/ --benchmark-only
```

If the full run hits the pre-existing flake above, re-run the two pooling
benches directly (see the exact commands in that section) to reproduce just
the pooling numbers.
