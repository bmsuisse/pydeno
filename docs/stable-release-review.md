# Stable-release review

Review of `main` at `40eb47d` (v0.2.0, dispatcher-parking fix merged). Review
only -- no source was changed by this pass.

> **Status, 0.3.0.** Every finding below has now been actioned or answered.
> M1-M6, T2 and the fuzz-strategy work landed in v0.2.1. The remainder landed
> in 0.3.0: R1/R2/R3/R4 (the job state machines, `SnapshotSource`, the
> duplicate depth counter, the empty impl), R5 (the three `PATCH*.md` files are
> now `docs/contributing/upstream-divergence.md`), S1 (README framing), S3
> (`call_async` returns a coroutine), T1 (the op-registry leak has a
> regression test). Two findings were answered rather than implemented, with
> reasons, and the docs now say so: **S4** -- `ToolNotFoundError` stays as
> vocabulary for user tools and its docstring no longer claims `pydeno` raises
> it, because an unexposed tool name correctly produces V8's own `TypeError`;
> **S5** -- there is no permission model and none is missing, the real boundary
> being capability tokens plus `ToolBridge` scoping (see
> `docs/contributing/architecture.md`). S6 is corrected in `CLAUDE.md`.
> **Line numbers in this document are as-reviewed and no longer match the
> tree** -- notably R1's table, since `runner.rs` has been rewritten there.
>
> Still open, deliberately: **S2** (two error types for "the runtime is gone"
> -- unifying them is a breaking change worth a considered pass, not a
> drive-by), **S7** (the two missing SAFETY comments and the detached
> `ArrayBuffer` `[suspect]`, which still wants a repro before anyone acts), and
> **S8** (the byte-limit message now names the setting, but `RuntimeError` is
> still the type).

## How to read the evidence labels

Every finding carries one of three labels, because "I read it" and "I ran it
and watched it happen" are not the same claim:

- **[ran]** -- reproduced by executing code. The repro is given inline.
- **[read]** -- established by reading the source. Cited with `file:line`.
- **[suspect]** -- a hypothesis from reading that I did **not** confirm. Needs
  a test before anyone acts on it.

Two builds were used, and the difference matters for one finding:

- `pydeno 0.2.0` built from this tree with `make build-dev` (debug), the
  documented developer build.
- `pydeno 0.1.1` from PyPI (release profile). `git diff 71d73ea main` touches
  only `runner.rs`, `BENCHMARKS.md`, `docs/tool-calling-at-pool-speed.md`,
  `tests/test_idle_cpu.py` and the version, so `ops.rs`, `conversion.rs`,
  `js_value.rs` and `pool.rs` are byte-identical between the two. Findings in
  those files were verified on both.

Machine: macOS 25.5, arm64, CPython 3.14 (source build) / 3.12 (wheel).

Two workstreams were read and are deliberately **not** re-litigated here: the
"guaranteed kill" escalation for runtimes parked on pending promises, and the
pptx/large-bundle verification effort. Where a finding of mine touches the
first, it says so and stops.

---

# MUST change before calling this stable

## M1. Nothing runs the test suite. [ran] [read]

`.github/workflows/` contains exactly three workflows:
`workflow.yaml` (`name: Publish to PyPI`, `on: release`), `benchmarks.yml`
(runs `benches_py/ --benchmark-only`), and `docs.yml`. **No workflow runs
`pytest tests/`, and none runs `cargo test`.**

So the 470 tests are 470 tests someone can run, not 470 tests that are run.
Every other test-quality question in this document is downstream of this one:
a weak assertion in an unenforced suite costs nothing, and a strong assertion
in an unenforced suite protects nothing.

This is not theoretical. M2 below is a hard process crash that the repo's own
fuzz suite catches on the first try, and it is sitting on `main`.

**Fix:** a `test` job on push/PR that runs `pytest tests/` and `cargo test`,
in both debug and release (see M2 for why both).

## M2. Guest JS crashes the host process with SIGBUS in a debug build. [ran]

Running the repo's own documented workflow:

```
$ make build-dev          # uv run maturin develop --uv
$ .venv/bin/python -m pytest tests/ -q -p no:randomly
.............................................
$ echo $?
132
```

Exit 132 = 128+4 (SIGILL); re-running standalone gives 138 = 128+10 (SIGBUS).
The suite does not fail, it **dies**, at
`tests/test_fuzz_eval_boundary.py::TestAdversarialSource::test_deeply_nested_literals`
-- test #46 of the run. Everything after it (roughly 20 remaining fuzz tests
and nine whole test files, alphabetically `test_gil_and_limits.py` onward)
never executes.

Minimal repro, debug build:

```python
from pydeno import Runtime
with Runtime() as rt:
    rt.eval("[" * 80 + "]" * 80)     # SIGBUS, whole process
```

Depth 50 is fine; depth 80 and up kills the process. On the release wheel the
same input is refused cleanly at every depth I tried (100 → OK, 200..2000 →
`RuntimeError: Depth exceeded maximum limit of 100`, 5000 → `RangeError`), so
**this is optimization-level-dependent, not universal.** That is precisely why
it must be fixed rather than shrugged at.

