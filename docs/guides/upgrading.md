# Upgrading

pydeno is pre-1.0: a minor release can change behaviour. This page lists, per minor, what changed in
a way you could notice, who it touches (`Runtime` is the in-process runtime, `IsolatedRuntime` the
sandboxed worker, which also covers `AgentSandbox` and `pydeno.configure_default_runtime(isolated=True)`),
and what to change. The full history is in `CHANGELOG.md`; facts here come from it and from
`git diff v0.4.5 v0.5.0` and `git diff v0.5.0 v0.6.0`.

## 0.4.x to 0.5.0

The changelog says "no breaking changes: every new limit is opt-in and `Runtime` is unchanged".
`IsolatedRuntime` is new in 0.5.0, so nothing in it can break an existing caller.

| Change | Affects | Who notices | What to change |
|---|---|---|---|
| `import pydeno` is lazy: `IsolatedRuntime`, `WorkerCrashed`, `ToolBridge`, `ToolError` and friends, `WEB_POLYFILLS`, snapshot signing load on first use | `Runtime` | Code that reaches into private modules after a bare `import pydeno`, or that measured import time (about 51 ms down to 19 ms) | Import what you use (`from pydeno import ToolBridge` works unchanged); import `pydeno._tools` explicitly if you really need it |
| Default-runtime helpers can return an `IsolatedRuntime` (new `configure_default_runtime`) | `Runtime` | Type checkers: `get_default_runtime()` is now `Runtime \| IsolatedRuntime` | Nothing at run time unless you opt in; narrow the type or call `configure_default_runtime()` once |
| `deno_core` 0.409 to 0.412 (V8 stays 150.4) | `Runtime` | Anyone who pinned behaviour of a particular `deno_core` | Re-run your test suite; no API change |
| Release extension is stripped and LTO'd (macOS 60 MB to 43 MB) | both | Native crash reports have fewer symbols | Keep a debug build if you symbolicate native crashes |
| Dropping a `SnapshotBuilder` without `build()` no longer leaks its isolate | `Runtime` | Nobody (a fix) | Nothing |
| New opt-in `RuntimeConfig(max_buffer_bytes=...)` | `Runtime` | Nobody unless set | Set it to bound `ArrayBuffer` bytes (`max_heap_size` does not) |
| New `IsolatedRuntime`, `sign_snapshot` / `verify_snapshot`, `clock=`, `random_seed=`, `WEB_POLYFILLS` | new | Adopters | See the [isolation guide](advanced/isolation.md) |
| Isolation wire uses a native JSON codec; the native parser is stricter than `json.loads` about encodings (no BOM, no UTF-16/32, no raw surrogates) | `IsolatedRuntime` | Only code that feeds odd encodings through the boundary | Send well-formed UTF-8 |

## 0.5.x to 0.6.0

0.6.0 is the first release whose defaults change. Most items touch `IsolatedRuntime` only.

