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

- Seccomp as an allow-list instead of today's deny-list. (Kill-on-violation for the never-legitimate
  calls is done; the traced syscall set it was checked against is in `tests/data/worker_syscalls_*.json`.)
- Kernel-enforced thread and memory caps (cgroups where available), in addition to the polling checks.
- Tool boundary: per-tool deadlines, result caps, redacted conversion errors, argument normalisation, a
  sandboxed tool process, safe preset tools (read-only files / SQL / HTTP fetch), call audit hooks.
- macOS: close the path-existence side channel.
- Landlock ABI probe plus a canary so a kernel that silently weakens it is noticed.

**API**:

- Decide what is stable and what is experimental. Proposed stable: `Runtime`, `IsolatedRuntime`,
  `AsyncIsolatedRuntime`, `AgentSandbox`, `RuntimeConfig`, the exception types and error kinds.
  Proposed experimental: `SessionPool`, the pydantic-ai integration, snapshots.
- Pin the journal and snapshot formats with an explicit compatibility promise.

## 0.9: external review and soak

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