Root cause [read]: no spawned thread sets a stack size.
`src/runtime/runner.rs:2033` and `src/runtime/pool.rs:124` both do
`thread::Builder::new().name(...).spawn(...)` with no `.stack_size()`, so the
V8 isolate *and* the recursive host-side serializer
(`RuntimeCoreState::value_to_js_value_internal`, `runner.rs:3225` onward, which
recurses once per level up to `MAX_JS_DEPTH = 100`) run on the platform default
thread stack -- 512 KB on macOS, 8 MB on glibc. Debug frames are several times
larger than release frames, so the same `MAX_JS_DEPTH = 100`
(`src/runtime/js_value.rs:15`, wired as the default at
`src/runtime/config.rs:257`) is comfortably inside the budget in one build and
past the guard page in the other.

A library whose stated invariant is "a result or a catchable Python exception
-- never SIGABRT, never a Rust panic, never a hang"
(`tests/test_fuzz_eval_boundary.py:7-8`) cannot have that invariant depend on
`-O`.

**Fix:** set an explicit `.stack_size()` on the runtime and pool threads, sized
with real headroom for `MAX_JS_DEPTH` debug-profile frames, and add a debug-mode
CI run so the difference can never hide again. Deriving the depth limit from
measured stack headroom instead of a hardcoded 100 would be the more honest
version.

## M3. The JS→Python direction enforces neither `max_serialization_bytes` nor `max_serialization_depth`. [ran] [read]

This is the same bug class the project already found and fixed once -- on the
other side of the boundary. `src/runtime/conversion.rs:165-175` documents the
fix in its own words:

> Needed because `max_serialization_bytes` is meant to cap what a single call
> transfers, not what each of its arguments transfers independently: a fresh
> tracker per argument lets N arguments each just under the limit through, for
> N times the intended budget.

That reasoning was applied to Python→JS (`python_to_js_value_tracked`, used at
`src/runtime/python/runtime.rs:634` for `JsFunction` arguments) and is
regression-tested in `tests/test_gil_and_limits.py:111-166`. It was never
applied to JS→Python. Both op entry points convert each guest argument with
**no limits object at all**:

```rust
// src/runtime/ops.rs:182  (sync)   and  src/runtime/ops.rs:226  (async)
js_value_to_python(py, arg, None).map_err(map_pyerr)
```

`None` there is the `RuntimeHandle` parameter, but the point stands: the
inbound path has no `LimitTracker` in it anywhere, aggregate or per-argument.

### M3a. Byte budget: measured 3.6x over the configured cap in one call

```python
with Runtime() as rt:                      # default max_serialization_bytes = 10 MB
    rt.bind_function("measure", lambda *a: sum(len(x) for x in a))
    rt.eval("measure('x'.repeat(9*1024*1024))")                       # -> 9437184
    rt.eval("measure(...Array(4).fill('x'.repeat(9*1024*1024)))")     # -> 37748736
```

37.7 MB accepted against a 10 MB limit, in a single call. Nothing in the code
bounds the argument count, so this scales linearly: the guest picks the
multiplier. Note that `max_heap_size` does not cover this either -- the cost is
host-side Python and Rust allocation, outside the V8 heap.

### M3b. Depth: the configured limit is silently ignored inbound

```python
cfg = pydeno.RuntimeConfig(max_serialization_depth=3)
with Runtime(cfg) as rt:
    rt.bind_function("sink", lambda v: "ok")
    rt.eval("let a={};let c=a;for(let i=0;i<10;i++){c.n={};c=c.n};sink(a)")
    # -> 'ok'          (depth 10 accepted against a limit of 3)
    rt.eval("let b={};let d=b;for(let i=0;i<10;i++){d.n={};d=d.n};b")
    # -> RuntimeError: Depth exceeded maximum limit of 3
```

Identical payload, identical runtime, identical limit: rejected as an `eval`
result, accepted as an op argument. What actually stops runaway inbound depth
today is `serde_v8`'s own internal recursion limit -- a fixed constant in a
dependency, not the knob the user set.

**Fix:** thread one `LimitTracker` built from the runtime's
`SerializationLimits` through the whole `args` loop in both ops, exactly as
`python_to_js_value_tracked` does outbound, and assert the symmetry in tests
(see T1).

## M4. A JS function handed to a host tool silently becomes `{}`. [ran]

```python
with Runtime() as rt:
    got = []
    rt.bind_function("take", lambda cb: got.append(cb) or "stored")
    rt.eval("take(function(){return 7})")   # -> 'stored'
    rt.eval("take({cb: () => 1})")          # -> 'stored'
    print(got)                              # -> [{}, {'cb': {}}]
```

No error, no warning: the callable is replaced by an empty dict, at both top
level and nested. A host tool that accepts a callback -- `onProgress`,
a comparator, a continuation -- receives `{}` and fails somewhere else, or
worse, silently treats it as an empty options object.

The value never reaches `js_value_to_python`'s `JSValue::Function` arm
(`conversion.rs:127-140`) because `#[serde] args: Vec<JSValue>` goes through
`serde_v8`, and `JSValue`'s hand-written `Deserialize` has no function
representation at all (`js_value.rs:156-332`) -- a V8 function arrives as an
object with no own enumerable properties. So `Function { id }` is
constructible only on the `eval`-result path (`runner.rs:3279-3305`), which
does not use serde.

Silent truncation at a trust boundary is the worst available failure mode, and
"bidirectional calling" is the library's headline feature. Pick one and commit
to it before the API is frozen:

