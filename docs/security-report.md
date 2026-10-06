# Sandbox security report

How the pydeno sandbox was tested, what independent review found, what was fixed, and what is
still open. It is written to be read sceptically: it lists the misses as well as the catches.

!!! note "What this report does not claim"
    It does not claim the sandbox is free of vulnerabilities. No sandbox can claim that, and a V8
    engine bug is *contained* by the design, not prevented by it. The goal is narrower and
    checkable: **a breach of one layer must not be a breach of the host**, and every layer is
    tested on the assumption that the one above it has already failed.

## 1. The stance: assume breach

`IsolatedRuntime` runs untrusted JavaScript in a separate worker process behind an OS sandbox
(Seatbelt on macOS; Landlock, an empty root and seccomp on Linux), with limits enforced from
outside. Every test and review below asks one of two questions:

1. **Can the guest do something it should not?** (JavaScript surface, resource abuse, bridge abuse.)
2. **If the guest becomes native code in the worker (a V8 exploit), what can it reach?**
   (OS confinement, the parent/worker boundary, information leaks.)

A deny-list is only as good as its last review, so the design leans on two further ideas:

- **Attestation.** Before any guest code exists, the worker *tries* the forbidden things (read a
  file, write a file, spawn, connect out, signal its parent, read the parent's environment) and
  refuses to start if one works. A gap in a list becomes a failed start instead of a finding.
- **Fail closed.** `sandbox="require"` refuses to run unless every layer applied and the
  self-test passed; limits that cannot be measured warn, or refuse to start under `require`.

## 2. What is tested, and how

| Layer | Tests | What they check |
|---|---|---|
| Behaviour and lifecycle | `test_isolated_runtime`, `_lifecycle`, `_limits`, `_determinism` | Normal use, limits, cancellation, restarts, determinism. |
| Capability denial | `test_isolated_capability_denial` | A checklist of what untrusted code tries first: files, network, subprocess, environment, `Deno`/Node/browser globals, the runtime's own plumbing; through `eval` and `eval_async`; plus a check that nothing reached the host. |
| Assume-breach (Linux) | `test_redteam_syscalls`, `test_sandbox_violation`, `scripts/redteam_syscalls.py` | Real dangerous syscalls fired from a sandboxed process (and from a real worker): each must answer `EPERM`, or, for the never-legitimate ones, kill the process, which the parent reports as `sandbox_violation`. The sweep covers ~350 syscalls. |
| What a worker calls (Linux) | `scripts/trace_worker_syscalls.py` | Real workers traced with `strace` through the isolation suites, natively on x86_64 and aarch64: the evidence for the allow-list. |
| Canaries (Linux) | `test_sandbox_canaries` | The filter's kill action and Landlock's enforcement are checked by a forbidden call after they are applied; a kernel that accepts either and does not enforce it is detected. |
| Filter logic | `test_seccomp_program` | The BPF program run in a small interpreter over every syscall number on x86_64 and aarch64, with no kernel. |
| Kernel tables | `test_sandbox_syscall_tables` | Every number in the filter checked against the kernel's own tables (`tests/data/syscalls.json`). |
| Startup self-test | `test_sandbox_attest*`, `test_seatbelt_process_info` | The probes detect a breach when there is no sandbox (negative control) and report none when there is. |
| Bridge tampering | `test_bridge_poisoning` | Guests that replace `Date.prototype.valueOf`, `Array.prototype.map`, and other built-ins cannot make the host abort. |
| Wear-down attacks | `test_isolated_attack_classes` | Leaks across create/close/crash/kill/cancel (processes, descriptors, threads), unbounded registries, memory growth, concurrent evals, `close()` racing `eval()`. |
| Untrusted worker | `TestUntrustedWorker` and the fake-worker harness | A worker that sends hostile frames, reuses tokens, floods calls, or allocates while the host is busy. |
| Wire codec | `test_wire_native`, differential tests against a Python reference | The native decoder and the reference agree; budgets bound nodes, depth and hash collisions. |
| Fuzzing | `test_isolated_fuzz`, `test_fuzz_eval_boundary` | Hostile sources and values across the boundary. |
| Real libraries | `test_isolated_libraries` and the examples | three.js with the glTF exporter, Vega-Lite, dagre, pptxgenjs, d3, ECharts, turf still work inside the sandbox. |
| Public examples | `test_example_*` | Each example is checked against an independent computation, and Monty's output against CPython's. |
| Platforms | Linux matrix | 30 cells: 12 images on x86_64 and aarch64, and simulated kernels without Landlock or seccomp; macOS in debug and release; a Linux-wheel job. |
| Supply chain | `security.yml` | `cargo-deny`, `cargo-audit`, `pip-audit`, OSV; a weekly engine watch for newer V8. |

Platform-specific tests are *deselected, never skipped*, and CI enforces a zero-skip budget, so a
test that cannot run is visible rather than quietly green.

## 3. Independent review

Three rounds, each with a different method:

- **Round 1 (during 0.5.0).** Self-review plus CI. CI found real bugs that local runs did not: a
  second-pass block of denials sitting in the wrong table, missing x86_64 legacy syscalls, two
  memory-ceiling tests that raced the deadline.
- **Round 3 (the new code).** The same three reviewers read what round 2 added: the self-test, agent
  sessions, a prototype public-challenge server and the bridge fix, and looked for ways around the
  fixes.
- **Round 2 (after 0.5.0).** Three AI reviewers were given separate slices and told to *reproduce
  before reporting*: one for the OS layer, one for the host/worker boundary and the engine, one for
  the guest surface and the tests. A second pass looked for "lockdown opportunities". Their reports
  were verified by experiment, and each fix was written as a failing test first. GitHub Copilot
  reviewed the pull request on top of that.
- **Prior-art research.** Sandbox escapes in vm2, SandboxJS, isolated-vm and others; Monty's design,
  public challenge and Hacker News thread; Node and Deno permission bypasses; recent V8 advisories.
  Most historical JavaScript sandbox escapes are bugs in a *same-realm* proxy layer, which this
  architecture does not have: the sandbox is a process boundary, not a proxy.

### An incident worth recording

During round 2, one reviewer's assume-breach probe called `setpriority(PRIO_USER, 0, 20)` to check
that the sandbox refuses it. A typo in a *candidate* profile meant the sandbox failed to apply
silently, so the call ran unconfined and lowered the priority of hundreds of the developer's own
processes. Nothing was lost and it is reversible, but it shows exactly the failure the self-test is
for: **a probe must verify its confinement before it runs**. Probe scripts now refuse to run if no
sandbox applied, and the product's own attestation fails closed in the same way.

## 4. Findings and status

Severity is a judgement about impact on the host when the finding is combined with a guest that
already runs native code, or by the guest alone for the JavaScript-level items.

### OS confinement

| Finding | Status |
|---|---|
| macOS: a worker could read its parent's argv **and environment** (`KERN_PROCARGS2`), undoing `env={}` | **Fixed.** `process-info*` denied and `sysctl-read` removed; self-test probes it. |
| macOS: hardware UUID (`IOPlatformUUID`, `gethostuuid`), other-process queries, host statistics, `F_GETPATH` | **Fixed** in the profile. |
| Linux: datagram `socketpair` + `sendto` could reach local sockets (journald, notify) | **Fixed.** Stream-only `socketpair`. |
| Linux: `prctl` unfiltered (core-scheduling cookies on other processes, dumpable, parent-death signal) | **Fixed.** Allow-list of what a worker uses. |
| Linux: executable mappings allowed although a jitless V8 never needs them | **Fixed.** `mmap`/`mprotect` with `PROT_EXEC` refused when jitless. |
| Linux: host fingerprinting (`sysinfo`, `getpriority`, `ioprio_get`, `get_robust_list`, `getpgid`, `getsid`, the socket ioctl block) | **Fixed.** `uname` deliberately stays open: see "Lessons". |
| Linux: pipe-buffer memory invisible to the RSS poll (`F_SETPIPE_SZ`) | **Fixed.** |
| Linux: root worker that cannot become `nobody` kept its capabilities | **Fixed.** Capabilities cleared; `require` still refuses. |
| Landlock/userns "single-thread" check could not see native threads | **Fixed.** Counts via `/proc/self/task`. |
| Worker with a thread bomb stays under the memory ceiling | **Fixed.** Parent kills a worker over 64 threads. |
| `sandbox="auto"` runs with fewer layers silently | **Mitigated.** Warns; `require` refuses. The default stays `auto` (a deliberate compatibility choice). |
| Denied calls answer `EPERM` (an exploit can probe the filter freely) | **Fixed** (0.10, #45). About 60 never-legitimate calls (`ptrace`, `execve`, the mount API, `bpf`, `io_uring_*`, `memfd_create`, ...) kill the worker; the parent reports `sandbox_violation`. Others still answer `EPERM`, on purpose: libraries probe some of them and fall back. |
| Seccomp is a deny-list with a default-deny tail for unreviewed syscalls | **Fixed** (0.10, #45). An allow-list (92 syscalls on x86_64, 79 on aarch64, plus about 20 with checked arguments), derived by tracing real workers natively on both architectures; `fcntl` and `ioctl` commands are allow-lists too. |
| A kernel (or a stack in front of it) that accepts the seccomp kill action or the Landlock ruleset and does not enforce it would go unnoticed | **Fixed** (0.10, #45). Landlock: a canary on every worker start (a directory readable before the ruleset must be refused after it), or the layer is not counted. Seccomp: a worker asks the kernel whether the kill action is supported; `sandbox_status()` exercises it (a never-legitimate call in its throwaway child must be killed). Not on every start, because the kernel audits each seccomp kill; a filter in front of pydeno's cannot weaken a kill (the most severe action wins). Failures fail the self-test, so `require` refuses. |
| macOS: `notify_post()` still reaches other processes; `kill(pid, 0)` still distinguishes live pids | **Open, known.** The connection to `notifyd` is made before the profile applies. |
| Landlock access rights newer than the reviewed ABI are not handled | **Open.** |
| Worker outliving a dead parent (`PR_SET_PDEATHSIG`) | **Open.** |

### Host / worker boundary

| Finding | Status |
|---|---|
| A lying worker could reuse a capability token so revoking one binding left another callable | **Fixed.** Duplicate or mismatched tokens end the session; revoke drops the host handler first. |
| No supervision while a synchronous host handler ran | **Fixed.** The watchdog thread checks memory and threads then. |
| In-flight call cap was per command; dangling calls accumulated | **Fixed.** Runtime-wide. |
| `fork()` in a pre-fork server: the child killed, or reused, the parent's workers | **Fixed.** Children forget inherited runtimes; `close()` is fork-aware; the spare lock is re-created. |
| Cross-thread re-entry from a host function deadlocked with no deadline | **Fixed.** Bounded wait. |
| `RuntimeConfig(snapshot=...)` silently dropped (with its bootstrap) | **Fixed.** Refused. |
| `eval_async` shared the application's default thread pool | **Fixed.** One thread per runtime. |
| Worker-chosen arguments reached the resolver, loader and console handlers | **Fixed.** Validated. |
| Host exception text reached the guest by default | **Fixed.** Redaction is on by default (a behaviour change). |
| Snapshot signature did not bind the engine build; V8 aborts on a snapshot from another build | **Fixed.** The signed header carries the pydeno release. |
| Limits that cannot be measured were silently inactive | **Fixed.** Warn, or refuse under `require`. |
| Escape sequences and native stack frames in crash messages | **Fixed.** |
| A guest could kill the worker's only reply-reading thread by calling an async host function without awaiting it | **Fixed.** Found while building the agent-sessions layer. |
| Decoder amplification: a 6 MB frame can become ~200 MB of Python objects | **Open.** Lower node budget planned. |
| Non-finite limit values (NaN, infinity) were accepted and silently disabled the limit | **Fixed** (0.8). Validated at construction; probe `non_finite_limits_are_refused`. |
| Console calls paused the hard deadline like tool calls, so a console flood stretched a run up to `max_host_wait` | **Fixed** (0.8). Console time pauses the deadline only within one deadline per command (since 0.9 also while one slow synchronous handler runs: the worker is killed on time and the command raises once the handler returns); probe `console_flood_does_not_stretch_the_hard_deadline`, `tests/test_sync_handler_deadline.py`. |
| `Pydeno`'s default printer wrote guest console output to the host's stdout without limit (~150 MB in 2 s) | **Fixed** (0.8). 1 MiB per feed, then `[truncated]`; probe `default_printer_volume_is_capped_per_feed`. |
| A `SandboxPool` / `Pydeno` that runs out of ready workers starts new ones without limit (cold starts) | **Mitigated, opt-in** (0.9). The default is unchanged (cold starts without limit). `max_workers=N` caps a pool's live worker processes, checked-out ones included; at the cap a checkout waits up to `checkout_timeout` and raises `CheckoutTimeout`. The cap is per pool, not per host. |

### Guest surface

| Finding | Status |
|---|---|
| Replacing `Date.prototype.valueOf`, `Array.prototype.map`, etc. made the bridge hand a Symbol to the Rust converter, which **aborts**: a lost worker, or, for plain `Runtime`, a dead host process | **Fixed.** Intrinsics captured before guest code; strict-mode bridge; indexed loops. |
| Host-call arguments had no size cap before being copied | **Fixed.** 1M values / depth 128. |
| `Temporal.Now` ignored the frozen clock (a nanosecond timer) | **Fixed.** |
| A `BigInt` result past Python's 4300-digit limit ended the session | **Fixed.** |
| Catastrophic regex ran until the deadline killed the worker | **Fixed** by default flag (linear-time fallback). |
| Typed-array bombs killed the worker via the RSS poll | **Fixed.** Default `max_buffer_bytes` makes them a catchable `RangeError`. |
| Op tokens visible through `Function.prototype.toString`; bridge globals writable | **Open.** |
| A guest that pre-defines a non-writable global makes a later host `bind_function` silently inert | **Open.** |
| Bridge frames and `ext:` paths visible in stack traces | **Partly fixed** (strict mode); path filtering open. |
| A huge source ignores `timeout=` while V8 parses it (plain `Runtime`; bounded by the frame cap in `IsolatedRuntime`) | **Open.** |
| Prototype pollution persists across evals in one runtime | **By design.** One runtime per trust unit. |
| A resizable `ArrayBuffer` (or growable `SharedArrayBuffer`, or the copy `transfer()` makes of one) was not counted by `max_buffer_bytes`: V8 takes those backing stores from its page allocator, not the embedder's, so 2 GiB could be committed under a 255 MiB cap and filling it was a `max_memory` kill instead of the promised `RangeError` | **Fixed.** The bridge charges their committed bytes to the same budget (constructor, `resize`/`grow`, `transfer*`), with weak handles and a GC-and-sweep when the cap is hit. `WebAssembly.Memory.grow` remains a sink the cap cannot see (not present under `--jitless`). |
| A host `SnapshotBuilder` bootstrap that keeps a reference to the native `ArrayBuffer` constructor or `ArrayBuffer.prototype.resize` (or `SharedArrayBuffer.prototype.grow`, `transfer`) and exposes it to guest code lets the guest create or grow resizable buffers past the charge: snapshot code runs before the bridge wraps those built-ins | **By design (host code is trusted).** Do not hand a snapshot-captured native buffer constructor or method to the guest; `IsolatedRuntime` refuses snapshots. |
| A refused buffer allocation left its refusal flagged after V8's final retry, so a later genuine heap overflow was taken for a refusal and V8 aborted the process (`FatalProcessOutOfMemory`, a few percent of runs) | **Fixed.** The attempt V8 makes right after the near-heap-limit callback consumed a refusal no longer flags again. |
| `enable_console=True`: one `console.log` of a megabyte killed the worker (SIGABRT). Its stdout was the stderr capture file under `RLIMIT_FSIZE`, and deno_core's `op_print` unwraps the flush of the failed write | **Fixed.** The worker never lets the engine echo console output; `on_console` is unaffected. |
| `execute()` returned console output with raw terminal escape sequences, while error text was already cleaned | **Fixed.** Same rule for both. |
| Found by the autoresearch red team (`scripts/autoresearch/metric_security.py`, 28 probes, all passing): the probe battery pins each of the above and the deadline-bypass and refused-bind classes from the fourth review round | **Pinned.** |
| Typed arrays (other than `Uint8Array`) and boxed strings, also behind a Proxy, as results, stream chunks or host-call arguments, were expanded into one key per element before the size budget was charged: hundreds of MB, several times past `timeout=`, and (a Proxy around one, as an argument to plain `Runtime`) a V8 out-of-memory abort of the process | **Fixed** (0.8, issue #75 slice A). Checked up front, looking through Proxies natively, plus a fixed cap of 1,048,576 elements whatever `max_serialization_bytes` is. |
| A Proxy's traps ran while a result, stream chunk or host-call argument was converted: an `ownKeys` trap could grow a resizable buffer after its size was checked, a Proxy hid an Array from the metered path, and `ownKeys() { return [] }` made the engine list the whole target for free at every reference | **Fixed** (review of #80). A Proxy is unwrapped natively to its innermost target, which is converted; no trap runs. Revoked Proxies and chains past 64 are refused. |
| `v8_flags` that deno_core's own start-up overrides (eight flags, among them `--no-harmony-temporal`, `--no-js-float16array` and `--no-js-explicit-resource-management`) were reported as applied while doing nothing | **Fixed.** Refused with a `ValueError` before a worker starts. The features themselves can be switched off only together, with the blunt opt-in `v8_flags=["--no-js-shipping"]` (see the isolation guide). |
| Numbers outside int64 were clamped (`2**63` came back as `2**63 - 1`) | **Fixed.** They stay floats. |
| Sparse-array natives (`sort`, `reverse`, `join`, `indexOf`, `lastIndexOf`, `copyWithin`, `toSorted`, `flat` on length `2**32-1`) ignore V8 termination | **Open, known.** Unbounded in plain `Runtime`; `IsolatedRuntime`'s hard deadline kills the worker (probe `slice_a_native_builtin_outlives_the_hard_deadline`). |
| `load_wasm()` (#37) runs a host-supplied WebAssembly module: it needs `jitless=False` (V8's JIT and WebAssembly, for that runtime's guest code too), and the module's linear memory is outside `max_buffer_bytes` | **Known limit, opt-in for trusted modules.** Refused on the default jitless worker (in the parent; that worker's global surface is unchanged). Size-capped (8 MiB), parsed with bounded reads, no imports, arguments checked against the signatures, each call under the runtime's timeout; intrinsics captured before guest code and the instance held only by the host. Linear memory counts toward `max_memory` in the isolated runtimes (a worker kill, not a catchable error) and is unbounded in plain `Runtime`. The worker takes its reference to the loader before guest code, so where V8 has no WebAssembly (`--jitless`, or `--lite-mode`, which implies it without the word) a loader the guest planted is never called (found in review). Probes `wasm_load_refused_on_jitless_worker`, `wasm_guest_planted_loader_gets_the_bytes`, `wasm_guest_poisoned_api_reaches_host`, `wasm_loader_global_replaceable`, `wasm_hostile_bytes_end_the_session`. |

### Round 3: review of the new code

The code added in round 2 (self-test, agent sessions, the bridge fix, and a prototype server for a
public "hack pydeno" challenge) was reviewed again by three reviewers, with the same "reproduce
before reporting" rule. The challenge server was then **dropped from the release**: it is not shipped and
not supported, so its findings are summarised in one line below.

| Finding | Status |
|---|---|
| A prototype challenge server had a slowloris, unlogged hang-ups, an evadable leak alarm, a rate-limit bypass and file-permission gaps | **Found and fixed in the prototype, then the prototype was removed from the release.** Not shipped. |
| macOS: a confined worker could create SysV semaphores, shared memory and message queues that outlive it (a small system-wide table) | **Fixed.** Denied by syscall; self-test probes it. |
| Linux: `READ_IMPLIES_EXEC` inherited from a parent personality defeats the no-executable-mapping rule | **Fixed.** Cleared before seccomp. |
| `attest()` counted any `OSError` (even ENOENT) as a refusal | **Partly fixed.** A path-independent `execve` probe (Linux) was added; path probes remain and are weaker where a distroless image has no `/bin/sh` or `/etc/hosts`. |
| Sessions: a journal could be rolled back (restoring a spent tool budget) or loaded as another tenant's under a shared key | **Fixed / documented.** Associated data in the signature; rollback needs a counter the caller keeps. |
| Sessions: a forked child holding a session stalled 20 s at exit; `\w` matched characters JavaScript rejects; a tool could close its own session; odd-but-authentic journals raised the wrong errors | **Fixed.** |
| Journal and snapshot signatures did not bind the platform or the redaction setting | **Fixed.** |
| Polyfill timers: the fire budget never reset (a reused runtime's timers silently died), O(n) per fire and clear, unbounded pending timers, `Infinity` delays, `structuredClone` through the `__proto__` setter | **Fixed.** |
| Bridge: `null` entries skipped the node count, so a four-billion-entry sparse array looped until the deadline | **Fixed in source; verified after the next build.** |
| Bridge globals (`__host_op_sync__` and friends) were writable | **Fixed in source; verified after the next build.** |
| macOS: path existence is observable (`stat` answers EPERM for a path that exists and ENOENT for one that does not); XNU build string and CPU/memory counts are readable | **Open, known.** Seatbelt cannot hide existence; Linux with only Landlock has the same oracle. |
| Linux: the thread cap is sampled, not kernel-enforced | **Partly fixed** (0.10, #45). `RLIMIT_NPROC` at 128 inside the worker's own user namespace on Linux 5.14+ (the kernel counts per namespace there). Without the empty root it would count every process of the host user, so it is not set and the sampled cap remains the limit. Cgroups need a delegated subtree and are not used. |
| Hosts that mount `/proc` with `hidepid` make the worker's usage unreadable | **Open.** `require` refuses to start there (fail closed); `auto` warns. |

### Round 4: host boundary and state (0.8 red team, slice C)

What the host believes about the guest, and what the guest can make the host do: tools and external
functions, error redaction, journals and replay, `SessionPool`, the `Pydeno` front door, and the text
pydeno writes for the host. Every row has a probe in `scripts/autoresearch/metric_security.py`
(section "slice C", each run in a fresh interpreter under a time cap) and a regression test in
`tests/test_redteam_boundary.py`.

| Finding | Status |
|---|---|
| `SessionPool`: a restored session did not always keep the tool budget it had already spent (after an oversized journal, overlapping calls on one session, a failed release, or pools sharing a store) | **Fixed.** Budget-only journal for oversized ones, written under the lease; stored-counter checks on `get` and `release`; unstored sessions are not evicted. `close()` stores unsaved sessions and wakes waiters. Overlapping leases in two pools sharing a store can still exceed the budget: sessions must be routed to one pool (documented). |
| Agent sessions (driving path, before 0.8.0): a tool raising a `BaseException` left a journal that `load()` refused | **Fixed.** Such a tool ends the run like a crash (worker killed, run recorded as lost with what it spent). |
| Agent sessions: concurrent tool calls past `max_inflight_host_calls` were refused depending on timing, which the journal did not record, so such a journal could fail to replay | **Fixed.** The session's wrappers queue calls past the cap and issue them in order. |
| Front door (before 0.8.0): the syntax check after a failed feed depended on guest-replaceable built-ins and ran outside the journal | **Fixed.** The check uses intrinsics the prelude captured before guest code. |
| `SessionPool`: `drop()` overlapping a `get` or a `release` could leave the dropped state in place; a `session()` block could end a newer lease of a session dropped meanwhile | **Fixed.** A barrier holds the session's place during `drop`; `session()` releases only its own lease. |
| `SessionPool` accepted some ids that `release` then refused (long non-ASCII ids; ids that are not valid UTF-8) | **Fixed.** Associated data may be 4096 bytes; ids must be valid UTF-8. |
| `Pydeno` dumps could not be bound to a tenant or a counter: any state a pool dumped loaded into any of its sessions, including an older dump of the same session (its external-call budget restored) | **Fixed (opt-in).** `dump` / `load_session` / `load_snapshot` take `associated_data=`; documented. |
| Tool names equal to the session's or the guest's globals (`__pydeno_agent_settle`, `globalThis`, `JSON`, `console`, ...) were accepted and silently broke the session or the tool | **Fixed.** Refused for bare-global tools; reserved prefixes always. |
| A front-door answer the session refuses (`resume(error="...")`) used up the snapshot, leaving the session paused forever | **Fixed.** Checked before the snapshot is used. |
| Front door (before 0.8.0): `feed_start` surfaced snapshots for names outside the feed's `external_lookup` | **Fixed.** Refused in the guest with a `ReferenceError`, as `feed_run` does. |
| Captured console output (`ExecutionResult.stdout`/`stderr`), error messages and the default printer passed bidi overrides and invisible characters (zero-width, Unicode tags) through, and captured output also escape sequences; the 0.7 CLI printed results and errors raw | **Fixed.** One shared filter (C0/C1 except tab and newline, bidi controls, separators, zero-width and tag characters, soft hyphen, BOM); the sandboxed CLI (#49) filters its output too, and a probe now guards it. Values and a custom `print_callback` stay raw (documented). |
| The 0.7 CLI ran code in-process with no deadline | **Fixed by #49** (the CLI runs `IsolatedRuntime(sandbox="require")` with a deadline). |
| Rollback of a stored journal by someone who can write both the journal and its counter | **Open, known** (documented since 0.7). |
| A journal can still fail to replay for reasons outside the journal (a run that only fits `timeout` on a quiet machine) | **Documented.** `drop` then starts over with a fresh budget; carry the spent budget over yourself if that matters. |

Tried and held: forged, truncated, bit-flipped and spliced journals, journals under another tenant's
associated data or key, moved or rolled-back pool journals; resuming another session's call, a forged
`ToolCall`, a used call, or a pre-dump call after `load`; catalog tools before discovery (refused and
not charged), including through `constructor`/`__proto__`; tool arguments (40 MB strings, 100k-deep and
cyclic nesting, Symbols, functions, Proxies with throwing traps, getters, `__proto__` keys, huge
`BigInt`s, out-of-range dates, lone surrogates, `Map`/`Set` with unhashable members); tool results that
cannot cross; errors from tools (redacted by default, also for `BaseException`); guest error names that
claim host failures; the guest's view of time and entropy (`Date`, `Temporal`, `Intl`, no
`performance`/`crypto`; seeded `Math.random` replays); calls from escaped run wrappers (charged and
journaled); re-entering, closing or dumping a session from its own tool; contextvars across calls;
`http_fetch` URL and address parsing (dot segments, encoded separators, IPv4-mapped/NAT64/6to4/Teredo
and other embedded forms, trailing-dot and confusable hosts); weird callables as tools (partials,
bound methods, classes, async generators, builtins).

### 0.10: the seccomp allow-list (#45), how it was derived and checked

- **Derived by tracing, natively.** `scripts/trace_worker_syscalls.py` ran real workers under
  `strace` through a built-in workload (both engine modes, WebAssembly, modules, async host calls,
  a deadline, a memory kill) and ten isolation test files, on GitHub's native x86_64 and aarch64
  runners, in Debian 13 (Python 3.10 and 3.14), Ubuntu 22.04, AlmaLinux 9 and Fedora images
  (glibc 2.34 to 2.43). About 520 sandboxed workers per image. A worker made 39 to 44 distinct
  syscalls after its sandbox was up; the union over every trace is 46 names, all on the allow-list
  or argument-checked, except the self-test's own probes and `pkey_alloc` (V8 on x86_64 asks for
  memory protection keys and carries on without them; it stays `EPERM`). `uname` and `pkey_alloc`
  appear only on x86_64, `epoll_wait` only on x86_64 (`epoll_pwait` on aarch64).
- **Re-traced under the new filter.** The same traces with the allow-list in force: the only
  refusals a healthy worker met were the self-test's probes (a non-thread `clone`, `kill` of the
  parent, `socket`, a datagram `socketpair`), `clone3` (`ENOSYS`, glibc falls back to `clone`) and
  `pkey_alloc`. Nothing on the default-deny path.
- **What the native runs caught.** CPython's `subprocess` calls glibc's `vfork()`, which on x86_64
  is the `vfork` syscall itself (aarch64 has none and goes through `clone`): with `vfork` on the
  kill list, a library that tried to start a program killed the worker on x86_64 and failed
  politely on aarch64. `fork` and `vfork` are `EPERM` now; `execve` is still killed, since it can
  only follow a fork the filter never allows. This is the second x86_64-only difference after
  `uname`, and the reason sandbox changes are verified on native runners.
- **Verification status of this change (be exact about it).** The traces above were taken in an
  earlier session on CI runners and are carried over, not repeated here. In this change the full
  filter was run natively on x86_64 only (kernel 7.0, Landlock ABI 8: `sandbox_status()` complete,
  kill verified, 1100+ filter, table, canary, violation and isolation tests green on a free-threaded
  CPython 3.14). The aarch64 numbers are checked only against `tests/data/syscalls.json` and the
  kernel-free interpreter in `tests/test_seccomp_program.py`; no aarch64 kernel ran them here.
  Native aarch64 and the container matrix come from CI. No outside review has happened.
- **Not traced:** the macOS sandbox (unchanged), architectures other than x86_64 and aarch64 (the
  filter is not applied there), and kernels older than the runners' 6.17 and the local 6.15
  (containers share the host kernel; the matrix simulates missing Landlock and seccomp, not older
  kernels).

### Rejected after measuring

- `--single-threaded`: no reduction in threads, +79% GC time. Not adopted.
- `RLIMIT_DATA` *at* the memory ceiling: turns the clean RSS kill into a V8 out-of-memory abort.
  Adopted in 0.10 with 1 GiB of headroom above `max_memory` and above what the interpreter already reserved instead (a free-threaded CPython reserves 1 GiB before any script runs; a fixed ceiling killed JIT workers there) (measured: private writable
  memory runs ahead of resident memory by about 45 MiB jitless and 300 MiB with the JIT), so the
  sampled limit fires first in normal use, and the abort, when a burst does reach the ceiling, is
  reported as `memory_limit`.
- `RLIMIT_AS`: V8 reserves 18 to 50 GiB of address space (the pointer-compression cage and guard
  regions) and touches a fraction, so an address-space limit either stops V8 or bounds nothing.
- `RLIMIT_CPU`: it counts the worker's whole life, and a non-root parent cannot raise it again for
  the next command, so it cannot express a per-command cap. The parent's CPU cap stays.
- A default `max_heap_size`: with one set, V8 turns an over-cap `ArrayBuffer` into a fatal heap-limit
  termination instead of a catchable `RangeError`. The buffer cap alone behaves better.

## 5. Lessons that shaped the design

- **Lists leak; ask the kernel.** The macOS environment leak existed because the profile was
  "allow broadly, deny what we thought of". It was found by a reviewer, then closed, and the
  self-test now asks the same question on every start.
- **One architecture is not both.** V8's x86_64 build calls `uname()` while starting and aborts if
  seccomp refuses it; the aarch64 build does not. A change that passed every aarch64 container run
  failed every x86_64 CI cell. Sandbox rules are verified on a native x86_64 runner before they
  are trusted. The 0.10 allow-list was derived from traces taken on native x86_64 and aarch64
  runners, never under emulation (a translator issues the host's syscalls, not the guest's): the
  x86_64 traces show `uname` and `pkey_alloc`, which aarch64 never calls.
- **Verify the verifier.** Each new probe has a negative control (it must report a breach in an
  unsandboxed process), otherwise a green result proves nothing.
- **Count what the kernel counts.** Threads, memory and CPU are read from outside the process, not
  from what the process says.

## 6. What security costs in speed

V8's JIT is where most V8 exploits live, so the worker runs `--jitless` by default. Measured on the
Monty + pydeno examples (one warm worker, median of three, Apple silicon):

| Example | jitless (default) | JIT on | JIT speed-up |
|---|---:|---:|---:|
| three.js terrain (129x129) | 879 ms | 180 ms | 4.9x |
| d3 network layout (200 nodes) | 1,619 ms | 198 ms | 8.2x |
| turf geospatial (8 vehicles) | 1,937 ms | 347 ms | 5.6x |
| ECharts dashboard | 64 ms | 42 ms | 1.5x |
| SQL to Vega-Lite chart | 18 ms | 17 ms | 1.1x |

Most agent-sized work is within a factor of two either way. If you trust the code a little more,
`IsolatedRuntime(jitless=False)` recovers the speed and still runs behind the whole OS sandbox.

## 7. Reproducing this

```bash
.venv/bin/python -m pytest tests -q          # the whole suite (macOS or Linux)
scripts/linux_matrix.sh WHEELS IMAGE         # one Linux image, optionally under a degraded kernel
scripts/trace_worker_syscalls.py --help      # what a real worker calls (strace, in a container)
python benches_py/monty_three_bench.py       # the speed numbers above
```

Found something this report missed? Please report it privately as described in
[`SECURITY.md`](https://github.com/bmsuisse/pydeno/blob/main/SECURITY.md).
