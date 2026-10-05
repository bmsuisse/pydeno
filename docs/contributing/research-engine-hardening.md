# Research: a smaller V8, and a Wasm-hosted engine tier

This page answers two research questions for 0.9:

- [#44](https://github.com/bmsuisse/pydeno/issues/44): would a custom V8 build without ICU (`Intl`) and
  without `Temporal` be worth maintaining?
- [#108](https://github.com/bmsuisse/pydeno/issues/108): should pydeno offer a JavaScript engine running
  inside WebAssembly, with instruction-count ("fuel") metering, as an optional hardened tier?

Both answers are **no for 0.9**, each with conditions for revisiting. The evidence follows. Every number
on this page was measured for this research unless it is marked as an estimate.

**Setup.** Apple M2 (8 cores), macOS. The `v8` crate is 150.4.0 and `deno_core` 0.412.0, as in
`Cargo.lock`, using the prebuilt static V8 library. The machine was shared with other builds while the
benchmarks ran (load average 33 to 36), so timings vary by up to almost 3x between runs. They are given as
ranges over the runs (two or three per engine), each the median of five. Use them as orders of magnitude, not as precise figures.

## Summary

| Question | Recommendation |
|---|---|
| Custom V8 without ICU / Temporal (#44) | **Don't build one.** The cheap part already exists as a runtime flag: `v8_flags=["--no-js-shipping"]` removes `Temporal`, `Float16Array`, explicit resource management and the other newest features inside the engine. Only ICU needs a source build, which also needs a patched `v8` crate. It would put five wheel targets on hour-long source builds and slow down every V8 security update. Keep the issue's gate: do it only if an outside security review names V8's built-in surface as the problem. |
| Make `--no-js-shipping` the `IsolatedRuntime` default | A good candidate for a later minor release, decided separately. It removes real engine code paths, and every vendored library passes with it. It changes documented defaults, though, so it is **not** part of this change. |
| Wasm-hosted engine tier (#108) | **Out of scope for 0.9.** The containment and the deterministic CPU limit are real and were measured. The cost is a second engine, a second bridge and a second set of patches to track. It ran our workloads 1x to 10x slower than jitless V8 and 10x to 60x slower than V8 with JIT, and lacks `Intl`. Revisit it as a separate opt-in package if a user needs deterministic CPU accounting or a smaller trusted base more than speed and library reach. |

## #44: a custom V8 build without ICU and Temporal

### What the guest sees today

Counted with `Object.getOwnPropertyNames(globalThis)`:

| Runtime | Globals | Removed compared with `Runtime` |
|---|---:|---|
| `Runtime` | 78 | (none; 71 are engine globals plus pydeno's bridge entry points) |
| `IsolatedRuntime` | 72 | `SharedArrayBuffer`, `Atomics`, `WeakRef`, `FinalizationRegistry`, `WebAssembly`, the Wasm loader |
| `IsolatedRuntime(v8_flags=["--no-js-shipping"])` | 67 | the above, plus `Temporal`, `Float16Array`, `DisposableStack`, `AsyncDisposableStack`, `SuppressedError` |

`Intl` is present in all three, and so is `Proxy`, iterator helpers and the rest of the older language.

### What the runtime flags can and cannot do

The `v8` crate builds V8 with `v8_enable_temporal_support = true` and `v8_enable_i18n_support = true`. The
ICU data is compiled into the static library (`icu_use_data_file = false`). V8 itself gates `Temporal`,
`Float16Array` and explicit resource management behind runtime flags, so in principle a flag can switch
them off. A small scratch program against the same `deno_core` and V8 tested four ways of doing that:

| How the flags were passed | Result |
|---|---|
| `--no-harmony-temporal --no-js-float16array --no-js-explicit-resource-management ...` **before** platform start-up (what `v8_flags=` does) | **Undone.** `deno_core`'s platform start-up (`runtime/setup.rs`, `v8_init`) sets these flags back to on afterwards, and V8 keeps the last value. Flags that `deno_core` does not touch (`--no-js-base-64`, `--no-js-upsert`, `--no-js-sum-precise`) do take effect. This is why `IsolatedRuntime` refuses the overridden flags with a `ValueError` (`src/runtime/v8_flags.rs`). |
| The same flags **after** platform start-up | **Process abort**: `Check failed: !IsFrozen()`. V8 freezes its flags (`--freeze-flags-after-init`, on by default) once it is initialised. |
| `--no-freeze-flags-after-init` first, the flags after start-up | Works (66 globals, `using` is a `SyntaxError`). It does so by turning off flag freezing, a hardening measure that write-protects V8's flag memory after start-up. **Not recommended.** |
| `--no-js-shipping` before start-up | Works (66 globals), with no other trade. This is the documented `IsolatedRuntime` opt-in. |

So the per-feature flags are not reachable without weakening V8, and the umbrella flag already reaches the
same result. `tests/test_isolated_guest_surface.py` pins exactly what `--no-js-shipping` removes and keeps.
`tests/test_isolated_libraries.py` runs every vendored library with it.

`Intl` has no runtime switch. Deleting the global is cosmetic, as the issue says: `toLocaleString`,
`localeCompare`, `\p{...}` in regular expressions and `normalize` still reach ICU directly.

### What a source build allows, and what it costs

- **Configurable.** `V8_FROM_SOURCE=1` builds V8 with GN, and `GN_ARGS` / `EXTRA_GN_ARGS` pass arguments
  through, so `v8_enable_i18n_support=false` and `v8_enable_temporal_support=false` can be requested.
  `Temporal` does not depend on ICU. Without ICU, V8 compiles a copy of the time-zone data in.
- **Not configurable without a fork.** The crate's own C++ glue (`src/binding.cc`) includes ICU headers
  and calls `icu::Locale` unconditionally (`icu_get_default_locale`, `icu_set_default_locale`). A build
  without ICU therefore needs a patched `v8` crate, carried forward on every V8 bump.
- **Build cost (estimate, not measured).** A source build downloads V8's pinned toolchain (GN, Ninja,
  Clang) and compiles everything in the prebuilt archive, which for one target is 147 MB and 1,847 objects. Cold builds take on the order of an hour per target on hosted CI runners, which is
  why the crate publishes prebuilt archives at all. Our release workflow builds five wheel targets today
  (manylinux x86_64 and aarch64, macOS arm64, Windows, plus the sdist), each in 8 to 12 minutes because the
  V8 library is downloaded. A source build would multiply that, need a build cache per target and per V8
  version, and add a cross-compilation risk for each target. No source build was attempted here: the
  machine had 21 GiB of free disk.
- **Security cost.** pydeno receives V8 security fixes only through `deno_core` upgrades (see the
  `engine-watch` workflow). A fork adds a rebuild-and-repatch step to every one of those upgrades. Slower
  V8 patching is a larger risk than the ICU code we would remove.

### What the build would save

The prebuilt library's embedded ICU data (`icudtl_dat.o`) is **10.3 MiB raw and 4.4 MiB gzipped**. The
0.8.0 wheels are 17 to 19 MiB, so the data alone is about a quarter of a wheel. ICU's code and V8's
`Intl` built-ins are on top of that and were not measured. The `Temporal` objects are small (about
0.5 MiB of text). Smaller wheels are a real benefit, but they are a packaging argument, not a security
one.

### What breaks without `Intl`

The `normalize` and `\p{...}` rows come from V8's code for builds without ICU (`builtins-string.cc`,
`regexp-parser.cc`). The other rows were observed in a small engine that has no `Intl`, and match V8's
fallbacks (which ignore the locale and options):

| Code | With ICU (today) | Without ICU |
|---|---|---|
| `(1234567.891).toLocaleString('de-CH')` | `1’234’567.891` | `1234567.891` (locale ignored, no error) |
| `(1234.5).toLocaleString('en-US', {style: 'currency', currency: 'USD'})` | `$1,234.50` | `1234.5` (options ignored, no error) |
| `new Date(0).toLocaleDateString('de-DE')` | `1.1.1970` | a fixed, non-localised format |
| `['z','a','Ä','b'].sort((a, b) => a.localeCompare(b, 'de'))` | `a Ä b z` | `a b z Ä` (code-point order) |
| `'Å'.normalize('NFC').length` | `1` | `2`: V8 without ICU checks the form and **returns the string unchanged** |
| `/\p{L}+/u` | works | `SyntaxError` (V8 refuses property escapes without ICU) |
| `new Intl.DateTimeFormat(...)`, `Intl.NumberFormat`, `Intl.Segmenter` | work | `ReferenceError` |

The silent cases are the worst ones. `toLocaleString`, `localeCompare` and `normalize` return plausible
wrong answers instead of failing.

**Vendored libraries** (`vendor/`) barely depend on ICU. None calls `Intl.*`, `localeCompare`,
`String#normalize` or `\p{...}`. d3 and Vega call `toLocaleString("en")` only for numbers ≥ 1e21. ECharts
calls `toLocaleLowerCase` once. three.js tests for `Float16Array` with `typeof` before using it. The
`normalize` hits in three.js, turf and ECharts are vector maths, not string methods.

**Our own code and tests do depend on it.** The clock freeze in `_worker.py` wraps
`Intl.DateTimeFormat#format`. Determinism and runtime tests assert `Intl.DateTimeFormat` behaviour
(`tests/test_isolated_determinism.py`, `tests/test_isolated_runtime.py`). The spreadsheet-deck example
formats money with `toLocaleString("en-US", {...})`. Model-written code reaches for
`toLocaleString`/`Intl.NumberFormat` by default for currency, thousands separators and dates.

### Recommendation for #44

1. **Do not build or maintain a custom V8 now.** The removable surface that matters most (the youngest
   engine code: `Temporal`, `Float16Array`, explicit resource management, `Promise.try`,
   `RegExp.escape`, ...) can already be removed without a build, with `--no-js-shipping`. Removing ICU
   would need a crate fork and source builds on five targets, would slow down every V8 security update,
   and would silently change results that model-written code relies on. Keep the issue's gate: revisit
   only if an outside review names V8's built-in surface, and ICU in particular, as the problem.
2. **Consider `--no-js-shipping` as the `IsolatedRuntime` default in a later minor release.** It removes
   engine code paths, not just globals, and the vendored libraries pass with it. It is a behaviour change
   for guests that use `using`, `Temporal` or `Uint8Array.fromBase64`, and the isolation guide and
   `test_no_js_shipping_removes_exactly_what_the_guide_says` currently document the opposite default. So
   it needs its own release note and an opt-out, and is not made here.
3. **Small follow-up.** The `ValueError` for `--no-harmony-temporal` and its siblings could point to
   `--no-js-shipping` as the setting that works.

## #108: a Wasm-hosted engine with fuel metering

### What was measured

The spike used a small bytecode-interpreter JavaScript engine (no JIT, no `Intl`) compiled to WASI. It ran
under a Rust WebAssembly runtime, through that runtime's Python binding, with fuel metering on and off.
The same fixed workloads ran on the engine's native build, on `Runtime` (V8 with JIT, in process) and on
`IsolatedRuntime` (jitless V8 in the sandboxed worker, `prewarm=False`, so each run includes a cold
worker):

- `cpu`: recursive `fib(27)`, a sieve up to 10^6, and building a 100k-character string
- `json-sort`: 100k objects through `JSON.stringify`/`JSON.parse`, then a two-key sort and a `Map` count
- `d3-force`: the vendored d3 bundle, a force layout with 300 nodes and 300 ticks
- `echarts-ssr`: the vendored ECharts, 20 server-side SVG renders

All engines produced the same checksums. The table gives the workload time, measured inside the engine,
in milliseconds:

| Workload | V8 JIT (`Runtime`) | V8 jitless (`IsolatedRuntime`) | Wasm engine | Wasm engine + fuel | Same engine, native |
|---|---:|---:|---:|---:|---:|
| `cpu` | 19–23 | 124–142 | 595–1,177 | 705–1,086 | 217–493 |
| `json-sort` | 131–219 | 241–487 | 2,248–2,626 | 2,640–3,902 | 1,440–1,812 |
| `d3-force` | 725–1,028 | 8,556–9,364 | 11,360–15,455 | 14,352–26,773 | 7,022–18,787 |
| `echarts-ssr` | 191–263 | 167–428 | 408–516 | 460–566 | 184–262 |

Start-up, from creating the engine to finishing an empty script:

| | Cold start |
|---|---:|
| Wasm engine, instantiated from a module compiled once per process | 1.1–1.4 ms (compiling the module once: 0.36–0.65 s, cacheable) |
| `Runtime` | 13–14 ms |
| `IsolatedRuntime`, cold worker | 155–172 ms (a pool hides this in normal use) |

Limits:

- **Deterministic CPU limit: confirmed.** The `cpu` workload used 4,093,634,402 fuel units on every run.
  An infinite loop with a budget of 2×10^9 trapped after 254–287 ms of wall time. The count does not
  depend on machine load, which no wall-clock or CPU-time limit can promise. Fuel overhead was hard to
  isolate on the loaded machine: between none and 2.4x across runs and workloads. The Wasm runtime's
  cheaper alternative (epoch interruption) is not deterministic.
- **Memory limit: hard.** With a 64 MiB cap on linear memory, an allocation loop ended in the engine's own
  catchable `InternalError: out of memory` after about 250 ms. Every guest allocation lives in that
  linear memory. In V8, `max_heap_size` does not bound `ArrayBuffer` storage, so pydeno needs
  `max_buffer_bytes` and the worker-killing `max_memory` on top.

### Containment

A memory-safety bug in the guest engine stays inside the Wasm module's linear memory. Escaping needs a
second bug, in the Wasm runtime's compiler or in the host functions it exposes. That is a smaller and
better-audited trusted base than V8. The trusted base is not empty, though: the Wasm runtime compiles
the module to native code, and its compiler has had code-generation vulnerabilities of its own.
`IsolatedRuntime` contains a V8 bug at the process level instead (seccomp and Landlock, or Seatbelt, a
fresh process, jitless V8, hard limits). The two layers are complementary. A Wasm engine *inside* the
sandboxed worker would be the strongest combination.

### Costs

- **Speed.** Compared with the default `IsolatedRuntime` (jitless V8), the Wasm engine was roughly 4x to
  10x slower on plain JavaScript (`cpu`, `json-sort`) and 1x to 3x slower on the two library workloads.
  It was 10x to 60x slower than V8 with JIT on the plain-JavaScript loads.
- **Ecosystem reach.** The engine has no `Intl`, no `Temporal`, no `WebAssembly`, and lags the newest
  language features (see the ICU table above for what code without `Intl` does). d3 and ECharts' SVG
  server-side rendering ran unmodified with a timer stub. Large libraries load and run slowly, though,
  and rendering stacks that expect V8 performance (3D scenes, large charts) become impractical.
  pydeno's selling point over small interpreters is exactly that reach.
- **A second bridge.** pydeno's value is the bridge: type conversion (`convert.rs`), capability-token
  ops, streams, modules, snapshots, the inspector. All of it is written against V8's API. A Wasm tier
  would need a second implementation over the guest engine's C API across the Wasm boundary, and a
  second set of tests to keep the two in step.
- **Packaging.** The engine module is 1.5 MiB (0.5 MiB gzipped). The Wasm runtime is the large part: its
  Python binding's shared library alone is 24 MiB uncompressed. Embedding the runtime as a Rust crate
  would add a compiler back end to every wheel. Its exact size was not measured.
- **Maintenance.** A second engine and a Wasm runtime to keep patched, next to V8.

### Recommendation for #108

**Keep it out of scope for 0.9, and do not add it to the main package.** The two properties that make it
attractive were confirmed: containment by linear memory, and a CPU limit that is deterministic and
independent of load. Neither is something `IsolatedRuntime` is missing for its target use (running
model-written JavaScript that uses real libraries): it already has hard wall-clock, CPU-time and
memory limits, and a process sandbox. The costs land squarely on that use: speed, ecosystem reach,
`Intl`, and a second bridge.

Revisit it as a **separate, opt-in package** with a deliberately smaller API (evaluate code and exchange
JSON, no streams or inspector), if a user needs one of these:

- CPU accounting that is reproducible across machines (billing, fairness between tenants, replayable
  runs);
- a trusted base small enough to audit, for small logic snippets where library reach does not matter;
- containment on a platform without an OS sandbox (Windows, where `IsolatedRuntime` is not available).

If that happens, run the Wasm engine inside the existing sandboxed worker, so that a Wasm runtime bug
still meets the OS sandbox. In the meantime, [Alternatives](../alternatives.md) points users who need
those properties to a Wasm-hosted engine directly.