- **Refuse it.** Detect a callable argument and raise, so the caller learns
  immediately. Cheap, and honest.
- **Support it.** Register the function in `fn_registry` on the way in and hand
  the tool a real `JsFunction`. More work, and it needs a documented answer for
  what a held `JsFunction` means once the call that supplied it has returned.

Either is defensible. `{}` is not.

## M5. Guest JS can enumerate the op registry and read op names out of error messages. [ran] [read]

`__host_op_sync__(id, ...)` is an ambient global that takes a raw integer op id
(`ops.rs:453-470`), and op ids are allocated sequentially from zero
(`ops.rs:88`). So any op registered on a runtime is callable by guest JS
regardless of how it was bound:

```python
with Runtime() as rt:
    seen = []
    rt.bind_function("__never_called_from_js", lambda c: seen.append(c) or "PWNED:"+c)
    rt.eval("__host_op_sync__(0, 'via-forged-id')")   # -> 'PWNED:via-forged-id'
    print(seen)                                        # -> ['via-forged-id']
```

To be clear about intent: this is not a hole someone left open. `tests/test_ops.py:84`
and `tests/test_runtime.py:1607` use `__host_op_sync__(op_id, ...)` as the
supported JS-side calling convention. The problem is what that design *implies*,
unstated, for a stable release:

1. **`ToolBridge(namespace=...)` provides naming, not isolation.** The namespace
   is a property on a global object; the ops behind it are reachable without it.
   Two bridges with different trust levels on one `Runtime` are one trust level.
   The budget does survive -- the registered handler *is* the budgeted shim
   (`_tools.py:214-239`) -- but only because the shim is the thing registered,
   which is luck-shaped rather than design-shaped.
2. **There is no revoke.** `PythonOpRegistry` has `register` and `get`
   (`ops.rs:79-104`) and no removal. An op is callable for the runtime's life.
3. **Names leak.** Calling an async op through the sync bridge returns
   `format!("Op {} is not synchronous", entry.name)` (`ops.rs:169-172`, and
   symmetrically `:206-210`), so a guest loop over ids harvests the names of
   host tools it was never given. An unknown id yields `Unknown Python op id
   {}` (`ops.rs:117`), which makes the registry's size enumerable too.
4. **Host exception text is forwarded verbatim.** `map_pyerr` (`ops.rs:144-154`)
   sends `err.value(py).to_string()` to the guest. Combined with (1), a guest
   can probe an unexposed tool with junk arguments and read the host's
   `TypeError` back. Observed: `0:TypeError:can only concatenate str (not
   "int") to str`.

None of this is a sandbox escape. All of it is a capability model that a stable
release will be held to.

**Fix, in priority order:** (a) stop putting `entry.name` in guest-visible
errors -- the mode-mismatch and unknown-id messages should be uniform and
name-free; (b) document, in the `ToolBridge` and `register_op` docstrings, that
the op registry is a single flat ambient capability per `Runtime` and that
namespaces are cosmetic; (c) decide whether `revoke`/per-binding
unguessable ids are worth adding now, because adding them later is a breaking
change to the JS-side convention.

## M6. pydeno's own internal error strings reach guest JS. [ran] [read]

The existing leak tests
(`tests/test_known_escape_techniques.py:278-315`) check a JS-thrown error and a
Python-raised tool error, and check for host paths, `.cargo`, `site-packages`
and `Traceback`. They cannot fail on pydeno's *own* error text, which is where
the leakage actually is:

```
TypeError: serde_v8 error: recursion limit exceeded
```

-- a dependency crate name, handed to guest JS, observed on both builds. Same
class, all guest-reachable [read]: `"Python op registry is missing"`
(`ops.rs:112`), `"GlobalTaskLocals not found in OpState"` (`ops.rs:216`),
`"Serialization limits not configured"` (`ops.rs:177`, `:221`),
`"PyStreamRegistry is missing"` (`ops.rs:279`).

These describe host-side data structures by name. They are also useless to the
guest, which cannot act on any of them.

**Fix:** map internal/infrastructure failures to one opaque guest-facing
message and keep the detail on the host side. Extend the leak test to assert on
`serde_v8`, `OpState`, `registry`, `deno_core` and `JsErrorBox` -- an assertion
that would have caught this.

---

# SHOULD change before calling this stable

## S1. `IsolatePool` is 21x slower than the thing it is offered as an alternative to, and the README still recommends it. [ran] [read]

Measured on the 0.1.1 release wheel, 3000 iterations each, same process:

| path | per-op |
| --- | --- |
| `IsolatePool` checkout held, `eval("1+1")` | **195 µs** |
| `IsolatePool`, fresh `checkout()` per call | **218 µs** |
| warm `Runtime`, `eval("1+1")` | **9.1 µs** |
| warm `Runtime`, full host tool call `eval("add(1,2)")` | **13.2 µs** |

A *complete host tool call* on a retained `Runtime` is 15x faster than a bare
`eval` on a pooled isolate. And `PooledIsolate` exposes exactly two methods:

```
[PooledIsolate] ['eval', 'release']
```

No `bind_function`, no `register_op`, no `eval_async`, no `termination_handle`,
no `get_stats`. Confirmed by attribute error on each.