| Change | Affects | Who notices | What to change |
|---|---|---|---|
| `redact_host_errors` now defaults to **True** (was False) | `IsolatedRuntime` | Guests, tests or logs that read a host function's exception text: they now see a generic message with the class name | Pass `redact_host_errors=False` if the text is safe to show the guest; otherwise branch on the class name |
| `max_buffer_bytes` defaults to `max_memory // 4` | `IsolatedRuntime` | Guests that allocate big typed arrays: an over-budget allocation is now a catchable `RangeError`, not a worker killed for memory | Set `max_buffer_bytes=` (or raise `max_memory`) if you legitimately need more |
| Signed snapshots are bound to the pydeno release (format `pydeno-snap2`); a snapshot signed by another release is refused | `Runtime` (and anyone using `sign_snapshot`) | Anyone who stores signed snapshots across upgrades: `verify_snapshot` raises `SnapshotAuthenticationError` | Rebuild and sign snapshots with the release you run |
| `RuntimeConfig(snapshot=...)` is refused by `IsolatedRuntime` (it used to be silently dropped, with its bootstrap) | `IsolatedRuntime` | `ValueError` at construction | Use `bootstrap=` source instead of a snapshot |
| Two more V8 flags in the worker (linear-time regexp fallback, `--freeze-flags-after-init`) | `IsolatedRuntime` | Anything that passed `v8_flags` that must change after V8 starts | Pass flags at construction only |
| `sandbox="auto"` emits a `RuntimeWarning` when the platform's full set of layers did not apply | `IsolatedRuntime` | Test suites with `-W error`; log noise on kernels without Landlock | Use `sandbox="require"` in production, or filter the warning where degraded is expected. `pydeno.sandbox_status()` answers it up front |
| Bind names must be plain identifiers (`IsolatedRuntime` now checks, as `ToolBridge` already did) | `IsolatedRuntime` | `ValueError` for names such as `"my-tool"` or `"a.b"` | Rename to `[A-Za-z_][A-Za-z0-9_]*` |
| Startup self-test: the worker tries forbidden operations before guest code and refuses to start if one works | `IsolatedRuntime` | `WorkerCrashed("worker failed to start: sandbox self-test failed ...")` on a host with a gap | Treat as a real finding, not a flake (it is `sandbox_unavailable` in `classify_error`) |
| `sandbox="require"` refuses when the worker's memory, CPU or thread usage cannot be read (hardened `/proc`, missing libproc) | `IsolatedRuntime` | `WorkerCrashed("... cannot be enforced on this system ...")`; other modes only warn | Make `/proc` readable, or accept `sandbox="auto"` knowing the limits will not fire |
| Host-call arguments from the guest are capped (1M values, depth 128) and the bridge captures its intrinsics, runs in strict mode | `Runtime` and `IsolatedRuntime` | A guest passing a huge or deeply nested argument to a host function now gets a `RangeError` | Pass less data, or chunk it |
| A worker with more than 64 threads is killed; memory and threads are watched while a host function runs; the in-flight host-call cap is runtime-wide | `IsolatedRuntime` | `WorkerCrashed` for thread floods; `max_inflight_host_calls` counts calls across commands | Lower concurrency in the guest, or raise `max_inflight_host_calls` |
| macOS profile no longer allows `sysctl-read` and denies process-info, IOKit, hardware-ID and host-statistics routes | `IsolatedRuntime` (macOS) | A library that read host facts (CPU count, memory size) inside the guest | Pass such facts in as arguments |
| Linux seccomp: stream-only `socketpair`, `prctl` allow-list, no `sysinfo`, no executable mappings when jitless, and more | `IsolatedRuntime` (Linux) | A bundle that relied on one of those syscalls | Run it on the 0.6 matrix (`scripts/linux_matrix.sh`); report real regressions |
| Forked children forget the parent's workers; `eval_async` uses a thread of its own | `IsolatedRuntime` | Pre-fork servers that shared a runtime with children | Create the runtime after the fork |
| New `AgentSandbox`, `pydeno.integrations.pydantic_ai`, optional extra `pydantic-ai` | new | Adopters | See [Agent sessions](agent-sessions.md) |

## 0.6.x to 0.7.0

Mostly additions (async classes, results, schema tools, diagnostics). Four things can change behaviour
you have today.

