# Roadmap to 1.0

1.0 means two things: **the public API will not break without a major version**, and **the security
claims have been checked on every platform we say we support, by someone other than us**. It does not
mean "no vulnerabilities". The [security report](security-report.md) lists what is known to be open, and
that list is the honest definition of the gap.

Status below is a plan, not a promise of dates.

## Where we are (0.6)

- `Runtime` (in process) and `IsolatedRuntime` (supervised worker, OS sandbox: Seatbelt on macOS,
  Landlock + empty root + seccomp on Linux, jitless V8).
- `AgentSandbox`: state across runs, pause at tool calls, signed replay journal.
- Native Linux x86_64 / aarch64 and macOS arm64 wheels. Tests on macOS and a 30-cell Linux container matrix.

## 0.7: async, diagnostics, results (in progress)

| Item | Issue / PR |
|---|---|
| `AsyncIsolatedRuntime`: asyncio-native, no thread per runtime, one supervisor per loop | PR #21 |
| `AsyncAgentSandbox`, `SessionPool` with a pluggable store, TTL, cap, LRU, rollback counter | #10, #15 |
| `sandbox_status()`, `classify_error()`, `check_source()`, upgrade guide | #12, #13, #17, #18 (PR #20) |
| Bounded `ExecutionResult` with ordered console capture | #11 |
| `dump()` after a crash returns the last good journal | #14 |
| Tools from JSON Schema and a lazy tool catalog | #16 |
| `memfd_create` denied, orphan guard | PR #19 |

## 0.8: platforms and hardening, API freeze candidate

**Platforms** (workflow `platforms.yml`, native GitHub runners):

- Green on Ubuntu 22.04 / 24.04 (x86_64 and arm64), macOS 14 / 15 (arm64) and macOS Intel, Python 3.10 to 3.14.
- Fix the macOS worker abort seen on some runners (Seatbelt-related, intermittent).
- Wheels for every platform we claim: macOS Intel and Linux musl are missing today.
- Windows: decide and document. Today only the in-process `Runtime` can work there (the isolated worker is
  POSIX only), and the POSIX-only test modules are not collected on Windows.
- Free-threaded CPython (3.14t): `Runtime` is `unsendable`, so it panics when used across threads. Either support
  it or state plainly that it is unsupported.

**Hardening** (each needs verification on native x86_64, not only emulation):

- Seccomp as an allow-list with kill-on-violation: implemented in 0.10 (#45); the aarch64 tables are
  checked in a kernel-free interpreter, native aarch64 results come from CI.
- Kernel-enforced thread and memory caps: `RLIMIT_DATA` and a per-namespace `RLIMIT_NPROC` in 0.10
  (#45); cgroups only where a delegated subtree exists (not assumed).
- Tool boundary: per-tool deadlines, result caps, redacted conversion errors, argument normalisation, a
  sandboxed tool process, safe preset tools (read-only files / SQL / HTTP fetch), call audit hooks.
- macOS: close the path-existence side channel.
- Landlock ABI probe plus a canary so a kernel that silently weakens it is noticed: done in 0.10 (#45).

**API**:

- Decide what is stable and what is experimental. Proposed stable: `Runtime`, `IsolatedRuntime`,
  `AsyncIsolatedRuntime`, `AgentSandbox`, `RuntimeConfig`, the exception types and error kinds.
  Proposed experimental: `SessionPool`, the pydantic-ai integration, snapshots.
- Pin the journal and snapshot formats with an explicit compatibility promise.

## 0.9: gates, worker caps, worker loop, WebAssembly, CI coverage

| Item | Issue / PR |
|---|---|
| Gates: a host-side check of the exact source before it runs (`gate=`, `SourcePolicy`, `static_gate`) | #99 |
| Opt-in worker caps for pools and the front door (`max_workers`, `checkout_timeout`) | #81, #101 |
| Persistent worker loop | #60 |
| Stream edge case | #58 |
| `load_wasm` | #37 |
| Follow-up pull requests: performance, hardening and test changes that came out of review | #85, #90, #91, #92, #93 |
| CI additions: every cargo feature compiles on its own, a default-install smoke test (wheel in a clean venv, no extras), examples run only for release publishes | this release |

## 0.10: cold start, tool process, review preparation

| Item | Issue / PR |
|---|---|
| Seccomp as an allow-list with kill-on-violation, Landlock canary, kernel caps (a sandboxed process for host tools is design-only: `docs/contributing/sandboxed-tool-process.md`) | #45 |
| Custom V8 build: research ([findings: don't for 0.10](contributing/research-custom-v8-build.md)) | #44 |
| A deep review run over the whole tree | |
| Preparation for an outside security review: threat model, scope, reproducible builds, known-issue list | [written](contributing/security-review-prep.md); builds are not yet bit-reproducible, no outside review has happened |
| Free-threaded CPython (3.14t): support it, or state plainly that it is unsupported | [decided: unsupported for now](contributing/free-threaded.md) (loads and passes a smoke test, GIL re-enabled at import, not audited) |

## 0.11: cold start

| Item | Issue / PR |
|---|---|
| Cold start in the same class as Monty. Step one, an opt-in fork-from-template worker start (about 63 ms to about 21 ms on x86_64), is in PR #124 and waits for independent review (shared ASLR layout and stack canary across forked workers, `empty_root` under fork, aarch64 and macOS) | #72 |

## Before 1.0: external review and soak

- An independent review or fuzzing campaign of the sandbox (not run by the authors, and not by the same models
  that wrote the code).
- A disclosure process that has been used at least once end to end.
- At least one downstream service running a release for several weeks, surviving an upgrade.
- Benchmarks tracked in CI so a performance regression is visible.
- No open issue that is a correctness or security bug in a stable API.

## 1.0

- Everything in the stable API frozen under semantic versioning.
- The security report lists no open item that is not an accepted, documented limitation.
- Support policy published: supported Python versions, how long old minors get fixes, and how
  `deno_core` / V8 updates are tracked and released.

## Not planned

- A permission model. There is none by design: a runtime grants nothing by default, and what a guest can reach
  is exactly what the host bound. See the
  [architecture notes](contributing/architecture.md#there-is-no-permission-model-and-none-is-missing).
- Making the in-process `Runtime` safe for hostile code. Use `IsolatedRuntime` for that.
