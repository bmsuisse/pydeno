# Preparing for an outside security review

This page is the briefing for a reviewer who did not write pydeno. It is a 0.10 deliverable on the
[roadmap](../roadmap.md), and it is deliberately limited to things that can be checked in this
repository. It does not claim the sandbox is safe; it says what we think we protect, where to look
first, and what we already know is open. A reviewer who finds a gap in this page has found something.

Read first: [`SECURITY.md`](https://github.com/bmsuisse/pydeno/blob/main/SECURITY.md) (policy),
[Sandbox security report](../security-report.md) (history and findings),
[Isolated runtime guide](../guides/advanced/isolation.md) (layers and their limits).

## 1. Threat model

**Assets.** The host process (memory, files, network position, credentials, environment); other
tenants' data when a host serves several; the host's capacity (CPU, memory, processes).

**Trust.**

| Party | Trusted? |
|---|---|
| Guest JavaScript (including model-written code) | **No. Hostile.** |
| Host application and the host functions / tools it binds | Yes. A host function runs with the host's full authority. |
| The worker process *before* sandbox application | Yes (it is pydeno's own Python and Rust). |
| The worker process *after* the guest runs | **No.** Assume the guest became native code in it (a V8 bug). Every frame it sends is untrusted input. |
| Snapshot and journal bytes | Untrusted unless authenticated (HMAC key, `associated_data=`). |
| V8, `deno_core`, other crates | Trusted, but unaudited by us; see "Known issues". |

**Attacker capabilities considered.** Arbitrary guest JavaScript, with a goal of: (a) reaching a
capability the host did not bind; (b) escaping the worker to the host; (c) crashing, hanging or
exhausting the parent; (d) leaking host information or another tenant's state; (e) forging or
replaying session state.

**Layers, outermost first** (each is tested on the assumption that the one above it failed):

1. Process boundary: guest runs in a separate worker process, supervised by the parent.
2. OS sandbox applied before V8 exists: Seatbelt (macOS); Landlock, empty root via user namespace and
   a seccomp filter, with privileges dropped (Linux).
3. `--jitless` V8 by default.
4. Limits enforced from outside the worker: hard deadline, memory (RSS poll), threads, CPU, buffer
   bytes, serialization budgets, in-flight host calls.
5. Startup self-test (attestation) that tries forbidden operations and refuses to start if one works.
6. The parent treats every worker frame as untrusted (native decoder with budgets).
7. Capability tokens for host functions (CSPRNG, usable only after bind; revocable).

**Explicit non-goals.** There is no permission model. Plain `Runtime` is not safe for hostile code.
The in-process layers do not stop an attacker who chains a V8 bug with a kernel bug; that gap is for
the deployment (container, gVisor, microVM; see `SECURITY.md`). Gates are defence in depth, not the
boundary.

## 2. Scope for a reviewer

**In scope** (these match what `SECURITY.md` calls a vulnerability):

| Area | Where to start |
|---|---|
| Sandbox escape from `IsolatedRuntime` / `AsyncIsolatedRuntime` | `python/pydeno/_sandbox.py`, `_worker.py`; `docs/guides/advanced/isolation.md` |
| Seccomp filter, Landlock, empty root, Seatbelt profile | `python/pydeno/_sandbox.py`; `tests/test_redteam_syscalls.py`, `tests/test_sandbox_syscall_tables.py`, `scripts/redteam_syscalls.py` |
| Worker/parent protocol and the native decoder | `python/pydeno/_wire.py`, `_isolated.py`, native codec `src/runtime/wire.rs`, `wire_json.rs`; `tests/test_wire_native.py` |
| Limit bypass (`max_memory`, `request_timeout`, `max_host_calls`, `max_buffer_bytes`, serialization) | `tests/test_isolated_limits.py`, `scripts/autoresearch/metric_security.py` |
| Capability tokens and `ToolBridge` | `src/runtime/ops.rs`, `python/pydeno/_tools.py` |
| Information leaks into the guest | `tests/test_isolated_capability_denial.py`, `tests/test_isolated_guest_surface.py` |
| Session state: journals, snapshots, `SessionPool`, signatures and associated data | `python/pydeno/_agent.py`, `_pool.py`, `_snapshot_auth.py`; `tests/test_redteam_boundary.py` |
| Gates (`gate=`, `SourcePolicy`, `static_gate`): fail-closed behaviour, check-to-use gap | `python/pydeno/_gate.py`, `src/scanner/`; `tests/test_gate*.py` |

**Out of scope** (matches `SECURITY.md`, "What is not"):

- Hostile code in plain `Runtime` crashing or hanging the host.
- What a host function you wrote does with its arguments.
- Path existence on macOS, and on Linux without user namespaces (documented, pinned by tests).
- Denial of service that needs a capability the worker does not have.
- Vulnerabilities in V8 or `deno_core` **as such**. Please report them upstream. In scope: pydeno
  failing to *contain* one, or pinning a version with a known fix available.
- Deployments that wrap the worker (`IsolatedRuntime(python="bwrap ...")`); `SECURITY.md` says not to.
- Free-threaded CPython: unsupported, so a finding that needs a 3.14t interpreter is not covered by
  the claims above (see [Free-threaded CPython](free-threaded.md)).
- Dev-only Python dependency groups (`uv.lock`): developer-machine risk, tracked in
  [Supply-chain security](supply-chain.md).

Platforms to target: Linux x86_64 and aarch64 (glibc, manylinux_2_28 wheels) and macOS arm64. Windows
runs only the in-process `Runtime`; there is no isolated worker there.

## 3. Building reproducibly

Honest status: **builds are pinned in the dependency graph but not bit-for-bit reproducible.** What is
and is not pinned:

| Input | State |
|---|---|
| Rust crates | `Cargo.lock` is committed. Release builds (`.github/workflows/workflow.yaml`) pass `--locked`. The CI test builds do not. |
| V8 | A prebuilt static library fetched by the `v8` crate's build script at build time (version visible in `Cargo.lock`; `v8` crate 150.4.0 at the time of writing). Not built from source by us. The download is verified by the crate's own checksum handling, which we have not audited. See [Research: engine hardening](research-engine-hardening.md) for the custom-build question (#44). |
| Rust toolchain | **Not pinned** (no `rust-toolchain.toml`). The reviewer should record `rustc -Vv`. |
| Python build deps | `maturin>=1.9,<2.0` (range). `uv.lock` pins the dev environment, not the wheel build. |
| Wheel container | `PyO3/maturin-action` with `manylinux: 2_28`; the action and container tags are not digest-pinned. |
| Timestamps / paths | Not normalised (`SOURCE_DATE_EPOCH` is not set). Expect differing wheel hashes. |

To build the same dependency graph the release uses:

```bash
git clone https://github.com/bmsuisse/pydeno && cd pydeno
git checkout <tag or commit under review>
rustc -Vv && uv --version             # record these in your report
uv tool install maturin               # or: pip install 'maturin>=1.9,<2'
maturin build --release --locked --out dist
# The Linux release wheels come from the manylinux_2_28 container:
#   maturin build --release --locked --out dist --find-interpreter   (inside ghcr.io/pyo3/maturin)
```

Then run the suite the way CI does (`.venv/bin/python -m pytest tests -q`, not `uv run`, which can
trigger a multi-GB rebuild; see `CLAUDE.md`). Linux sandbox tests need a container matrix:
`scripts/linux_matrix.sh WHEELS IMAGE [PROFILE]`.

Planned, not done: toolchain pin, `--locked` on every CI build, digest-pinned actions, and a
documented procedure that compares two independent builds. Until then, review the source at a commit
and compare behaviour, not wheel hashes.

## 4. Known issues

Derived from the [security report](../security-report.md) (status column of section 4 at the time of
writing) and the issues named below. The report is the source of truth; if this list and the report
disagree, the report wins and this page has a bug.

### Open in the sandbox

| Item | Source |
|---|---|
| Denied syscalls answer `EPERM`, so an exploit can probe the filter. Plan: kill the process on never-legitimate calls. | Report (OS confinement); issue #45 |
| Seccomp is a deny-list with a default-deny tail, not an allow-list derived from tracing real workers (about 44 syscalls identified). Plan in #45; needs native x86_64 and aarch64 verification (V8 on x86_64 calls `uname()`). | Report; issue #45 |
| Thread and memory caps are sampled by polling, not kernel-enforced (pids cgroup / `RLIMIT_NPROC` / cgroups v2 planned). | Report; issue #45 |
| No Landlock ABI canary: access rights newer than the reviewed ABI are not handled, and a kernel that silently weakens Landlock would not be noticed. | Report; issue #45 |
| Worker may outlive a dead parent (`PR_SET_PDEATHSIG` not set). | Report |
| macOS: `notify_post()` still reaches other processes; `kill(pid, 0)` distinguishes live pids; path existence is observable (also on Linux with Landlock only). | Report |
| Hosts mounting `/proc` with `hidepid`: usage unreadable; `require` refuses to start, `auto` warns. | Report |
| `sandbox="auto"` (the default) runs with fewer layers if some cannot apply; it warns. `require` is the hardened choice. | Report; `SECURITY.md` |
| Sandboxed tool process: host tools run in the parent's address space. | Issue #45 |

### Open at the host/worker boundary and guest surface

| Item | Source |
|---|---|
| Decoder amplification: a 6 MB frame can become about 200 MB of Python objects. | Report |
| Op tokens visible through `Function.prototype.toString`; bridge globals writable in older builds (round 3 notes "verified after the next build"); a guest that pre-defines a non-writable global makes a later `bind_function` silently inert. | Report |
| Bridge frames and `ext:` paths visible in stack traces (path filtering open). | Report |
| A huge source ignores `timeout=` while V8 parses it (plain `Runtime`; bounded by the frame cap in `IsolatedRuntime`). | Report |
| Sparse-array natives on length `2**32-1` ignore V8 termination; unbounded in plain `Runtime`, killed by the hard deadline in `IsolatedRuntime`. | Report |
| Journal rollback by someone who can write both the journal and its counter. | Report |
| A pool can exceed a tool budget if sessions are not routed to one pool (documented). | Report |

### Cold start (#72): what it changes for a reviewer

Issue #72 is a performance item, but each option touches security-relevant design: opt-in
fork-from-template shares address-space layout and stack canary between workers (no per-worker ASLR);
a native Rust worker would re-implement the wire protocol and sandbox application in a new
language. Neither has landed. A reviewer should treat the current Python worker as the reviewed
design and expect these changes to need their own review.

### WebAssembly (#37, shipped in 0.9 as `load_wasm()`)

Opt-in for trusted modules only. It needs `jitless=False`, which enables V8's JIT and WebAssembly for
that runtime's guest code too. Linear memory is outside `max_buffer_bytes`: in the isolated runtimes
it counts toward `max_memory` (a worker kill, not a catchable error); in plain `Runtime` it is
unbounded. Refused on the default jitless worker. Details and probes: security report, "Guest surface".

### Engine and dependency

| Item | Source |
|---|---|
| V8 and `deno_core` fixes arrive only on a `deno_core` upgrade; the scanners do not see Chromium V8 CVEs. `scripts/check_engine.py` runs weekly. | [Supply-chain](supply-chain.md) |
| `RUSTSEC-2026-0176` / `-0177` (`pyo3` 0.27.2) excepted in `deny.toml` as unreachable; removal needs `pyo3-async-runtimes` for `pyo3 >= 0.29`. | `deny.toml` |
| `paste` unmaintained (transitive via `v8`); `yoke-derive` 0.8.3 yanked. | Supply-chain page |

### Process gaps

- No independent review has happened yet. The four rounds in the report used the authors' own tooling
  and AI reviewers; the roadmap requires a review by someone else before 1.0.
- The disclosure process below has not been exercised end to end.
- Reproducible builds are not achieved (section 3).

## 5. Disclosure process

Consistent with [`SECURITY.md`](https://github.com/bmsuisse/pydeno/blob/main/SECURITY.md), which stays
the authoritative policy.

1. **Report privately** through GitHub Security Advisories
   (<https://github.com/bmsuisse/pydeno/security/advisories/new>). No public issue. Include a minimal
   reproduction: the JavaScript, the runtime settings, OS and kernel.
2. **Acknowledge** within a few working days. (SECURITY.md does not state a time; this is a target,
   not a promise, until the policy says so.)
3. **Reproduce and rate.** A maintainer reproduces on a clean build and states whether it is in scope
   (section 2). Each fix is written as a failing test first, as in earlier rounds.
4. **Fix on a private branch** (advisory temporary fork where possible), with a regression test and a
   probe in `scripts/autoresearch/metric_security.py` where the finding is a guest-surface class.
5. **Release.** Fixes are made against the latest release (older ones best effort, per
   `SECURITY.md`). Update the [security report](../security-report.md) with the finding and status
   and the changelog in neutral terms, as 0.9.0 did.
6. **Publish the advisory** with the fix version, credit for the reporter if they wish, and a CVE
   through GitHub when the severity warrants one.
7. **Agree a date with the reporter.** Proposed default for an outside review engagement: 90 days from
   report to public disclosure, shorter when a fix ships earlier, extendable by agreement. This is a
   proposal for the engagement terms, not published policy.

For an outside review, agree in advance: the commit under review, the channel (advisory or a private
repository), whether findings may be published in the report, and who may see them.

## 6. What we will hand a reviewer

- A tagged commit, `Cargo.lock`, `uv.lock`, and a built wheel plus the build log.
- The test suite and how to run it (`CLAUDE.md`, `scripts/linux_matrix.sh`).
- A native x86_64 and aarch64 Linux environment, and a macOS arm64 one, with `sandbox_status()` output
  from each.
- The security report and this page, including what is open.