`docs/tool-calling-at-pool-speed.md:13-21` already reaches this conclusion in
the project's own words ("That premise is wrong, and measurement inverts it").
The API and the README have not caught up. `README.md:179` still says: "check
out an isolate from a warm `IsolatePool` instead of ...". A reader who follows
that advice gets a 21x slowdown and loses ops.

The pool is not worthless -- 195 µs for a *guaranteed-fresh* context beats
~3 ms for a fresh `Runtime` by ~15x, and I confirmed the isolation is real
(`globalThis.__leak` set in one checkout is `undefined` in the next). That is a
genuine niche: stateless, mutually-untrusting, one-eval-each.

**My recommendation: keep the capability, demote the framing.** Do not make it
the recommended path for anything, and do not freeze `IsolatePool` /
`PooledIsolate` / `checkout` / `release` as headline API on the strength of a
number that is 21x off. Concretely: rewrite `README.md:176-190` to lead with
"a fresh context per call, ~195 µs, no ops -- use this only when a fresh
context is the requirement; otherwise retain a `Runtime`", and put the
comparison table in the docs next to it. If you would rather not commit to the
names at all, moving it behind `pydeno.experimental` before 1.0 is the cheaper
door to walk through than deprecating it after.

## S2. Two error types mean "the runtime is gone". [ran]

```python
rt.close();      f(21)   # -> RuntimeError: Function call failed: Runtime has been shut down
h.terminate();   g()     # -> RuntimeTerminated: Function call failed: Terminated via ...
```

Same condition, two exception types, so every defensive caller writes
`except (RuntimeError, RuntimeTerminated)`. `RuntimeTerminated` is the more
useful one; a closed runtime should raise it too (or a shared base). Worth
settling before the type is a promise.

## S3. `JsFunction.call_async` returns a `Future`, not a coroutine. [ran]

```python
asyncio.create_task(g.call_async())
# TypeError: a coroutine was expected, got <Future pending ...>
```

`await f.call_async(5)` works; `asyncio.create_task(...)` does not, and
`asyncio.gather` on several of them has the same shape problem.
`asyncio.ensure_future` is the workaround. Either return a coroutine or say so
loudly in the docstring and stub -- `create_task` is the first thing a user
reaches for when they want two tool calls in flight.

## S4. `ToolNotFoundError` documents behaviour that does not exist. [read]

`python/pydeno/_tools.py:63-64`: "Raised when JS asks for a tool the bridge does
not expose." Nothing in the module raises it, and there is no code path that
could: a tool the bridge does not expose is simply not a property on the
namespace object, so JS gets a plain `TypeError: ... is not a function` from
V8. Either wire it up (an own-property-less namespace via a `Proxy` trap would
do it) or delete the class. Exporting an exception that never fires is a
promise to keep exporting it.

## S5. Documented behaviour that does not exist: the permission model. [read]

`docs/contributing/architecture.md:273` -- "Permission-based (ops require
specific permissions)". `CLAUDE.md` says the same twice: "Ops System
(`src/runtime/ops.rs`): Permission-based host function registry", and under
Common Pitfalls, "Ops requiring permissions will fail if runtime not granted
those permissions via `RuntimeConfig`".

`grep -rni permission src/` returns **zero** hits. There is no permission
concept in the Rust source, and `RuntimeConfig` has no permission field
(`bootstrap`, `enable_console`, `initial_heap_size`, `inspector`,
`max_heap_size`, `max_serialization_bytes`, `max_serialization_depth`,
`on_console`, `snapshot`, `timeout`). This is the most load-bearing doc/code
divergence in the repo, because it is a *security* claim, and it interacts
directly with M5: a reader who believes ops are permission-gated will not
notice that they are ambient.

## S6. `CLAUDE.md` describes a layout the repo no longer has. [read]

Beyond S5: it points at `src/runtime/python.rs` (now the `src/runtime/python/`
package) and `docs/internals/` (does not exist; the architecture doc lives at
`docs/contributing/architecture.md`). It documents no `ToolBridge`, no
`JsFunction`, no `IsolatePool`, no `TerminationHandle`, and none of the
serialization limits. It is the file that tells an agent how this codebase
works, and on the three subjects this review cares about most it is wrong.

## S7. Two `unsafe` blocks have no SAFETY comment. [read]

`src/runtime/runner.rs` gets this right twice (`:224`, `:241`). The other two
do not:

- `src/runtime/conversion.rs:222` -- `unsafe { py_bytearray.as_bytes() }`. The
  invariant (the GIL is held and no arbitrary Python runs before `to_vec()`)
  holds as written, but it is exactly the kind of invariant that a later edit
  breaks silently.
- `src/runtime/runner.rs:3324` -- `ptr::copy_nonoverlapping` out of an
  `ArrayBuffer`. Separately, the `if let Some(data_ptr) = array_buffer.data()`
  around it means a **detached** `ArrayBuffer` silently yields
  `byte_length()` zero bytes rather than an error. Guest JS can detach a buffer
  (structured-clone transfer) between `byte_length()` and `data()`. Worth an
  explicit refusal instead of zeros [suspect -- I did not build a repro].

## S8. Error messages do not name the knob that rejected the call. [ran]

```
RuntimeError: Size (11534336 bytes) exceeded maximum limit of 10485760 bytes
```

