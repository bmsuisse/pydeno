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
  holding the deadline paused or freezing the caller. Use `redact_host_errors=True` if your
  host functions raise exceptions whose text must not reach the guest.
- Validate the arguments of every host function and tool you bind.
- Restore snapshots only from sources you trust and authenticate: pydeno does not verify snapshot
  bytes unless you do (`pydeno.verify_snapshot`).

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

- `tests/test_isolated_runtime.py`, `tests/test_isolated_lifecycle.py`,
  `tests/test_isolated_determinism.py`: behaviour, containment, leaks, limits.
- `tests/test_redteam_syscalls.py`: assume-breach tests that fire every dangerous syscall from a
  sandboxed process and require `EPERM`. `scripts/redteam_syscalls.py` sweeps all ~350 syscalls.
- `tests/test_sandbox_syscall_tables.py`: every number in the seccomp filter checked against the
  kernel's own tables.
- `scripts/linux_matrix.sh` and the `linux-matrix` CI job: the same suites in many distro images, on
  x86_64 and aarch64, and under simulated kernels that lack Landlock or seccomp.