| Change | Affects | Who notices | What to change |
|---|---|---|---|
| A JS `Map`, `WeakMap`, `WeakSet` or `Error` crossing the boundary **raises** (`Cannot serialize ...`); it used to become an empty dict silently | both | Code that returned one of these and got `{}` | Convert first: `Object.fromEntries(map)`, `[...map]`, `{name: e.name, message: e.message}` |
| `AgentSandbox.dump()` after a crash, timeout or kill **returns** the last good journal plus a `lost` record; it used to raise | `AgentSandbox` | Code that relied on the error to detect a dead session | Check `is_closed()`; `load()` charges the lost run's tool calls (no budget refund) |
| `AgentSandbox` always routes `console.*` through the parent, and `Done`/`Failed` carry the console output (left out of equality) | `AgentSandbox` | A host that set `max_host_calls`: console calls now count against it | Raise the cap, or stop logging in a loop |
| Messages pydeno writes itself (catalog guidance, wrong-argument `TypeError`) are not hidden by `redact_host_errors`; errors from host tools are still redacted | `AgentSandbox` | Code that matched on the generic "host function failed" text for those | Match on the error class |
| The isolation worker starts with `-I -S` and imports `pydeno` from the parent's own package directory (no `site`, so no `.pth` or `sitecustomize` runs in the worker); a custom `python=` is started as before | `IsolatedRuntime` | Code that relied on a `.pth` file or `sitecustomize` taking effect inside the worker | Do that work in the parent, or pass what the worker needs through `bootstrap`/bound functions |
| The worker's seccomp filter denies `memfd_create` | `IsolatedRuntime` (Linux) | Nobody running normal JavaScript | Nothing |
| New: `AsyncIsolatedRuntime`, `AsyncAgentSandbox`, `SessionPool`, `ExecutionResult`, `SchemaTool` and the lazy catalog, `sandbox_status()`, `classify_error()`, `check_source()` | new | Adopters | See the [async guide](advanced/async.md), [async agent sessions](advanced/async-agent-sessions.md) and the [reference](../reference/error-kinds.md) |

0.6.1 fixed a macOS-only bug: the sandboxed worker aborted at start on Python 3.10 to 3.12. If you are on
0.6.0 there, upgrade.

## 0.7.x to the next release: the `Pydeno` front door

**Two things change behaviour: `python -m pydeno`** (the CLI rows below) **and the limit fixes** (the
last rows). `Pydeno` / `AsyncPydeno` and their sessions, snapshots, limits and errors are new names;
otherwise every existing class keeps its behaviour, and the docs now lead with `Pydeno` and file the
building blocks under "Advanced".

