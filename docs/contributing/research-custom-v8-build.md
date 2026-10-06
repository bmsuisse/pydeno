# Research: a custom V8 build (pointer compression, sandbox, jitless, disabled features)

This page answers the roadmap item "Custom V8 build: research"
([#44](https://github.com/bmsuisse/pydeno/issues/44)) for 0.10. It extends
[Research: engine hardening](research-engine-hardening.md), which already answered one slice of #44 in 0.9:
a build **without ICU and `Temporal`**. This page does not repeat that. It covers the build options that page
left open: **pointer compression**, the **V8 sandbox**, a **build-time jitless** V8 and **build-time removal
of WebAssembly**, and it re-checks the cost side against the pins in `Cargo.lock`.

**Method and limits.** Nothing here was built or benchmarked: no V8 source build was attempted, and no
timing or memory number on this page was measured. What is stated as fact comes from one of four sources,
named where it is used: this repository at `future/0.10`; the `v8` crate (`rusty_v8`) at tag `v150.4.0`
(`Cargo.toml`, `build.rs`); the asset list of the `v150.4.0` GitHub release; and `deno_core` 0.412.0 /
`deno_v8` 0.4.0 manifests on docs.rs. Everything else is marked **unknown** or **from V8's own
documentation, not re-verified here**.

## Summary

| Question | Answer |
|---|---|
| What are the options? | `rusty_v8` already exposes cargo features for pointer compression (`v8_enable_pointer_compression`) and the V8 sandbox (`v8_enable_sandbox`, which implies pointer compression). `deno_core` 0.412.0 forwards the first one. JIT and WebAssembly can be compiled out only through GN arguments on a source build. |
| Is any of it available without a source build? | **Pointer compression: partly.** The `v150.4.0` release has `ptrcomp` archives for x86_64 Linux, x86_64 macOS and aarch64 macOS, none for aarch64 Linux or Windows, and **none that also include `simdutf`**, which `deno_core` always requests. **Sandbox: no.** No `sandbox` archive exists in that release. **Jitless / no-Wasm: no.** |
| What would it buy? | Mainly a smaller exploit surface for a V8 memory-corruption bug (sandbox) and a smaller native binary (jitless, no Wasm). The sandbox part rests on V8's design, not on anything measured in pydeno. `IsolatedRuntime` already contains such a bug at process level and already runs `--jitless`. |
| What would it cost? | Source builds of V8 for every wheel target, a build cache and a reproducibility story pydeno does not have today, a second build flavour for the JIT users, and a slower path for every V8 security update. |
| Recommendation | **Don't, for 0.10.** Revisit the sandbox variant **later**, under the conditions at the end. |

## What a V8 build is made of today

- `Cargo.toml` pins `deno_core = "0.412.0"`. `Cargo.lock` resolves `deno_core` 0.412.0 to `deno_v8` 0.4.0,
  which is a facade over two optional backends: `v8` **150.4.0** (the real `rusty_v8`) and `v8x`
  `149.4.0-rc.4` (`Cargo.lock`, `deno_v8` entry; `deno_v8` 0.4.0 manifest).
- `deno_core` depends on `deno_v8` with `default-features = false, features = ["simdutf"]` (`deno_core`
  0.412.0 manifest), so **every pydeno build uses the `simdutf` flavour** of the prebuilt library.
- `build.rs` of `v8` 150.4.0 downloads
  `librusty_v8{_ptrcomp}{_sandbox}{_simdutf}_{debug|release}_{target}.a.gz` from the `rusty_v8` GitHub
  release for the crate's own version. `V8_FROM_SOURCE=1` builds from source instead. `GN_ARGS` and
  `EXTRA_GN_ARGS` pass GN arguments, `RUSTY_V8_ARCHIVE` and `RUSTY_V8_MIRROR` redirect the download.
- pydeno does **not** vendor V8 or the Rust crates. `vendor/` holds only JavaScript libraries
  (d3, ECharts, three.js, Vega, ...). There is no `[patch]` section for `v8` or `deno_core` in `Cargo.toml`;
  the two `profile.*.package."*"` sections there only turn off overflow checks for a `deno_core` bug.
  So the "pin" is `Cargo.lock` plus the prebuilt archive it implies, and nothing in the tree builds V8.
- V8 security fixes arrive only by bumping `deno_core`; `.github/workflows/engine-watch.yml` and
  `scripts/check_engine.py` check this weekly ([supply chain](supply-chain.md#the-v8-and-deno_core-upgrade-path)).
- The worker already runs `--jitless` by default (`python/pydeno/_worker.py`), and with `--jitless` the
  sandbox also refuses executable mappings (`allow_exec="--jitless" not in flags`, `_worker.py`).

## Option 1: pointer compression

**What it is.** V8 stores heap references as 32-bit offsets from a base. That is a build-time choice
(`v8_enable_pointer_compression`), and it cannot be switched at run time.

**What is available.** In the `v150.4.0` release:

| Archive flavour | x86_64 Linux | aarch64 Linux | x86_64 macOS | aarch64 macOS | Windows |
|---|---|---|---|---|---|
| `release` | yes | yes | yes | yes | yes |
| `simdutf_release` (what pydeno uses) | yes | yes | yes | yes | yes |
| `ptrcomp_release` | yes | **no** | yes | yes | **no** |
| `ptrcomp_simdutf_release` | **no** | **no** | **no** | **no** | **no** |

(Release asset names, `v150.4.0`. The compressed size of the x86_64 Linux archive is 39.3 MB with pointer
compression against 38.8 MB without, from the same listing.) `deno_core` 0.412.0 forwards the feature as
`v8_enable_pointer_compression`, so turning it on is one line in `Cargo.toml`. But because `deno_core`
also requests `simdutf`, the build script would ask for `librusty_v8_ptrcomp_simdutf_release_*`, which does
not exist. **Unknown:** whether the missing combination is deliberate (incompatible) or just not built; it
needs to be asked of `rusty_v8`.

**What it would buy for pydeno.**

- *Memory.* V8's blog reports a large heap reduction (about 40 percent on its benchmarks). **From V8's own
  documentation, not re-verified here, and not measured for pydeno's workloads.** pydeno's memory limits are
  mostly outside the V8 heap: `max_heap_size` does not bound `ArrayBuffer` storage, which is why
  `max_buffer_bytes` and `max_memory` exist (`python/pydeno/_isolated.py`, security report). So the benefit
  to `max_memory` accounting is likely smaller than the headline. **Unknown** until measured.
- *Security.* On its own, nothing. Pointer compression is the precondition of the sandbox below.

**What it would cost.**

- A per-isolate heap cap of about 4 GiB (**from V8's own documentation, not re-verified here**). The
  default `max_memory` is 1 GiB (`DEFAULT_MAX_MEMORY`, `_isolated.py`); users may set it higher.
- Gaps in the prebuilt matrix (table above): no aarch64 Linux or Windows archive, so those targets
  need a source build or no pointer compression, and one cannot ship a mixed policy per wheel without
  two code paths in tests.
- **Snapshots.** V8 aborts on a snapshot built by another build; the snapshot signature binds the pydeno
  release for that reason (`docs/security-report.md`, "Snapshot signature did not bind the engine build").
  A build flavour would have to enter that binding too. **Unknown:** whether a snapshot built by a
  non-compressed build is rejected cleanly or aborts.

## Option 2: the V8 sandbox

**What it is.** The V8 sandbox (`v8_enable_sandbox`) confines the memory a V8 bug can reach to a reserved
virtual-address region, so a heap-corruption bug alone should not reach the rest of the process. In
`build.rs` it sets `v8_enable_sandbox=true`, `v8_enable_external_code_space=true` and
`v8_enable_pointer_compression=true`; the crate feature implies pointer compression. What it does and does
not stop is defined by V8's documentation and is **not re-verified here**.

**Availability.** No `sandbox` archive exists in the `v150.4.0` release (asset list above). Using it means
`V8_FROM_SOURCE=1` for **every** target.

**What it would buy.** An extra layer *inside* the process, below the OS sandbox. `IsolatedRuntime` already
assumes the V8 layer fails: the guest runs in a supervised worker with Seatbelt or Landlock plus seccomp,
and `tests/test_redteam_syscalls.py` fires dangerous syscalls from the sandboxed process
(`CLAUDE.md`, isolation tests). The V8 sandbox would make the first step of an exploit harder, which
narrows the window between a V8 bug and its fix. How much is **unknown**, and no pydeno test could show it
without a working exploit. It would apply to plain `Runtime` as well, which has no OS sandbox. This is the
only option here with a real security argument, and it is a defence-in-depth argument.

**What it would cost.** All of pointer compression's costs, plus: no prebuilt at all; **unknown** interaction
with how the bridge hands buffers to V8 (the bridge charges `ArrayBuffer`, resizable and transferred buffers
itself, `docs/security-report.md`), which would need a test pass; and any limits of the V8
sandbox itself would have to be stated in pydeno's threat model (**unknown** which). **Unknown:** the effect
on cold start and on the benchmarks in `BENCHMARKS.md`.

## Option 3: build-time jitless and no WebAssembly

**What it is.** GN arguments `v8_jitless=true`, `v8_enable_turbofan=false`, `v8_enable_maglev=false`,
`v8_enable_sparkplug=false` and `v8_enable_webassembly=false`. `rusty_v8`'s `build.rs` already sets exactly
these for iOS devices, so the combination is known to be buildable there. **Not tested for desktop
targets; unknown.**

**What it would buy.** The JIT compilers and the Wasm engine would no longer be in the binary at all,
instead of being switched off by `--jitless` at start-up. That removes dead code an attacker cannot
re-enable by a flag, and probably shrinks the library (**unmeasured**). It also removes a dependency of the
sandbox on a command-line flag: today `allow_exec` depends on `"--jitless" in flags`.

**What it would cost.** pydeno ships features that need the JIT and Wasm:

- `IsolatedRuntime(jitless=False)` is documented as the way to recover 1.5x to 8.2x speed
  (`docs/security-report.md`, section 6).
- `load_wasm()` requires `jitless=False` (security report, `load_wasm` row).
- In-process `Runtime` is the fast path, with JIT.

A jitless-only build would take those away, so the choice would be **two builds** (two native libraries per
wheel, or two wheels) with a runtime selector, which doubles the test matrix of the 30-cell Linux
container matrix in `CLAUDE.md`. For the security-sensitive default, the flag already gives the same
behaviour at no build cost, and the worker's sandbox rules already refuse executable mappings under it.
**Net: low added value for high cost.**

## Option 4: disabling language features

Covered in [Research: engine hardening](research-engine-hardening.md): `--no-js-shipping` already removes
`Temporal`, `Float16Array` and explicit resource management without a build, and `deno_core` re-enables the
individual flags (`src/runtime/v8_flags.rs`, `SET_BY_DENO_CORE`). Only ICU needs a source build. Nothing in
this page changes that conclusion. The `v8_flags.rs` list has to be re-checked on every `deno_core` bump
regardless.

## Cost and maintenance against the pins

| Item | Today | With a custom V8 |
|---|---|---|
| Build input | prebuilt `simdutf_release` archive per target, downloaded by `build.rs` | `V8_FROM_SOURCE=1`: V8 checkout, GN, Ninja and a Clang toolchain per target (what `build.rs` drives) |
| Wheel build time | not recorded in this repo; [Research: engine hardening](research-engine-hardening.md) records 8 to 12 minutes per target (measured for that page) | **Unknown.** That page estimates about an hour per target on hosted runners; **no source build was run for either page** |
| CI | `workflow.yaml` builds manylinux_2_28 x86_64 and aarch64 plus macOS and Windows with `maturin-action` | a build cache keyed by V8 version, target and GN arguments, plus a cross-build path for aarch64 Linux. **Unknown** whether hosted runners have the disk and time for V8 |
| Security updates | bump `deno_core` (engine-watch tells us), rebuild, run the suite | the same, plus a V8 rebuild with every bump |
| Crate fork | none | **Not needed** for pointer compression, sandbox, jitless or no-Wasm: `build.rs` already reads the cargo features and `GN_ARGS`. It was needed only for the no-ICU option (research-engine-hardening) |
| Lock-step with `deno_core` | `deno_core` chooses the `v8` version | unchanged, but a flag combination the facade or the prebuilt matrix does not cover (as `ptrcomp` + `simdutf` today) turns an upgrade into a debugging task |
| Support matrix | 30-cell Linux matrix plus macOS | a flavour multiplies it, unless one flavour replaces the default |

### Reproducibility

- **Today**: `Cargo.lock` pins crate versions and checksums (the `v8` crate checksum is in the lock), but
  the prebuilt V8 archive that `build.rs` downloads is **not** pinned in the lock. `build.rs` writes a `.sum`
  file next to it and compares that with the URL to skip re-downloads (`build.rs`). **Unknown:** whether the
  archive's content hash is verified against anything beyond that URL. So the wheel's V8 is reproducible
  to the extent that the release asset never changes.
- **A source build** is only as reproducible as V8's toolchain pinning. V8's GN build downloads its own
  Clang and sysroot at fetch time (`build.rs` sets `use_sysroot=true`). Bit-for-bit reproducible output
  is **unknown** and no one has been measured here. The roadmap's "preparation for an outside security
  review: threat model, scope, reproducible builds" item would want this answered either way; a
  custom build makes the question harder, not easier.
- A middle path exists: `RUSTY_V8_ARCHIVE` / `RUSTY_V8_MIRROR` let CI point at a self-built archive and
  keep the crate build as it is. That moves the cost to producing and storing the archive, and adds a
  new supply-chain item (see [supply-chain](supply-chain.md)).

## Recommendation

**Don't build or ship a custom V8 for 0.10.**

1. **Pointer compression alone: don't.** Its security value is nil without the sandbox, the memory benefit
   is unmeasured and probably small for pydeno's limits, it needs a source build today (`ptrcomp` plus
   `simdutf` has no prebuilt, and aarch64 Linux and Windows have none at all), and it adds a heap cap.
2. **V8 sandbox: later, not now.** It is the only option with a security argument, but there is no prebuilt,
   no pydeno test can show its value, and the OS sandbox already covers the same threat for
   `IsolatedRuntime`. Revisit when **all** hold:
   - `rusty_v8` publishes a `sandbox` archive for the targets pydeno ships (so no source build is needed),
     **or** an outside review of the sandbox names the V8 layer as the weak point;
   - a spike measures cold start, `max_memory` accounting and the bridge's buffer handling on it (the
     unknowns above);
   - the `ptrcomp` + `simdutf` combination exists or `deno_core` offers a path without `simdutf`.
3. **Build-time jitless / no Wasm: don't.** The flag already does it at no build cost, and the build would
   remove `jitless=False`, `load_wasm()` and the JIT fast path unless pydeno ships two builds.
4. **Language-feature removal and ICU: unchanged**, as in
   [Research: engine hardening](research-engine-hardening.md).

**What to do instead, cheaply, in the meantime:**

- Keep `engine-watch` and the upgrade path as the V8 security mechanism; they are the real control.
- Add the facts above to the outside-review preparation item: which V8 archive flavour each wheel uses
  (`simdutf_release`), and that its hash is not pinned in `Cargo.lock`. Whether to pin it is a separate,
  small question for the reproducible-builds item.
- If someone wants data, the cheapest spike is one source build on a Linux x86_64 machine with
  `v8_enable_sandbox` (the prebuilt route cannot work: no archive combines `ptrcomp` with `simdutf`), with a
  recorded build time, disk use, wheel size, cold start and a full test-suite run. That answers most of the
  **unknowns** above for one target, and should be run before this recommendation is changed.
