# Free-threaded CPython (3.14t)

**Status: not supported.** pydeno does not declare free-threaded compatibility, publishes no
free-threaded wheels, and runs no free-threaded job in CI. On a free-threaded interpreter the
extension still loads, but CPython turns the GIL back on for the whole process when it does.
This page records what was checked for the 0.10 roadmap item and why we stop there.

## What was checked (2026-10, on 0.10 development)

Environment: CPython 3.14.7 free-threaded (`uv python install 3.14t`), Linux x86_64, extension built
with `maturin develop` from this tree (PyO3 0.27.2, unoptimised build).

| Check | Result |
|---|---|
| Extension builds for `cp314t` | Yes, no source change. |
| `import pydeno` | Works, with `RuntimeWarning: The global interpreter lock (GIL) has been enabled to load module 'pydeno._pydeno', which has not declared that it can run safely without the GIL`. `sys._is_gil_enabled()` is `True` afterwards. |
| One `Runtime` per thread, 16 threads, 200 evals each with a bound Python function | Correct results, with the GIL re-enabled and also under `PYTHON_GIL=0`. |
| Using one `Runtime` from a second thread | `PanicException: ... Runtime is unsendable, but sent to another thread`. Same as on a GIL build; it is a thread rule, not a GIL rule. |
| `tests/test_runtime.py`, `test_function.py`, `test_modules.py`, `test_gil_and_limits.py` (244 tests) | All pass, with and without `PYTHON_GIL=0`. |
| The rest of the suite (including the isolated runtimes and sandbox tests) | **Not run to completion** on 3.14t. An attempt was abandoned after it ran for a long time on an unoptimised build; no result is claimed. |

That is a smoke test. It is not evidence that the extension is safe without the GIL.

## Why we do not claim support

- **No declaration.** The `#[pymodule]` in `src/lib.rs` does not set `gil_used = false`. Setting it is
  a promise that every `#[pyclass]` and every piece of shared state in the crate is sound with
  parallel Python threads. Nobody has audited that: the `PyOnceLock` statics in `src/runtime/wire.rs`,
  the stream and finalizer paths that wait for or hand over the GIL (`src/runtime/handle.rs`,
  `src/runtime/stream.rs` are written around a GIL that serialises Python code), the Python-side
  module state in `python/pydeno/` (pools, default-runtime registry, gate thread pool), and PyO3
  borrow flags on the `#[pyclass]` types that are not `unsendable` or `frozen` (for example
  `RuntimeConfig`, `InspectorConfig`, the stats classes, `PyStreamSource`).
- **`Runtime` is `unsendable`** (`src/runtime/python/runtime.rs`, as are `JsFunction`, `JsStream` and
  `SnapshotBuilder`). Free threading does not change that: one runtime belongs to the thread that made
  it. The documented patterns (one runtime per thread or task, `TerminationHandle` for watchdogs,
  `IsolatedRuntime` for sharing) are the same on both builds.
- **No test budget.** A 16-thread loop that passes is not a race detector. Supporting it means a
  `3.14t` CI cell, a threaded stress suite, and ideally ThreadSanitizer on the Python parts, none of
  which exist.
- **The sandbox claims are not re-checked.** The isolated runtimes' threading, fork handling and
  worker supervision were reviewed on GIL builds. We make no security claim for a free-threaded host.

## What would be needed to support it

1. Audit shared state in Rust (`static`, `OnceCell`, `Arc<Mutex<..>>`) and Python modules for
   assumptions that GIL-held code is serial.
2. Add `#[pymodule(gil_used = false)]` only after step 1, with a regression test that imports the module
   with warnings as errors on a `3.14t` interpreter.
3. Add a `cp314t` wheel and a `3.14t` CI job running the whole suite, plus a threaded stress test.
4. Re-run the isolated runtime and Linux matrix on `3.14t`, since the worker is a Python process too.
5. Decide the classifier and docs wording only then.

Until those exist, treat `3.14t` as "loads, re-enables the GIL, behaves like 3.14 in the checks above",
which is an observation, not a supported configuration. Setting `PYTHON_GIL=0` is at your own risk.
