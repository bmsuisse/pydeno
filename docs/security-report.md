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
| Assume-breach (Linux) | `test_redteam_syscalls`, `scripts/redteam_syscalls.py` | Real dangerous syscalls fired from a sandboxed process; every one must answer `EPERM`. The sweep covers ~350 syscalls. |
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
| Denied calls answer `EPERM` (an exploit can probe the filter freely) | **Fixed** for the never-legitimate calls (`ptrace`, `process_vm_*`, the mount family, `pivot_root`/`chroot`, `setns`/`unshare`, `kexec*`, modules, `bpf`, `perf_event_open`, `userfaultfd`, keyrings, `open_by_handle_at`, `io_uring_*`, swap, `reboot`, `acct`, the x86 relics, `setuid` and friends, `capset`): `SECCOMP_RET_KILL_PROCESS`, reported as `sandbox_violation`. Chosen from a strace of real workers on native x86_64 and aarch64 (`tests/data/worker_syscalls_*.json`), none of which makes any of them once the filter is up. Calls real code does probe (`clone3`, the self-test's `execve`/`socket`/`kill`) keep their errno. |
| Seccomp is a deny-list with a default-deny tail for unreviewed syscalls | **Open.** An allow-list derived from tracing real workers is planned. |
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
| Linux: the thread cap is sampled, not kernel-enforced | **Open.** A pids cgroup or `RLIMIT_NPROC` in the new user namespace is planned. |
| Hosts that mount `/proc` with `hidepid` make the worker's usage unreadable | **Open.** `require` refuses to start there (fail closed); `auto` warns. |

### Rejected after measuring

- `--single-threaded`: no reduction in threads, +79% GC time. Not adopted.
- `RLIMIT_DATA` at the memory ceiling: turns the clean RSS kill into a V8 out-of-memory abort.
  Deferred until the abort path is handled.
- A default `max_heap_size`: with one set, V8 turns an over-cap `ArrayBuffer` into a fatal heap-limit
  termination instead of a catchable `RangeError`. The buffer cap alone behaves better.

## 5. Lessons that shaped the design

- **Lists leak; ask the kernel.** The macOS environment leak existed because the profile was
  "allow broadly, deny what we thought of". It was found by a reviewer, then closed, and the
  self-test now asks the same question on every start.
- **One architecture is not both.** V8's x86_64 build calls `uname()` while starting and aborts if
  seccomp refuses it; the aarch64 build does not. A change that passed every aarch64 container run
  failed every x86_64 CI cell. Sandbox rules are verified on a native x86_64 runner before they
  are trusted.
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
python benches_py/monty_three_bench.py       # the speed numbers above
```

Found something this report missed? Please report it privately as described in
[`SECURITY.md`](https://github.com/bmsuisse/pydeno/blob/main/SECURITY.md).