Nothing in that string says `max_serialization_bytes`, and it arrives as a bare
`RuntimeError`. A user hitting the 10 MB wall with a real payload has to grep
the docs to learn there is a knob. `js_value.rs:379-388` should name the
setting and the type should be specific enough to catch.

---

# Test quality

The suite is better than a raw count of 470 suggests, and the fuzz suite in
particular is not the "eval entry point only" shape I was told to expect. It
*does* reach the marshaling boundary: `TestValueRoundTrip` drives values out
through a tool's return value and back in as another tool's argument, i.e.
through `prepare`/`revive` in the bridge JS, and `TestAdversarialHostCallArguments`
fuzzes the argument direction on purpose. `TestKnownConversionResiduals` pins
documented lossiness so a change to it is visible. That is real work.

The gaps are specific.

## T1. Regression coverage for the seven known bugs

| bug | regression test | verdict |
| --- | --- | --- |
| cross-thread termination panic | `test_fixed_termination_handle_kills_runaway_loop`, `test_the_process_survives_a_terminated_runtime`, plus `test_pathological_regexes_are_interruptible` (docstring names `PATCH.md`) | **covered** [read] |
| uncatchable SIGABRT (large script / snapshot) | `tests/test_large_script_eval.py` (8 tests), `test_source_around_the_streaming_compile_threshold` | **covered** [read] |
| GIL deadlock on a logging bootstrap | `test_constructing_a_runtime_does_not_deadlock_on_a_logging_bootstrap` | **covered** [read] |
| prototype corruption via `__proto__` | `test_a_dunder_proto_key_round_trips_through_the_op_paths`, `test_dunder_proto_survives_from_js_back_to_python`, `test_bind_object_nested_dunder_proto_is_a_known_residual` | **covered** [read] |
| GIL held across a blocking round trip | `TestGilIsReleasedAcrossBlockingCalls` (with a documented pre-fix 1.04x / post-fix 1.91x) | **covered** [read] |
| per-argument vs aggregate serialization budget | `TestSerializationBudgetIsAggregate` -- but **only Python→JS**. The JS→Python half of the same boundary has no equivalent test, and is still broken (M3) | **half-covered** |
| memory leak (op-registry `Rc` in embedder slot 0) | nothing. `test_rapid_checkout_release_churn_does_not_grow_rss` (`test_stress_concurrency.py:67`) measures pool checkout churn, and `test_no_global_state_leaks_across_reused_isolate` is about state visibility, not RSS. Neither exercises create/close of a `Runtime` *with ops registered*, which is where the leak was | **not covered** |

So: **the memory leak has no regression test at all**, and **the serialization-budget
bug has a test for the half that was fixed and none for the half that was not**
-- which is how M3 survived.

## T2. Three fuzz tests cannot fail on what their names promise

- `test_a_tool_called_with_a_deeply_nested_object`
  (`test_fuzz_eval_boundary.py`, depth 1..400) catches
  `(JavaScriptError, RuntimeError, ValueError)` and asserts **nothing**. With
  the default depth limit of 100, depth 50 must succeed and depth 300 must be
  refused -- the test accepts either outcome at every depth, so it passes
  identically if the depth limit is removed. Its docstring says "Past
  `max_serialization_depth` this must be refused cleanly". It does not check
  that. **This is the test that should have caught M3b and structurally cannot.**
- `test_a_tool_called_with_arbitrary_generated_js_values` generates *source
  text*, escapes it, and interpolates it into a single string literal:
  `sink('{escaped}')`. Every example passes exactly one JS **string**. No
  object, array, `Date`, `Set`, `BigInt`, typed array, `Proxy` or getter ever
  reaches `sink`. The name promises value-shape fuzzing; the body does
  source-text fuzzing one layer down. It also asserts nothing.
- `test_a_tool_called_with_many_arguments` asserts `got == count` for 0..400
  arguments, which is good, but every argument is a small integer. No strategy
  anywhere generates a *large* argument, so the aggregate byte budget (M3a) is
  outside the fuzzer's reach entirely.

**Fix:** parametrise the depth test on the configured limit and assert
`accepted == (depth <= limit)`; add a strategy that builds JS *values* (a
recursive object/array/Date/Set/BigInt/Uint8Array generator emitted as JS
source) and passes them to `sink`; add a size axis so `max_serialization_bytes`
is reachable from both directions.

## T3. Minor

- `tests/test_fuzz_eval_boundary.py`, `test_any_tool_exception_yields_a_catchable_js_error`:
  `getattr(__builtins__, exc_name, None)` is dead in a module context, where
  `__builtins__` is a dict rather than a module, so the `or {...}[exc_name]`
  fallback is always what runs. Harmless; ~8 lines to delete. It is a smell
  because a silently-dead branch in a test is indistinguishable from a working
  one.
- `TestPoolFuzz` asserts nothing, which is correct for a crash-only invariant,
  but the class docstring should say so.
