# Security policy

## Reporting a vulnerability

**Do not open a public issue.** Report it privately through GitHub:
<https://github.com/bmsuisse/pydeno/security/advisories/new>

Please include a minimal reproduction (the JavaScript, the `IsolatedRuntime`/`Runtime` settings,
your OS and kernel). A report that shows a guest doing something it should not be able to do is
the most useful kind.

## What is in scope

pydeno's job is to run JavaScript you do not trust. These are vulnerabilities:

- **A sandbox escape from `IsolatedRuntime`**: guest code reading or writing a file, opening a
  socket, starting a process, signalling or tracing another process, reading the host's
  environment or secrets, or otherwise doing anything the worker's OS sandbox is meant to stop.
- **A host crash or hang caused by guest code under `IsolatedRuntime`** that the parent does not
  contain (the parent should always get a catchable error and stay alive).
- **A flaw in the worker/parent protocol**: a worker frame that crashes, hangs or exhausts memory
  in the parent, or gets a host function called that was never bound (`pydeno/_wire.py`,
  `pydeno/_isolated.py`).
- **A bypass of a resource limit** the documentation promises (`max_memory`, `request_timeout`,
  `max_host_calls`, `max_buffer_bytes`, serialization limits).
- **An unbound or forged capability**: calling a host function or tool the host did not hand to the
  guest (`ToolBridge`, op tokens).
- **Leaking host information into the guest** beyond what is documented (host paths, timezone,
  environment, Python internals, tracebacks).

## What is not

- **Plain `Runtime` can be crashed or hung by hostile guest code.** V8 runs in your process, and a
  single native builtin can abort it or ignore `timeout=` (for example
  `new Array(2 ** 32 - 1).fill(0)`). This is documented, pinned by strict-`xfail` tests in
  `tests/test_monty_parity_security.py`, and is why `IsolatedRuntime` exists. Use that for code you
  do not trust.
- Things a host function you wrote does with its arguments. A host function runs with your full
  authority; validate its inputs.
- Path *existence* on macOS, and on Linux where unprivileged user namespaces are unavailable (so
  the empty-root layer cannot apply): a sandboxed worker can tell a path exists, and on Linux can
  also `stat` it (Landlock does not govern metadata). With the empty root, nothing is visible.
  Documented and pinned by tests in `docs/guides/advanced/isolation.md`.
- Denial of service against the machine that needs a capability the worker does not have.

## Threat model in one paragraph

Guest JavaScript is hostile. The host process is trusted. Between them: a process boundary, an OS
sandbox applied before V8 starts (Seatbelt on macOS; Landlock plus a seccomp filter on Linux, with
all privileges dropped), a JIT-free V8, hard time and memory limits enforced from outside the
worker, and a decoder that treats every worker frame as untrusted input. The design and its
reasoning are in `docs/superpowers/specs/2026-10-02-isolated-runtime-design.md`; the layers and
their limits are in `docs/guides/advanced/isolation.md`.

## Hardening you should turn on

- Use `IsolatedRuntime`, not `Runtime`, for untrusted code. If you use the module-level helpers,
  call `pydeno.configure_default_runtime(isolated=True, sandbox="require")` once at start-up.
- Pass `sandbox="require"` so a platform or kernel that cannot apply the OS sandbox refuses to
  start instead of quietly running without it.
- Keep the default `max_memory` and hard deadline, or set your own.
- Leave `max_host_wait`, `max_inflight_host_calls` and `write_stall_timeout` at their defaults
  (600 s, 64, 10 s) unless you have a reason; they stop a guest or compromised worker from
  holding the deadline paused or freezing the caller.
- Leave `redact_host_errors` at its default, `True`: the guest then sees `host function failed`
  (with the exception class name) instead of the text of an exception a host function raised.
  Setting it to `False` hands the guest that text verbatim, so paths, queries, credentials or
  stack details in it reach the guest; do that only when the guest is trusted with them.
- Do not run the host process as PID 1. Run with an init such as `tini` (`docker run --init`,
  `podman run --init`) so orphaned and exited workers are reaped; see issue #128.
- Know the sandbox default. `IsolatedRuntime` and `pydeno.configure_default_runtime(isolated=True)`
  default to `sandbox="auto"`, which runs with fewer OS layers (and warns) when some cannot be
  applied. Pass `sandbox="require"` explicitly; see issue #127, which may change this default.
- Validate the arguments of every host function and tool you bind.
- Bind session state to its owner: `AgentSandbox.dump(key, associated_data=...)`, and the same
  `associated_data=` on `PydenoSession.dump` / `load_session` / `load_snapshot`, with a tenant id and
  a counter you keep (or use `SessionPool`, which does both). A signature alone proves the state is
  yours, not whose it is or that it is the newest.
- Restore snapshots only from sources you trust and authenticate: pydeno does not verify snapshot
  bytes unless you do (`pydeno.verify_snapshot`).

## Linux resource visibility

The supervisor must be able to read the worker's `/proc/<pid>/stat` and `statm` after the
worker drops privileges. A `hidepid=2` procfs mount can hide a worker that changed from root
to `nobody`, or even a same-UID worker after it clears its dumpable flag. Successfully reading
an ordinary same-UID child does not prove these limits work.
`sandbox_status()` therefore hardens its resource-probe child like a worker and waits for that
step before measuring it. Missing memory, CPU or thread counters make `resource_probes.applied`
and `complete` false. Both isolated runtimes refuse startup under `sandbox="require"`, and
warn under `auto`, if an enabled counter is unreadable.