| Change | Affects | Who notices | What to change |
|---|---|---|---|
| New: `Pydeno`, `AsyncPydeno`, `PydenoSession`, `AsyncPydenoSession`, `PydenoSnapshot`, `AsyncPydenoSnapshot`, `PydenoComplete`, `PydenoLimits` and the `PydenoError` family | new | Adopters | See the [front-door guide](quickstart-pydeno.md); a Monty user can keep their code's shape |
| `AgentSandbox` / `AsyncAgentSandbox` accept `runtime=` (an already-built, fresh runtime, such as a pool checkout) | new, opt-in | Nobody unless passed | Nothing |
| `classify_error()` sees through a `PydenoError` to the pydeno exception it wraps | new | Nobody | Nothing |
| New: `Pydeno(max_tool_threads=...)` / `AsyncPydeno(max_tool_threads=...)` (default 128, clamped to the process ceiling of 512) and `ToolThreadLimitError`: per-pool cap on session tool threads (an upper bound, not a reservation) | new | Pools whose sessions hold many tool threads at once (many concurrent sessions calling externals, or externals that never return); processes with more than four default pools open (a `RuntimeWarning`) | Raise or lower the caps so they fit the ceiling, or give externals their own timeouts |
| `AgentSandbox.run()` / `execute()` drive the worker from the calling thread, which keeps enforcing every limit; tool calls are answered on the session's own threads (a tool thread for plain functions, its loop thread for coroutine functions; started at its first tool call, never shared with another session), in the order the guest made them, as before, each call in a fresh copy of the caller's context, instead of on the calling thread | `AgentSandbox` | Tools that read the caller's **thread-local** state (`threading.local()`): they no longer see it. Tools see the caller's contextvars as before (a copy per call) | Keep per-call state in contextvars or closures, not thread-locals |
| `AsyncAgentSandbox.run()` / `execute()` stop waiting for a tool once its run has ended (the supervisor killed the worker for `max_pause`, the CPU cap or memory): the call raises at once instead of when the tool returns; the tool is cancelled | `AsyncAgentSandbox` | Nobody, unless they waited for a slow tool to finish after its run was killed | Nothing |
| `SandboxPool` builds its runtimes through an overridable core (`_core_type`, private) | internal | Nobody | Nothing |
| `python -m pydeno` runs code in `IsolatedRuntime(sandbox="require")`; it used the in-process `Runtime` | the CLI | Scripts that ran `python -m pydeno` on a machine without the complete OS sandbox: they now exit with code 4 | Pass `--sandbox auto`, or `--no-sandbox` for code you trust; `pydeno.sandbox_status()` shows what is missing |
| A positional argument is JavaScript to evaluate; it used to be a file name | the CLI | `python -m pydeno script.js` now evaluates the text `script.js` (a `ReferenceError`, exit code 1) | `python -m pydeno -f script.js` |
| The result prints as JSON (`"text"` with quotes, `{"a": 1}`); it used to print Python's `str()` of it | the CLI | Scripts that parse the output | Parse it as JSON, or pass `--raw` for a plain string |
| A JavaScript error exits with code 1 and `pydeno: js_error: ...` on stderr; a timeout exits 3, a missing sandbox 4, another failure 5, a result with no JSON form 6 (all used to exit 1) | the CLI | Scripts that match on the old `JavaScript Error:` text or treat every failure the same | See the exit codes in [Command line](cli.md) |
| The code runs in a separate worker with `--jitless` V8 (no WebAssembly), a 30 s default deadline and a 1 GiB memory cap; the old CLI had no deadline | the CLI | Code that ran long, used a lot of memory or used WebAssembly from the CLI | `--timeout`, `--max-memory`; for WebAssembly use the API (`IsolatedRuntime(jitless=False)`) |
| Limit values are validated: NaN, infinity, zero/negative deadlines, bools, strings, and floats for counts (`max_memory`, `max_host_calls`, `max_inflight_host_calls`) raise `ValueError`/`TypeError` | `IsolatedRuntime`, `AsyncIsolatedRuntime`, pool `checkout()`, `AgentSandbox`/`AsyncAgentSandbox`, per-call `timeout=` | Code passing such values (they silently disabled the limit before) | Pass `None` to remove a limit; use an `int` for byte and call counts |
| Console output no longer pauses the hard deadline (tool calls still do) | runtimes with `on_console` / `capture_console`, agent sessions, `Pydeno` feeds | Hosts with a slow `on_console` or `print_callback` and a chatty guest: it times out at its deadline instead of running longer | Make the console handler fast (buffer it), or raise the deadline |

## Safe to bump?

**From 0.4.x to 0.5.0** (`Runtime` users: nothing to change)

- [ ] `import pydeno` followed by `pydeno.X` still works for every public name (it is lazy, not removed).
- [ ] Your type checker accepts `get_default_runtime()` returning `Runtime | IsolatedRuntime`.
- [ ] Your test suite passes on `deno_core` 0.412.
- [ ] If you adopt `IsolatedRuntime`: set `sandbox="require"` and read `.sandbox`.

**From 0.5.x to 0.6.0**

- [ ] You use `Runtime` only, and store no signed snapshots: bump; the bridge argument caps are the one thing to test.
- [ ] You use `IsolatedRuntime`: decide on `redact_host_errors` explicitly (the default flipped), and check anything that reads host exception text.
- [ ] No `-W error` job fails on the new degraded-sandbox `RuntimeWarning` (or you use `sandbox="require"`).
- [ ] Every bound name is a plain identifier.
- [ ] You pass no `snapshot=` to `IsolatedRuntime`.
- [ ] Signed snapshots were rebuilt and re-signed with 0.6.
- [ ] Large typed arrays in guests fit `max_buffer_bytes` (`max_memory // 4` by default).
- [ ] On your deployment hosts `pydeno.sandbox_status().complete` is `True`.
- [ ] You handle `WorkerCrashed` by cause: see [Error kinds](../reference/error-kinds.md).