- On the "`conftest` pattern where an unlisted module is skipped without
  noise": there is **no `conftest.py` anywhere in this repo** (`find . -name
  conftest.py` outside `target/` returns nothing). That failure mode is not
  present here. Collection is plain file discovery, and M2 is the real
  silent-skip mechanism -- worse, because it takes nine files with it.
- `tests/test_gil_and_limits.py:1` and `:87` refer to "v0.3" and "the v0.2.2
  bug" while `Cargo.toml` says `0.2.0`. Confusing in a repo about to make
  version numbers mean something.

---

# Code reduction

Ordered by lines removed per unit of risk.

## R1. Collapse the five `RuntimeJob` state machines. ~450-550 lines.

`src/runtime/runner.rs` is 3525 lines, and roughly 900 of them (`:1141` to
`:2050`) are five structs that are the same state machine:

| job | struct | impl |
| --- | --- | --- |
| `EvalAsyncJob` | `:1141` | `:1189` |
| `StreamReadJob` | `:1333` | `:1357` |
| `EvalModuleAsyncJob` | `:1448` | `:1498` |
| `CallFunctionAsyncJob` | `:1653` | `:1707` |
| `ResumeFunctionCallJob` | `:1896` | `:1930` |

Each carries the same fields (`timeout_ms`, `task_locals`, `responder`,
`start_time`, `deadline`, `state`, `watchdog`) -- a textbook Data Clump -- and
each `poll` opens with the *same* eight-line deadline check
(`ensure_reason` / `terminate_execution` / `Poll::Ready(Err(timeout))`,
differing only in the wording of two strings) followed by the *same* task-locals
installation block, verbatim:

```rust
if let Some(ref locals) = self.task_locals {
    core.task_locals = Some(locals.clone());
    core.module_loader.set_task_locals(locals.clone());
    core.js_runtime.op_state().borrow_mut()
        .put(crate::runtime::ops::GlobalTaskLocals(Some(locals.clone())));
}
```

(compare `runner.rs:1212-1221` with `runner.rs:1731-1740`.) Each then has an
`Init` → `Waiting { promise }` → `Done` progression where only the `Init` arm
differs -- it is the one step that produces the promise.

**Replace with** a single `PromiseJob` holding the shared fields plus
`start: Box<dyn FnOnce(&mut RuntimeCoreState) -> RuntimeResult<v8::Global<v8::Promise>>>`
and a `RuntimeCallKind`. The five `Init` bodies become five closures; the five
`*JobState` enums, five deadline preambles, five task-locals blocks, five
`start_time`/`deadline`/`kind` accessors and five constructors all become one.
The trait itself earns its keep and should stay. Estimate: 5 × ~140 lines of
scaffolding → ~180 lines, so roughly **500 lines out** of the file that most
needs them gone. This is also the right shape for the parked-promise kill
escalation, which currently has five places to land instead of one.

## R2. Delete the `SnapshotSource` wrapper. ~15 lines.

`src/runtime/runner.rs:192-206`: a single-variant enum

```rust
enum SnapshotSource { Owned(OwnedSnapshot) }
```

whose only method is a `match` with one arm delegating to `OwnedSnapshot`.
Pure Speculative Generality plus Middle Man. Use `OwnedSnapshot` directly.

## R3. Drop the duplicated depth accounting in `conversion.rs`. ~10 lines.

`python_to_js_value_internal` (`conversion.rs:185-197`) enforces depth **twice**:
an explicit `depth: usize` parameter checked against `limits.max_depth`, and
`tracker.enter()` which increments `LimitTracker::current_depth` and checks it
against the tracker's own `max_depth` (`js_value.rs:360-368`). Same limit, two
counters, threaded through six recursive call sites. Keep the tracker, delete
the parameter.

While in there: `tracker.exit()` at `conversion.rs:392` is skipped by every
early `return Err` in the function (`:233`, `:256`, `:279`, `:301`, `:345`,
`:351`, `:371`) and by every `?`. Benign today because an error aborts the whole
conversion, but it is a latent leak if the tracker ever becomes reusable -- and
dropping the duplicate counter makes the asymmetry go away for free.

## R4. `impl JSValue {}`. 1 line.

`src/runtime/js_value.rs:94`. An empty inherent impl block.

## R5. Prune the doc-vs-reality drift rather than maintaining it. ~150 lines.

`PATCH.md`, `PATCH_LARGE_SCRIPT_ABORT.md` and `PATCH_SNAPSHOT_ABORT.md`
(281 lines total, at the repo root) document three landed fixes that now have
regression tests naming them. They read as open work. Fold what is still true
into `docs/contributing/` and delete them. Same for the `CLAUDE.md` sections
S5/S6 identify as wrong: wrong documentation is negative lines.

## What earns its complexity

Not everything here is cuttable, and some of it is the good kind of careful:

- `HOST_ERROR_MARKER` and `restoreHostError` (`ops.rs:122-154`, `:422-451`).
  This looks baroque and is not. The comment explains *why*
  `JsErrorBox::new(class, ...)` cannot work (unregistered classes make
  `buildCustomError` return `undefined`, so guest JS catches a literal
  `undefined`) and says it was verified empirically against `deno_core` 0.409
  rather than read off the source. That is the standard.
- `setOwn` via `Object.defineProperty` (`ops.rs:329-336`) with the prototype-
  pollution rationale inline. Keep, and keep the comment.
- The `RuntimeJob::deadline` default method (`runner.rs:1136`) with its
  explanation of why the dispatcher needs the deadline to bound its park. That
  comment is why the parking fix is correct.
- `LimitTracker` as a shared aggregate budget. The right abstraction; it is just
  not installed on one of the two paths (M3).

---

# `JsFunction`: is it solid?

Mostly yes on the synchronous path, with one inherited gap and one ergonomic
wart.

**What I verified works [ran]:**

- Lifetime across `close()`: `f(21)` after `rt.close()` raises
  `RuntimeError: Function call failed: Runtime has been shut down`. No crash,
  no use-after-free, and `repr(f)` still works (`<JsFunction id=0>`).
- Lifetime across `terminate()`: raises
  `RuntimeTerminated: Function call failed: Terminated via TerminationHandle
  from another thread`.
- The GIL fix is real and is regression-tested with a measured pre/post ratio
  (`test_gil_and_limits.py:76-88`), which is a better test than most.
- Round-trip through `python_to_js_value` validates liveness before transfer
  (`function_id_for_transfer`, `conversion.rs:380-385`).
- `await f.call_async(5)` on an `async` JS function returns `15`.
- `tests/test_function.py` has 16 tests including
  `test_runtime_close_releases_outstanding_functions`.

**The gap [ran]:** a pending `call_async` is not resolved by termination.

```python
g = rt.eval("(() => new Promise(r => {}))")     # never resolves
task = asyncio.ensure_future(g.call_async())
h.terminate()
await asyncio.wait_for(task, 5)                  # -> TimeoutError; the future never settles
```

`TerminationHandle.terminate()` does not settle an in-flight `call_async`
future. This is the same parked-on-a-pending-promise condition the "guaranteed
kill" workstream exists to fix, so I am not proposing a mechanism here -- only
recording that **`JsFunction.call_async` is one of the surfaces that
workstream has to cover**, and that it is not obvious from the outside that it
is in scope. `ResumeFunctionCallJob` / `CallFunctionAsyncJob` are the two job
types involved (see R1: after that refactor there is one place to fix instead
of two).

**The wart:** S3, `call_async` returns a `Future` rather than a coroutine.

**Verdict:** solid enough to stabilise, provided the async variant is explicitly
in the kill-escalation's test matrix and S3 is either fixed or documented.

---

# Arrow / large data interchange

**It is already more first-class than the brief assumed.** On `main` there is a
full guide (`docs/guides/advanced/arrow-ipc-dataframes.md`), a runnable example
(`examples/arrow_ipc_dataframes.py`), and a README entry (`README.md:196`) with
the measurement and the honest crossover ("Below roughly 1,000 rows, don't...
the crossover measured here sits around 5,000 rows"). The zero-changes claim
holds: `bytes` → `Uint8Array` works
(`rt.eval("d.small.constructor.name")` → `'Uint8Array'` [ran]), and
`conversion.rs:217-229` maps `bytes`/`bytearray`/`memoryview` in and
`runner.rs:3316-3325` maps `Uint8Array`/`ArrayBuffer` back out.

What is missing is **a test**. `grep -rli arrow tests/` matches only
`tests/test_function.py`, and that is an unrelated substring. So the documented
capability -- with published numbers, in the README -- has no automated
verification that it still works. Given M1, "no test" and "no CI" compound.

**Recommendation:** not a new API. A `bytes` round-trip is the right interface
and wrapping it would be the speculative kind of generality this codebase is
otherwise good at avoiding. Do add (a) an integration test that pushes a
~100k-row Arrow IPC buffer in, reconstructs it in JS, and asserts a computed
aggregate comes back correct, and (b) a size-scaling assertion so a regression
in the `Uint8Array` path shows up as a failure rather than as a slow example.
The guide's instruction to load `apache-arrow` from a jsdelivr CDN URL deserves
a sentence about vendoring it, in a library whose users are running untrusted
code -- fetching your deserializer over the network is a different trust
decision than the rest of the guide.

## The 10 MB default

**Usability bug, and the error message is the worse half of it.** Three
separate observations:

1. `MAX_JS_BYTES = 10 * 1024 * 1024` (`js_value.rs:17`) is the default
   (`config.rs:258`). A 100k-row JSON payload exceeds it and is hard-rejected.
   It *is* configurable -- `RuntimeConfig(max_serialization_bytes=...)` -- and
   the Arrow guide says so at `:35-46`. So it is not a wall, it is a wall with
   an undocumented door.
2. The rejection does not mention the knob (S8). That is what turns a tunable
   limit into a perceived hard limit.
3. The limit is only enforced on **one** of the two directions (M3a). A default
   that rejects a legitimate 11 MB host payload while accepting a hostile 37 MB
   guest payload has the polarity backwards: it inconveniences the trusted
   side and does not constrain the untrusted one.

**Recommendation:** fix M3a first -- the asymmetry is the actual defect, and a
limit that only binds the trusted party is not a security limit. Then either
raise the default (64 MB is defensible for a library whose documented use case
includes 100k-row tables) or keep 10 MB and make the error name
`max_serialization_bytes` and point at the Arrow guide. Do not do the second
without the first, or you have documented a limit that isn't one.

---

# Fine as-is

Recorded so the punch list is not mistaken for the whole picture.

- **Guest globals are clean [ran].** `delete globalThis.Deno` works;
  `typeof globalThis.Deno` is `"undefined"` and the surviving globals are
  stock ECMAScript intrinsics plus pydeno's own `__pydeno*` / `__host_op*` hooks.
  No `require`, no `process`, no `fetch`, no timers.
- **Prototype pollution via `__proto__` is fixed and tested**, with the
  rationale in the source and a named regression test. I looked for siblings:
  `setOwn` is used at every object-construction site in the bridge JS
  (`prepare`'s object arm `:362`, `revive`'s default arm `:412`,
  `__pydeno_bind_object` `:488` and `:490`), so the fix is applied uniformly
  rather than at the one site the fuzzer happened to hit.
- **Tag forgery is contained [read].** Guest JS *can* put `__pydeno_type` on an
  object it hands to a host op, and `JSValue`'s `Deserialize` will honour
  `Undefined` / `Date` / `Set` / `BigInt` tags -- a type-confusion primitive
  (the host sees `pydeno.undefined` or an `int` where the guest returned an
  object). But the two tags that would matter, `JsStream` and `PyStream`, cannot
  be turned into host handles: `js_value_to_python` is called with
  `handle: None` on the op path, so both arms refuse
  (`conversion.rs:141-150`). Forging a stream id fails closed. Worth a comment
  at `ops.rs:182` saying the `None` is load-bearing for that reason -- right
  now it reads like an oversight, and a future change to pass the handle
  through (which M4 might tempt someone into) would open it.
- **Getter and `Proxy` re-entrancy during host-side serialization does not
  break anything [ran].** A getter that calls a host op mid-traversal returns
  `{'a': 1, 'b': 2}`; a getter that throws surfaces as a clean
  `JavaScriptError`; a `Proxy` with `ownKeys`/`get` traps serializes to
  `{'a': 1, 'b': 1}`. No `RefCell` double-borrow panic, no deadlock. Note for
  the docs, not a bug: guest JS *can* run arbitrary code (including other tool
  calls, spending their budget) from inside a getter while the host serializer
  is mid-object.
- **Cycle detection via `get_identity_hash` is sound in practice [read].**
  I initially read this as collision-prone, and it is not: `seen` holds only
  the current ancestor chain (`insert` before the recursive call, `remove`
  after -- `runner.rs:3368`, `:3406`), so at most `max_depth` (100) entries are
  live at once. Birthday risk over a 32-bit space at n=100 is negligible. A
  payload-wide `seen` set would have been a real bug at 100k objects; this
  isn't one.
- **The dispatcher parking fix (#1) looks right**, and
  `RuntimeJob::deadline` with its comment is the reason: the park is bounded by
  the in-flight job's own deadline, so a job that enforces its timeout inside
  `poll` still gets there. `tests/test_idle_cpu.py` measures the claim with
  `getrusage(RUSAGE_SELF)` rather than asserting a vibe.
- **`ToolBridge`'s name validation** (`_tools.py:28-41`, stricter than JS
  allows, with `__proto__`/`constructor`/`prototype` explicitly reserved) is
  the right shape: a small fail-closed allowlist with the reasoning written
  down.
- **`ToolBridge` refuses `PooledIsolate` loudly**, at attach time, with an
  error that explains *why* it is permanent rather than a todo
  (`_tools.py:265-283`). This is how an API wall should be presented.
- **Names.** `ToolBridge`, `TerminationHandle`, `JsFunction`, `SnapshotBuilder`,
  `RuntimeConfig` and its fields all say what they are; I found no Mysterious
  Name worth renaming in the public surface. `IsolatePool` is accurate too --
  S1 is about what the README claims for it, not what it is called.

---

# Summary

| # | Finding | Evidence |
| --- | --- | --- |
| M1 | No CI runs the tests | ran + read |
| M2 | Guest JS SIGBUSes a debug build; kills the suite at test 46 | ran |
| M3 | JS→Python enforces neither byte nor depth limit (37.7 MB vs a 10 MB cap) | ran + read |
| M4 | A JS function passed to a host tool silently becomes `{}` | ran |
| M5 | Op registry is ambient, unrevocable, and leaks op names in errors | ran + read |
| M6 | pydeno's own internal error strings reach guest JS | ran + read |
| S1 | `IsolatePool` is 21x slower than a warm `Runtime`; README recommends it | ran + read |
| S2 | `RuntimeError` and `RuntimeTerminated` both mean "runtime is gone" | ran |
| S3 | `call_async` returns a `Future`, not a coroutine | ran |
| S4 | `ToolNotFoundError` is never raised | read |
| S5 | Documented permission model does not exist | read |
| S6 | `CLAUDE.md` describes a stale layout and API | read |
| S7 | Two `unsafe` blocks lack SAFETY comments; detached `ArrayBuffer` yields zeros | read + suspect |
| S8 | Limit errors do not name the setting | ran |
| T1 | Memory leak has no regression test; budget bug tested on one side only | read |
| T2 | Three fuzz tests assert nothing their names promise | read |
| R1 | Five duplicated job state machines, ~500 lines | read |

**Verdict.** The security engineering here is better than average -- the
fixes that landed came with reasoning, not just patches, and the fuzz suite is
doing real work at the real boundary. What is not ready for a stability promise
is the enforcement layer around all of it: no CI runs the tests, one of the two
marshaling directions has no limits installed, and the API commits to a
`IsolatePool` recommendation the project's own measurements have already
retracted. M1 through M4 are the blocking set. M5, M6 and S1 are the ones that
get expensive to change *after* the promise, so they are worth spending
pre-1.0 time on even though they are not bugs.