For the supervisor's procfs mount, use `hidepid=0`, or deliberately configure procfs visibility
for the service (for example an authorized `gid=` exemption). Verify the resulting deployment
with `sandbox_status()` and `sandbox="require"`; do not assume root or a container label alone
grants access. This is a startup check, not a promise that later procfs or credential changes
are harmless.

## Supervisor termination authority

Both isolated runtimes check signal permission against the actual worker after startup
hardening, before accepting guest commands or handing the worker to a pool. If permission is
missing, startup refuses in every sandbox mode with a non-retryable `sandbox_unavailable`
error. The still-trusted worker receives a close command and is reaped.

`sandbox_status().termination` additionally tests SIGKILL against a disposable hardened child.
Missing authority makes `complete` false. These are startup checks: the supervisor must retain
its signal permissions throughout each worker's lifetime. Signal zero on the actual worker
checks current permission; it is not a proof against later credential changes or a host policy
that distinguishes individual signal numbers. Validate custom host policies with the status
probe and deployment tests as well.

## Deploying it: the outer boundary

The in-process layers contain a V8 bug to the worker; they do not stop an attacker who chains it
with a *kernel* bug. Container and microVM sandboxes exist for that gap, and they are a deployment
choice, not a library feature, so build them around pydeno:

- **One `IsolatedRuntime` per trust unit.** Everything inside one runtime can see everything else in
  it (globals, earlier results). Never share a runtime between tenants; derive it from the
  authenticated caller, and do not reuse one after a different caller's code ran in it.
- **Keep credentials outside.** Bind a host function that *uses* the secret and returns only the
  result (`bind_function`, `ToolBridge`); never put a token in guest source, a bound object or an
  error message (`redact_host_errors=True` hides host exception text).
- **No egress.** pydeno gives the guest no network, and the OS sandbox blocks it for a compromised
  worker. Keep it that way at the container level too.
- **Treat output as untrusted.** Results are plain data, but validate them before using them in a
  query, a path, HTML or a prompt.
- **Run the host process in a locked-down container.** The flags below are the strictest
  combination the Linux test matrix already runs under (no network, no capabilities); the
  read-only root and `no-new-privileges` flags are recommended, not yet exercised by the tests:

      podman run --rm --network none --read-only --tmpfs /tmp:exec \
        --cap-drop all --security-opt no-new-privileges \
        --pids-limit 256 --memory 2g --cpus 2  your-image

  **Do not wrap the worker** (for example `IsolatedRuntime(python="bwrap ... python")`). The parent
  measures the process it started: behind a wrapper that is `bwrap` (1 MiB, one thread, no CPU time),
  not the worker (hundreds of MiB, nine threads), so `max_memory`, the CPU cap and the thread cap all go
  blind; the worker also loses its own empty-root layer, and `sandbox="require"` does not notice. Put
  the **whole host process** inside the container, microVM or `systemd-run --user --scope -p
  MemoryMax=... -p TasksMax=...` instead, so the outer limits wrap everything pydeno starts. The worker
  already runs in its own session, so TIOCSTI is not reachable, and the seccomp filter denies it
  regardless.
  Add gVisor (`--runtime runsc`) or a microVM (Firecracker, Kata) when a kernel boundary is
  required. pydeno's seccomp filter and Landlock apply inside any of them.

## Keeping the engine current

pydeno embeds V8 through `deno_core`. V8 and `deno_core` security fixes reach pydeno only when the
dependency is upgraded, so a pydeno release that pins an old `deno_core` carries that engine's
known vulnerabilities. The JIT-free default and the process sandbox limit the damage of an engine
bug but do not remove it. Watch the `deno_core` and V8 release notes, and upgrade promptly.
`scripts/check_engine.py` (run weekly by `.github/workflows/engine-watch.yml`) fails when a newer
`deno_core` carries a higher V8 than `Cargo.lock`; "latest `deno_core`" alone is not the signal, because
a newer release can use an older V8.

## Supported versions

Security fixes are made against the latest release. Older releases are fixed on a best-effort
basis.

## How the sandbox is tested

The full account (methods, independent review rounds, every finding with its status, what is still
open, and what security costs in speed) is in [`docs/security-report.md`](docs/security-report.md).

- `tests/test_isolated_runtime.py`, `tests/test_isolated_lifecycle.py`,
  `tests/test_isolated_determinism.py`: behaviour, containment, leaks, limits.
- `tests/test_isolated_capability_denial.py`: a checklist of what untrusted code tries first (read or
  write a file, network, subprocess, environment, `Deno`/Node/browser globals, the runtime's own
  plumbing), through both `eval` and `eval_async`, plus a check that nothing reached the host.
- `tests/test_redteam_syscalls.py`: assume-breach tests that fire every dangerous syscall from a
  sandboxed process and require `EPERM`, or the death of the process for the never-legitimate ones
  (the seccomp filter is an allow-list that kills on those). `tests/test_sandbox_violation.py` does
  the same from a real worker and checks the parent reports `sandbox_violation`.
  `scripts/redteam_syscalls.py` sweeps all ~350 syscalls; `scripts/trace_worker_syscalls.py` shows
  what a real worker calls.
- `tests/test_sandbox_syscall_tables.py`: every number in the seccomp filter checked against the
  kernel's own tables.
- `scripts/linux_matrix.sh` and the `linux-matrix` CI job: the same suites in many distro images, on
  x86_64 and aarch64, and under simulated kernels that lack Landlock or seccomp.
