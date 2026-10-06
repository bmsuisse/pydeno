# Changelog

## Unreleased / 0.10.0

### Added

- **Fair, bounded tool descriptions.** `describe_tool_catalog(namespaces, max_chars=8000)`
  selects complete tool descriptions round-robin across namespace objects and labels the
  catalog complete or partial. Types are local to each entry; the entry character budget
  includes examples and schemas, while instructions and namespace summaries are additional.
  This description-only helper does not bind or discover capabilities. Existing
  `describe_tools()` output is unchanged.

### Fixed

- **Late async tool calls after cancellation.** A queued callback entering an already-closed
  `AsyncAgentSandbox` is refused before allocating an unanswered future or charging the tool
  budget. This prevents shim tasks from remaining parked after cancellation.
- **Bounded model previews.** Dictionary previews inspect only the five entries they display,
  rather than copying the entire dictionary first. Displayed output is unchanged; no
  wall-clock speed improvement is claimed.
- **Local Linux overlays include Python subpackages.** `OVERLAY_PY=1` now includes integrations
  and tools while preserving the installed native extension. Release artifact gates continue
  to run without overlays.
### Documentation

- Research note on a custom V8 build (pointer compression, V8 sandbox, build-time jitless, disabled features), against the `deno_core` 0.412.0 / `v8` 150.4.0 pins: recommendation is not to build one for 0.10 (#44). See `docs/contributing/research-custom-v8-build.md`.

## 0.9.0 — 2026-10-05

Highlights: **gates** (a host-side check of the exact source before it runs, with a fail-closed
contract and a static source policy), **`load_wasm()`** for trusted WebAssembly modules, **opt-in
worker caps** for pools, a **persistent worker event loop** (about 35 to 40 percent faster async
commands and feeds), bounded memory for large async reply backlogs, and a second hardening pass
(stream sources belong to one runtime, host functions that re-enter their runtime get an error
instead of killing the process, no `close()` hang, live registration ids are never reused). See
"Upgrading" for the few behaviour changes.

Security note: this release hardens behaviour of 0.8.0 and earlier. Several defects found in
independent reviews are fixed (listed below, in neutral terms). The gate is defence in depth, never the
boundary; the sandbox is.

### Added

- **Gates: a host-side check of the exact source before it runs.** A gate is a callable
  `(source, GateContext) -> Verdict(allow, reason, labels)`, sync or async.
  - **Where it attaches.** Pass it as `gate=` (with `gate_timeout=`, default 10 s) to:
    - `Pydeno` / `AsyncPydeno`: every `feed_run` / `feed_start`;
    - `AgentSandbox` / `AsyncAgentSandbox`: `start` / `run` / `execute`;
    - `IsolatedRuntime` / `AsyncIsolatedRuntime`: `eval*`, `execute*`, `add_static_module` sources,
      module-loader sources, and `RuntimeConfig.bootstrap` (checked before the worker starts).
  - **It fails closed.** A denial raises `GateDenied` (kind `gate_denied`, not retryable). A gate that
    raises, times out or answers with anything but an exact `Verdict` raises `GateUnavailable` (kind
    `gate_unavailable`, retryable, and the run is still blocked). Both are `PydenoError` subclasses.
    Cancellation propagates unchanged.
  - **No gap between check and use.** The source is normalised to an exact `str` once. The gate sees
    it, and the same string runs. Sources over 16 MiB are refused before the gate is called.
  - **No side effects.** A denied call sends nothing to the worker and uses no budget, in-flight
    slot or journal record. Journal replay (`load_session`, `load_snapshot`, `AgentSandbox.load`) is
    not re-gated.
  - **Standalone use.** `gate_check` / `async_gate_check` run a gate in your own process.
  - **Sync gates and async callers.** In the async classes and `async_gate_check`, sync gates run on
    a gate thread, never on the event loop. There `gate_timeout=None` is refused. The gate threads
    form one process-wide pool of daemon threads, 32 by default; change it with
    `set_gate_threads(n)` or `PYDENO_GATE_THREADS`.
  - **Module loaders and closed sessions.** A module-loader refusal is raised by the command that
    imported, keeps its `__cause__`, and is raised even when the guest catches the failed import.
    A closed session or runtime never calls its gate.
  - **Pools.** `SandboxPool` / `SessionPool` pass `gate=` through.
  - **Signatures checked up front.** A gate may also take only the source (`async def
    classify(source)`). A gate whose signature fits neither form raises `TypeError` when it is
    configured or passed to `gate_check`, so a programming error is not mistaken for an outage.
  - **Static policy.** `check_source(source, policy=SourcePolicy(...))` covers:
    - forbidden identifiers and globals;
    - `import()`;
    - `eval`, and timers given strings;
    - the `Function` constructor and `WebAssembly`;
    - `max_source_bytes`;
    - optionally, computed access on a global (`forbid_computed_global_access`, best effort).

    By default the scan fails closed. It decodes every escape (`\u`, `\u{...}`, `\x`, legacy
    octal, identity escapes, line continuations) and reads the whole text, strings and comments
    included. It runs in linear time, with `max_source_bytes` defaulting to 1 MiB.
    `ignore_strings_and_comments=True` selects a tokenizer-based precise mode instead: opt-in,
    best effort, with its known bypasses listed in the guide. The messages are fixed templates
    (`POLICY_MESSAGES`, a public contract). `Finding.text` is the bare message without a location.
    `static_gate(policy)` turns a policy into a gate, and `.check(source)` returns its findings. `all_of(*gates)` stops at the first denial; `any_of(*gates)` stops at the
    first allow. `check_source(source)` without a policy is unchanged.
  - **Moved class.** `PydenoError` now lives in `pydeno._errors`. It is the same class, still
    exported as `pydeno.PydenoError`.

  See [`docs/guides/gate.md`](docs/guides/gate.md).

- **`load_wasm()`: a trusted WebAssembly module, loaded by the host** (#37). On `Runtime`,
  `IsolatedRuntime` and `AsyncIsolatedRuntime`: `rt.load_wasm(bytes_or_path)` returns a
  `WasmModule` (`AsyncWasmModule`) with `.exports` (name -> callable), `.signatures`,
  `.call(name, *args, timeout=)` and `.unload()`. The host reads the bytes (the guest never gets
  file access), refuses more than `max_bytes` (default and ceiling 8 MiB), parses the type, import,
  function and export sections with bounded reads, refuses modules with imports, checks each
  argument against the signature (`int` for `i32`/`i64`, exact up to 64 bits; `int`/`float` for
  `f32`/`f64`) and runs each call under the runtime's timeout. The bridge compiles and instantiates
  the module with WebAssembly intrinsics captured before any guest code, through one new fixed
  global, `__pydeno_wasm_load`, installed only where V8 has WebAssembly; the instance is held only by
  the host (the isolated worker takes its reference to it before any guest code, so a loader the
  guest planted where V8 has no WebAssembly, for example under `--lite-mode`, is never called).
  **Explicit opt-in for trusted modules:** the isolated runtimes need `jitless=False`
  (with the default, `load_wasm` raises `RuntimeError` in the parent and the worker is unchanged),
  and a module's linear memory is not bounded by `max_buffer_bytes` (it counts toward `max_memory`
  in the isolated runtimes; nothing bounds it in an in-process `Runtime`). Not on `Pydeno`,
  `AgentSandbox` or `SessionPool`, whose journaled sessions could not replay it (a `SandboxPool`
  checkout is a plain runtime and works). See
  [`docs/guides/advanced/webassembly.md`](docs/guides/advanced/webassembly.md);
  `benches_py/wasm_kernel_bench.py` compares one kernel as JavaScript and as WebAssembly (no faster
  under the JIT; about 19x faster than jitless JavaScript).
- **Opt-in worker caps for pools** (#81). `SandboxPool`, `AsyncSandboxPool`, `Pydeno` and
  `AsyncPydeno` take `max_workers=None` (default: unchanged, cold starts without limit) and
  `checkout_timeout=30.0`. With a cap, the pool counts every worker process that has not exited
  (starting, ready, checked out, and the custom-memory and replay workers of `Pydeno` sessions); a
  checkout at the cap waits up to `checkout_timeout` (polling every 20 ms, not FIFO) and raises the
  new `pydeno.CheckoutTimeout` (a `TimeoutError`; `classify_error` kind `checkout_timeout`,
  retryable). `load_session` / `load_snapshot` on a capped pool kill the session's current worker
  after the state is authenticated and before the replay, so a load never needs a second slot; if
  the replay then fails, the session has no worker until the next successful load (the next feed
  raises `PydenoCrashedError` saying so; another `load_session` / `load_snapshot` recovers it). The
  killed worker's slot is handed to the replay, so a checkout already waiting cannot take it in
  between. `stats()` adds `max_workers`, `workers`, `waiting` and `checkout_timeouts`.
  Based on the contribution in #93.

### Changed

- **The static source scanner is native** (#107): `check_source` and `static_gate` scan in Rust
  with the GIL released, with findings identical to the Python scanner, which stays as the reference.
- **Faster async commands in the isolated worker** (#60). The worker keeps one event loop for its
  whole life instead of building one per command, created before the OS sandbox goes up (no thread
  starts). Each command still ends with the loop emptied: pending tasks are cancelled and finished,
  and callbacks that a finished host call left queued run before the next command starts, so nothing
  is held over to a later command. Measured on macOS arm64 (release build, paired runs): a warm
  `eval_async` about 237 to 150 microseconds, a warm `feed_run` about 220 to 140 microseconds,
  checkout plus 11 feeds about 4.4 to 3.4 ms. The synchronous paths are unchanged.
- **Large reply frames are copied once, not twice, in the host's frame readers** (#66). The sync
  and async readers took a payload out of the reassembly buffer as `bytes(buf[start:end])`, which
  first builds a payload-sized `bytearray` slice; payloads of 64 KiB and more are now copied
  through a memoryview released before the buffer is resized. Smaller frames, the sync single-read
  fast path, the 16 MiB cap and all frame checks are unchanged, and a payload is still owned `bytes`.
  Measured on macOS arm64 (release build, one native extension, 9 interleaved fresh-process rounds,
  medians): reading 16 MiB frames, Python-heap peak 52.1 to 35.3 MB and peak RSS 69.8 to 53.0 MB,
  reader CPU 17.6 to 12.3 ms (async) and 27.8 to 22.8 ms (sync) per three frames. End to end, a
  16 MiB `eval` reply has a Python-heap peak of 52.1 to 35.3 MB, and 1 to 16 MiB replies use about
  2 to 4 percent less host CPU; the process's peak RSS for such a call is unchanged, because a later
  phase sets it. Small frames, warm `eval`, host calls and `feed_run` are unchanged within noise.
- **Clearer limit and diagnostics wording** (#84). Agent sessions name the argument the caller passed
  (`timeout`, `max_pause`) when they reject a value; `-0.0` is stored as `0.0`; the docs describe
  how the Proxy, console-deadline and default-printer limits behave.
- **`stats()["checkouts"]` / `["cold_starts"]` count only checkouts that got a worker**, with or
  without a cap: a cold start that fails to start (or, with a cap, a checkout that raised
  `CheckoutTimeout`) is no longer counted in either.

### Fixed

- Final integration review: gate pools reset their synchronization before child threads run;
  module-loader gates restore control-flow exceptions after completing the worker reply.
  Failed or cancelled Wasm unloads remain retryable.
- Persistent command-loop cleanup serializes the idle transition with submissions and drains
  cancellation descendants and old delayed callbacks before the next command.
- Capped session replay keeps the old worker counted until its process exits. A replacement
  waits or raises `CheckoutTimeout`; cancellation retains accounting for a still-live process.
- Release containment tests receive the complete scanner corpus assets. The scanner's absolute
  speed budget runs on optimized builds; correctness and linearity remain tested in debug too.

- **The synchronous isolated runtimes enforce their limits while a host handler runs** (#84). In
  `IsolatedRuntime`, agent sessions and `Pydeno` feeds, the hard deadline (with its console
  allowance), `max_host_wait` and the CPU cap were checked only after a synchronous `on_console`,
  `print_callback` or tool returned: a 6 s console handler under a 1 s deadline kept the worker
  alive for about 6 s (the async runtimes: about 2 s). The idle watchdog now applies them while a
  handler runs and kills the worker on time (about 2 s in that case); the handler is not
  interrupted and still runs on the calling thread, and the command raises `RuntimeTimeout` once
  it returns.
- **`check_source(source)` without a policy runs in linear time on hostile input.** It no longer
  retries a regex scan after one failed on the same line, and its `\u{` look-ahead is capped.
  Results are unchanged.
- **A large backlog of async replies no longer gets a worker killed that is still reading, and the
  parent's memory is bounded** (#86, #91). Replies to async host calls are now written one at a time
  under a send lock, a reply waits for its turn before it is encoded, and its host call stays in
  flight (and counts toward `max_inflight_host_calls`) until the reply has been written. A worker that stops reading or drips bytes is still killed
  within one stall window (`write_stall_timeout`). Measured with 3000 concurrent calls of 200 KB:
  the worker is no longer killed at a 1 s stall limit, the transport buffer peaks at about 0.1 MB
  instead of about 590 MB, and the parent's peak RSS drops from about 3 GB to about 0.7 GB.
- **A host function that re-enters its own runtime, or returns a stream source, no longer aborts the
  process** (#58). Calling `rt.eval` (or any `Runtime` method) from inside a host function made
  PyO3 raise a panic that was re-raised when the call returned and aborted the process, and a
  returned `PyStreamSource` hit the same check. The guest now gets a catchable `RuntimeError`
  ("this object cannot be used from the runtime thread, where host functions run; ...") with fixed
  text, the panic detail goes to the log, and the runtime stays usable. `PyStreamSource` no longer
  has to stay on the thread that made it. Keep a reference to a source until the guest has read it
  (the finalizer cancels the stream); see the "Inside a host function" section of the runtime guide.
- **Registered function and stream ids are never reused while live** (#89 follow-up). The ids are
  32-bit counters; after a wrap a new registration could overwrite a live entry. Allocation now skips
  live ids, and a wrapper that fails to build rolls its registration back without a double release.
- **A stream source is refused by any runtime other than the one that created it** (#98). Stream
  ids are allocated per runtime, so a source from runtime B returned by runtime A's host function
  (or passed to A's functions, bound into A, or yielded by one of A's streams) was read through A's
  own stream with the same id, and A's guest received A's data. Each source now carries its
  runtime's identity and the transfer raises `RuntimeError` ("this stream source belongs to a
  different runtime; ..."); a source whose runtime is closed raises `RuntimeError` too. The same
  runtime is unaffected, and `IsolatedRuntime` has no stream sources.
- **`AsyncIsolatedRuntime`: a burst of async host replies no longer gets a worker killed that is
  still reading, and the parent's memory for it is bounded** (#86, #91). Replies are encoded and
  written one at a time under a send lock, so the transport buffer holds at most one reply above
  its 64 KiB high watermark, and each reply gets its own `write_stall_timeout` window. A host call
  stays in flight (and counts toward `max_inflight_host_calls`) until its reply is written.
  3000 concurrent 200 KB replies on macOS: peak transport buffer ~595 MB -> ~0.1 MB, parent max
  RSS ~3.1 GB -> ~0.7 GB, and the burst completes at `write_stall_timeout=1`, where the worker
  used to be killed; 10000 x 20 KB: parent max RSS ~1.8 GB -> ~0.13-0.27 GB. A worker that does
  not drain one reply within the stall window is still killed, however slowly it reads.
- **`Runtime.close()` no longer hangs when a host function returning a stream source is running.**
  `close()` holds the runtime's shutdown lock while it waits for the runtime thread, and converting
  the returned source on that thread checked the same lock, so the host deadlocked (for example
  when an `eval_async` task was cancelled inside `with Runtime()`). The check no longer waits for
  the lock.
- A process no longer intermittently aborts at interpreter exit after using `Runtime`: `close()` cancels stream sources on the calling thread, and background threads stop entering Python once the interpreter exits. `eval_async` started later than that (in an `atexit` handler registered before `import pydeno`, or after `atexit._run_exitfuncs()`) still completes. The stream's `aclose()` runs in the contextvars the source was created in, and its exceptions are not reported as "Task exception was never retrieved".
- **A `JsFunction` or `JsStream` garbage-collected inside a host function no longer hangs the
  process or panics.** Its finalizer then runs on the runtime thread and waited for that same
  thread; the stream finalizer hung forever. On the runtime thread the handle is now released
  without waiting.
- **`load_wasm` on `IsolatedRuntime` / `AsyncIsolatedRuntime`: instances of dropped modules are no
  longer left in the worker.** The ids of dropped modules ride along with the next wasm command;
  a command that failed before it was sent lost them, and two threads sending at once could lose
  ids appended in between. The ids are now taken one at a time and put back if the command fails.
- **`classify_error` gives the new refusals stable kinds.** A stream source whose runtime is closed
  and an unloaded WebAssembly module are `closed`; a stream source from another runtime is
  `invalid_input` (previously all `unknown`). The messages are unchanged.
- Tests: the worker-capacity and pool tests that need the OS sandbox are marked `full_sandbox`,
  so a run on a kernel without Landlock or seccomp deselects them instead of failing, and the
  WebAssembly `max_memory` test no longer depends on the sampled kill landing during the one call.

## 0.8.0 — 2026-10-04

Highlights: one Monty-shaped front door (`Pydeno` / `AsyncPydeno`) as the default path, much faster
start-up and warm calls, a command line and an `llm` plugin, an allow-listed `http_fetch` tool, a
`strict_eval` profile, and a large hardening pass over the worker, the bridge, the limits and the
session pools. See "Upgrading" for the few behaviour changes.

Security note: this release hardens behaviour of 0.7.0. A restored `SessionPool` session now keeps the
tool budget it has already spent, a failing result conversion no longer keeps handles, and several
limit values that were silently accepted are now refused. Upgrading is recommended for hosts that
rely on `max_tool_calls` or on limits for untrusted code.

- Refuse isolated worker startup when the supervisor lacks signal authority, in every sandbox
  mode. `sandbox_status().termination` reports a hardened-child termination probe; refusal is
  classified as non-retryable `sandbox_unavailable` (#71).

- Bound the async parent's queued frame count as well as payload bytes (#65). Empty or tiny
  frames from a compromised worker now trigger backpressure while the consumer is idle.

### Added

- **`pydeno` command** (`[project.scripts]`, same as `python -m pydeno`): evaluates JavaScript from an
  argument, `-c`, `-f FILE` or stdin and prints the result as JSON (`--raw` for plain strings). Runs in
  `IsolatedRuntime(sandbox="require")`; `--timeout` (default 30 s), `--max-memory`, `--sandbox auto`,
  `--no-sandbox` (warns on stderr). Exit codes: 1 JavaScript error, 2 usage, 3 timeout, 4 OS sandbox
  unavailable, 5 other runtime failure, 6 result has no JSON form; the error and its `classify_error`
  kind go to stderr. Input is read up to 16 MiB (bounded, so `-f /dev/zero` cannot fill memory),
  guest output is stripped of control, format and other invisible characters, and integers past
  2^53 - 1 print as JSON strings. See [`docs/guides/cli.md`](docs/guides/cli.md).
- **`llm-pydeno`**, an [`llm`](https://llm.datasette.io/) tool plugin in `integrations/llm-pydeno/`
  (a separate package; `pydeno` gains no dependency): a `PyDeno` toolbox whose `run_javascript` runs
  code in an `AgentSandbox` session that keeps its state between calls and returns the
  `ExecutionResult` fields with output and result caps.
- **`Pydeno` / `AsyncPydeno`: one front door, shaped like Monty.** `with Pydeno() as pool:`,
  `with pool.checkout(limits=...) as session:`, `session.feed_run(code, inputs=, external_lookup=,
  print_callback=)` (the feed's trailing expression is its result; state persists), `feed_start` with a
  `PydenoSnapshot` at every external call (`resume`, `resume_auto`, `dump`), `dump` / `load_session` /
  `load_snapshot` (signed, replayed deterministically on a fresh worker), `worker_pid`, `PydenoLimits`
  (Monty's `ResourceLimits` mapped onto pydeno's limits) and typed errors (`PydenoError`,
  `PydenoRuntimeError`, `PydenoSyntaxError`, `PydenoCrashedError`, `PydenoTimeoutError`, which is a
  `TimeoutError`; `classify_error` sees through them). Secure by default: `sandbox="require"` with no
  silent downgrade, jitless V8, host errors redacted, every limit set, single-use workers from a warm
  `SandboxPool`. `benches_py/alternatives_bench.py pydeno-front` measures it.
- `AgentSandbox(runtime=...)` / `AsyncAgentSandbox(runtime=...)`: run a session on an already-built
  runtime (a pool checkout); its seed (and frozen clock, if any) become the session's.
- Faster sessions: a `Pydeno` worker arrives with the session's setup pre-installed (checkout does no
  worker round trip, about 0.1 ms), and `AgentSandbox.run()` / `execute()` (and `feed_run`) drive the
  worker from the calling thread, which enforces every limit also while a tool runs (tools are answered
  on the session's own threads, never shared with another session). Journals are unchanged: a dump
  from either path replays on the other.
- Session tool threads are capped per pool (`Pydeno(max_tool_threads=128)`, clamped to a process
  ceiling of 512; caps are upper bounds drawn from that ceiling, not reservations, and `Pydeno()` warns
  when the open pools' caps add up to more). A refused call is journaled and charged like a failed
  tool call, fails generically for the guest, is logged once per session for the host, and raises
  `ToolThreadLimitError` (a `PydenoError`) if the feed then fails. A session dropped without `close()`
  gives its threads back. An `AsyncPydenoSession`'s console sink runs on the session's own thread.
- **`strict_eval=True`: no code generation from strings in the guest** (#42). On `IsolatedRuntime`,
  `AsyncIsolatedRuntime`, `SandboxPool` / `AsyncSandboxPool` (a spawn option: fixed per pool, refused
  per checkout), `AgentSandbox` / `AsyncAgentSandbox` and `Pydeno` / `AsyncPydeno`. `eval`, `new
  Function` and the async/generator function constructors throw `EvalError` however the guest reaches
  them; the host's own scripts still run. It appends V8's `--disallow-code-generation-from-strings`
  after the hardening flags, frozen with them. Sessions record it in their journal (only when on, so
  default journals are unchanged) and refuse to load a journal under the other setting. It does not
  cover WebAssembly with `jitless=False`, and it guards trusted code against injected strings rather
  than containing hostile code (a guest can ship its own interpreter); see the isolation guide. Off
  by default.
- `vendor/libs/vega-interpreter-2.3.2.bundle.js` (BSD-3-Clause, 5 KB): Vega's CSP-safe expression
  interpreter, so Vega and Vega-Lite render under `strict_eval=True`. The library tests now also run
  d3, turf and ECharts SSR, and every library under strict eval.
- **Cargo feature `inspector`** (on by default, so the published wheels are unchanged). It gates the DevTools
  inspector server and its network crates (`hyper`, `hyper-util`, `fastwebsockets`, `http`, `http-body-util`,
  tokio's `net`). A `--no-default-features` build keeps `InspectorConfig` as a type, but `Runtime` with an
  inspector configured raises `RuntimeError` ("built without inspector support"). `pydeno._pydeno._INSPECTOR_AVAILABLE`
  reports which build you have. A CI job builds it, runs the isolated-runtime suites against it, and prints the
  size and dependency difference. See `docs/guides/advanced/inspector.md`.

Nothing changes for existing code except `python -m pydeno` (see Changed), the limit fixes
and the red-team restrictions under Security; see
[`docs/guides/upgrading.md`](docs/guides/upgrading.md).

### Security

- **0.8 red team, host boundary and state** (#75 slice C; probes in `scripts/autoresearch/metric_security.py`,
  tests in `tests/test_redteam_boundary.py`, details in `docs/security-report.md`):
  - `SessionPool`: a restored session keeps the tool budget it has already spent, also after its
    journal outgrew `max_journal_bytes` (a journal without state that charges the spent calls is
    stored instead), when calls on one session overlap, and when a release fails (the session then
    stays live until a release succeeds). `get` checks a live session against the stored counter
    and `release` refuses (`StaleJournal`) to store over a newer journal; this catches one pool
    picking up a session another released, not overlapping leases in two pools, so sessions must
    be routed to one pool. `close()` stores every unsaved session (leased ones as after a crash),
    warns about any it cannot store, and wakes waiting `get`s. Ids must be valid UTF-8.
    **Behaviour change.**
  - Agent sessions queue concurrent tool calls past `max_inflight_host_calls - 1` and issue them in
    order; they were refused depending on timing, which the journal did not record.
  - A tool raising a `BaseException` during `AgentSandbox.run()` / `execute()` / `feed_run` ends the run
    like a crash (recorded as lost); the journal stays loadable.
  - Tool names that would replace a guest global (bare-global tools), and names starting with
    `__pydeno` / `__host_op`, are refused. **Behaviour change.**
  - `SessionPool.drop()` holds the session's place while it works and wins against an overlapping
    `get` or `release`; `pool.session()` releases only its own lease; journal associated data may be
    up to 4096 bytes, so every valid pool id persists.
  - Front door: the syntax check after a failed feed uses captured intrinsics; `dump` / `load_session` /
    `load_snapshot` take `associated_data=`; a refused answer no longer uses up a snapshot; `feed_start`
    surfaces snapshots only for the feed's declared functions.
  - Captured console output, error messages and the default printer use one filter with the CLI's
    set: control characters, bidirectional controls, line separators and invisible format characters
    (zero-width joiners and spaces, variation selectors, soft hyphen, BOM, Unicode tags, ...) become
    `?`. **Behaviour change** for output that contained them.

- **Large indexed values are checked before they are expanded.** A typed array other than
  `Uint8Array`, or a boxed `String` (also behind a Proxy), returned as a result or stream chunk or
  passed to a host function, was expanded into one key per element in a single native call before the
  serialization budget (or the host-call argument cap) was checked. A 16 MB value took hundreds of
  megabytes and ran several times past `timeout=`; a Proxy around one, passed to a host function in
  plain `Runtime`, could run V8 out of memory. Such values are now refused at once; small ones convert
  as before. Upgrade note: whatever `max_serialization_bytes` is, a typed array or `String` object of
  more than 1,048,576 elements is refused as a result (return a `Uint8Array` over its buffer instead).
- **A Proxy crosses the boundary as its target, and no trap runs.** In a result, stream chunk or
  host-function argument, a Proxy's traps ran during conversion, so guest code could act mid-conversion:
  an `ownKeys` trap could grow a resizable buffer after its size was checked (16 million keys listed
  past the budget), a Proxy hid an Array from the metered array path, and `ownKeys() { return [] }`
  made the engine walk the whole target for free at every reference. A Proxy is now unwrapped natively
  to its innermost target, which is converted instead; a revoked Proxy or a chain of more than 64 is
  refused. Upgrade note: a Proxy whose traps synthesise values now arrives as its target's data, and a
  Proxy around an Array arrives as a list (it used to be a dict of indices).
- **`v8_flags` that cannot take effect are refused.** deno_core's start-up switches on `Temporal`,
  `Float16Array`, explicit resource management, source-phase and deferred imports and the native
  `queueMicrotask` after the worker's flags, so a flag such as `--no-harmony-temporal` was undone
  while `IsolatedRuntime.v8_flags` listed it. `IsolatedRuntime` now raises `ValueError` before it
  starts a worker. Upgrade note: code that passed one of these flags gets an error instead of a silent
  no-op; remove the flag, or use the opt-in `v8_flags=["--no-js-shipping"]`, which does switch these
  features off (together with the other newest ones; see the isolation guide).
- Numbers outside the 64-bit integer range now come back as floats (`2**63` used to return
  `2**63 - 1`).
- **Limit values are validated.** Durations (`request_timeout`, `max_host_wait`, `write_stall_timeout`,
  `RuntimeConfig.timeout`, per-call `timeout=`, `AgentSandbox` `timeout`/`max_pause`, `SessionPool`
  `ttl`/`counter_ttl`/`eviction_interval`/`idle_timeout`, `PydenoLimits` seconds) must be `None` or a
  finite number of seconds above zero and at most about 70 years (`threading.TIMEOUT_MAX / 4`);
  `timeout_grace`, `SessionPool(acquire_timeout=)` and `get(timeout=)` may be 0. Counts (`max_memory`,
  `max_host_calls`, `max_inflight_host_calls`, `max_tool_calls`, `max_journal_bytes`, output caps, pool
  sizes, `max_sessions`, `max_per_owner`, `max_suspensions`, `max_tool_threads`) must be an int between
  their minimum and 2**53 - 1 (`max_memory`: between 1 and 2**53 - 1). NaN or infinity was accepted
  before and turned the limit off without saying so (every comparison with NaN is false); Python's
  `json` parses both, so they could come from a config file. Any real number (`Fraction`, `Decimal`,
  numpy floats) works as seconds and any integer-like (numpy ints) as a count. Except for Rust-converted
  `RuntimeConfig.timeout`, errors are uniform: a wrong type raises `TypeError`, a bad value `ValueError`.
- **Console output pauses the hard deadline only within an allowance.** The deadline pauses while the
  host runs a tool, and console calls were treated the same way, so time spent handling a flood of
  `console.*` output stretched a run (or a `Pydeno` feed) past its deadline, up to `max_host_wait`
  (600 s by default). Console time now pauses the deadline for at most one hard deadline in total per
  command: one slow write does not end a run, and a flood can at most double it. Console time while a
  tool call is in flight is covered by that call's pause. Console calls are no longer refused by
  `max_inflight_host_calls` (they are synchronous, never in flight) and still count toward
  `max_host_calls`.
- **`Pydeno`'s default printer is capped.** Without a `print_callback`, a feed's console output goes to
  the host's stdout/stderr; it now stops after 1 MiB per feed with one `[truncated]` line (it was
  unbounded: about 150 MB in 2 s measured). An explicit `print_callback` gets everything.
- **A guest can no longer make a later bind silently inert.** `bind_object` (and so `ToolBridge.attach`)
  installed onto whatever `globalThis[name]` already was and walked its assignment list with `for...of`;
  `bind_function` assigned `globalThis.name = ...` in sloppy mode. Guest code that ran earlier could plant a
  Proxy namespace that swallowed `defineProperty`, an accessor returning a throwaway object, a read-only or
  setter global, or a replaced `Array.prototype[Symbol.iterator]`, and the bind then installed nothing while
  the host still received and exposed the op tokens. The bind now defines own data properties only, runs no
  guest-replaceable built-in, and raises `Cannot bind '<name>': ...` (exposing no token) when the existing
  global is an accessor, read-only, a Proxy, a function, a class instance, or frozen. An existing plain
  object is still extended, a writable global (including a `var`) is still replaced, and an inherited
  property is shadowed rather than written through. A built-in object (`Object.prototype`, `Math`,
  `Array.prototype`, `%IteratorPrototype%`...) is refused as a namespace too, and the refusal's error carries a
  pre-rendered stack so a guest `Error.prepareStackTrace` does not run inside the host's bind.
- `ToolBridge.attach(..., namespace=None)` is all-or-nothing: when a later tool is refused, the tools this
  call already bound are revoked before the error propagates.
- The Python-stream helper `__pydeno_from_py_stream` is fixed in place like the other bridge globals (it was
  defined after them and stayed writable), and the bridge builds streams with a captured `ReadableStream` and
  a prototype-less source. A guest could otherwise run code inside a host `bind_object` (or any hand-over of a
  Python stream), see every stream id and substitute its own value.
- The `ReadableStream` polyfill keeps its state in a private WeakMap, so setters a guest plants on
  `ReadableStream.prototype` no longer run (and receive the stream's source) when the bridge creates a stream.
  Recognising a guest's `ReadableStream` result no longer uses `instanceof globalThis.ReadableStream` (a guest
  `Symbol.hasInstance` or a replaced global could turn every object result into a stream); it checks the
  prototype chain against the prototype captured at startup.
- A refused bind's error has own `name`, `message`, `constructor` (`null`), `cause`, `stack` and
  `Symbol.for("errorAdditionalPropertyKeys")` (everything deno_core reads when it converts the error, including
  the `constructor` walk of its AggregateError check), so reading it runs no getter from a prototype or
  constructor.
  No deadline covers a bind, so a looping getter there used to block `bind_object` indefinitely (and an
  `IsolatedRuntime` bind until the worker's hard deadline killed it). A non-extensible global object is
  refused with the same clear error, and the handlers a refused bind registered are dropped instead of kept for
  the runtime's lifetime.
- The built-in set behind the namespace check is collected from the standard global names only, so objects a
  host snapshot puts on the global object are bindable again (this was a regression in the previous change),
  and now also covers `CallSite.prototype`, `%SegmentsPrototype%`, the iterator-helper prototypes and the
  `ReadableStream` polyfill.
- The bridge rebuilds host results without `Array.prototype.map`, `Object.entries`, `for...of`,
  `Promise.prototype.then` or the global `Array.isArray`/`Date`/`Set`/`BigInt`. Arrays the bridge builds
  (host results, copied arguments, and the arrays the Rust converter creates) define their elements as own
  properties, so an index setter on `Array.prototype` neither sees nor replaces them.
- **`max_buffer_bytes` now covers resizable buffers.** V8 allocates a resizable `ArrayBuffer`
  (`new ArrayBuffer(n, {maxByteLength})`), a growable `SharedArrayBuffer`, and the copy `transfer()` makes of
  a resizable buffer from its own page allocator, not the embedder's, so the cap never saw them: a guest could
  commit gigabytes under a cap of a few hundred megabytes, and filling the buffer was a `max_memory` kill
  instead of the promised catchable `RangeError`. The bridge now charges their committed bytes to the same
  budget at construction, `resize`/`grow` and `transfer*`, through one op that keys each buffer by a private
  symbol and holds it weakly; when a charge would exceed the cap the op forces a GC, gives collected buffers'
  bytes back and retries, so churn through short-lived resizable buffers does not exhaust the budget; the
  allocator does the same cheap sweep before refusing a fixed-length buffer, and the bookkeeping is bounded
  (it is swept as it doubles and capped). Resizable buffers are charged in whole OS pages, which is what V8
  commits for them (a one-byte resizable buffer costs a page). `instanceof`, subclassing, `Symbol.species`
  and the prototype objects are unchanged.
- **A refused allocation no longer leaves the runtime terminated.** With `max_heap_size` set, an
  `ArrayBuffer` the buffer cap refused was reported to the guest as a `RangeError` but also marked the
  runtime as over its heap limit, so every later command failed with `RuntimeTerminated`. Only a JS heap
  that really is at its limit terminates now, and a refusal no longer stays flagged after V8's final retry
  (a later real heap overflow used to be taken for a refusal, and V8 then aborted the process). `WebAssembly.Memory`
  remains a sink the cap cannot see (`IsolatedRuntime` has no WebAssembly under `--jitless`).
- **A guest could kill an `IsolatedRuntime` worker with one large `console.log`** when the host set
  `enable_console=True`: the engine echoed console output to the worker's stdout, which is the parent's
  stderr capture file under `RLIMIT_FSIZE` (1 MiB), and deno_core's `op_print` unwraps the flush of the
  failed write, so the worker aborted (SIGABRT). The worker no longer lets the engine echo console output;
  `on_console` and `capture_console` are unaffected.
- **Captured console output carries no control or escape characters.** `execute()` (and the agent layer's
  `ExecutionResult`) cleaned error text but returned `stdout`/`stderr` with raw ANSI/C1 sequences; they now
  follow the same rule (newlines and tabs stay), and both also drop the Unicode bidirectional controls that
  reorder a line and the invisible format characters (zero-width joiners and spaces, the BOM, soft hyphen,
  line and paragraph separators, variation selectors, TAG characters) that carry text a reader never sees
  but a model does. Emoji sequences lose their joiners and skin-tone modifiers and render as their parts.
  An `on_console` callback still receives the guest's text raw; sanitise it before printing.
- **`http_fetch` caps host name lookups in flight per process** (`DNS_MAX_PENDING`, 64 by default,
  running or waiting for one of the `DNS_THREADS`; both read when the first lookup creates the
  pool). A running lookup cannot be stopped, so each call that timed out used to leave its lookup
  queued behind stalled ones, and later calls from any session in the process waited behind all of
  them. A lookup still waiting for a thread is now cancelled when its caller gives up, a running one
  counts until it finishes, and past the cap a call fails at once with `HttpFetchFailed` ("too many
  host name lookups are in progress"). A forked child starts with a fresh pool.
- **A result its caller never receives no longer leaves function or stream handles behind.**
  Converting a value registers each function and `ReadableStream` in it. When the caller got an
  error instead (a later part failed to convert, on the runtime thread or in Python, e.g. a `Map`,
  a throwing getter, a cycle, the size limit, a date past year 9999; or the deadline passed while
  the value was converted) or had stopped waiting (a cancelled `eval_async`), those registrations
  stayed for the life of the runtime, so repeating the call grew memory without bound. They are now
  released.

### Fixed

- macOS on Intel (x86_64): sandboxed workers no longer abort at start-up. V8 on x86_64 calls `uname()`
  while it starts, which reads the kernel version through one `sysctl` name the Seatbelt profile
  denied; the profile now allows that read-only name (`kern.osrelease`, for example "24.6.0"). 0.7.0
  shows the same failure on Intel Macs. Apple-silicon Macs were not affected.
- `AsyncIsolatedRuntime` (and `AsyncSandboxPool`) start on Python 3.10 again: they used
  `asyncio.timeout` and `create_task(context=...)`, which need Python 3.11.
- `IsolatedRuntime` and `AsyncIsolatedRuntime` raise `ValueError` for a `max_memory` (or a
  `RuntimeConfig` limit) above 2^53 - 1. Such a value used to reach the worker as a tagged object
  and fail at startup as `WorkerCrashed` ("argument 'max_buffer_bytes': 'dict' object cannot be
  interpreted as an integer"); `max_memory=2**62` was enough, since it derives
  `max_buffer_bytes = 2**60`.
- Timeouts are enforced when guest code customises `Error.prototype` or `Error`: the watchdog keeps stopping
  the isolate until a timed-out call has returned, and a call whose deadline fired reports `RuntimeTimeout`
  even when the guest's error was still being read at that point.
- The runtime stays usable after a module evaluation times out (or waits on a top-level `await` that never
  settles): later commands, timeouts and `TerminationHandle.terminate()` are served as usual, and the idle
  runtime thread does not spin. Known limit: after that, a *new* module stuck on a top-level `await` is no
  longer reported at once; it waits for its timeout (forever without one). See the modules guide.
- After a deadline has fired, a later `TerminationHandle.terminate()` reports its own reason instead of the
  earlier timeout's, also when it lands while the timed-out call is still returning.
- Evaluating a module again after its first evaluation timed out or was terminated explains that, instead
  of failing with `Uncaught null`.

### Changed

- **`python -m pydeno` now runs code in the sandboxed worker**, not the in-process `Runtime`, and a
  positional argument is JavaScript, not a file name (use `-f FILE`). Results print as JSON. See
  [`docs/guides/upgrading.md`](docs/guides/upgrading.md).
- Cold start of the isolation worker about 15 ms shorter on macOS arm64 (interleaved A/B, median of 120 cold
  creations, release build): the Seatbelt profile is compiled on a background thread while the worker imports
  and only applied afterwards (`sandbox_compile_string` + `sandbox_apply`, 0.1 ms instead of `sandbox_init`'s
  ~8 ms; same profile, same self-test, falls back to `sandbox_init`); the worker never loads `ssl` (asyncio
  imports it only optionally, and the worker has no network) and, on Python 3.14, never imports `typing`.
- Lower warm-call overhead of `IsolatedRuntime` (#47): a warm `eval("1 + 1")` went from about 112 to 67 µs
  (interleaved A/B, 30 rounds, medians; debug build of the extension on a loaded macOS arm64 machine, so
  release numbers will differ). Where it came from:
  - Reading the worker's CPU time on macOS no longer opens libSystem through a fresh `ctypes.CDLL` per call
    (about 30 µs, done twice per command); `proc_pidinfo` and the timebase are resolved once, and
    `_sandbox.usage(pid)` returns memory, CPU time and thread count from one kernel read (about 2 µs).
  - A command's CPU baseline is the latest cached reading (the previous command's final check, or the idle
    watchdog's, re-read if older than 0.5 s) instead of a fresh one, as `AsyncIsolatedRuntime` already did.
    CPU time only grows, so an older baseline can only charge a command more, never less. The end-of-command
    check is one reading for the memory ceiling, the thread cap and the idle baseline.
  - The worker reads the next command on its main thread instead of handing it over from a reader thread.
    The helper thread reads only while a command waits on host calls; a parent killed while a command runs
    without one is caught by the worker's watchdog thread (it now always runs, and exits the worker when its
    parent pid changes).
  No limit, wire check or sandbox requirement changed.
- `bind_function(name, ...)` now defines exactly the global property `name`. A dotted name such as `"a.b"`
  used to be spliced into a script and assign `globalThis.a.b`; it now defines a property literally named
  `"a.b"`. Use `bind_object` for a namespace.

## 0.7.0 — 2026-10-04

Async, results, diagnostics. See [`docs/guides/upgrading.md`](docs/guides/upgrading.md) for what can change
behaviour you have today, and [`docs/roadmap.md`](docs/roadmap.md) for where this is going.

### Added

- **`SandboxPool`** and **`AsyncSandboxPool`**: isolated runtimes started ahead of time and handed out once.
  `checkout()` returns a runtime whose worker has already passed its handshake and sandbox self-test in about
  0.04 ms (a cold `IsolatedRuntime` is about 53 ms). A checked-out runtime is never returned to the pool;
  replacements start in the background; an empty pool falls back to a cold start, never an error. Options the
  worker receives at start-up are fixed per pool; parent-side ones (`SandboxPool.SESSION_OPTIONS`) can be set
  per checkout. `benches_py/alternatives_bench.py pydeno-pool` measures it.
- **`AsyncIsolatedRuntime`**: an asyncio-native isolated runtime. Pipes on the event loop, one shared
  supervisor task per loop, no thread per runtime; cancelling a call kills the worker. On macOS (1 to 64
  runtimes) it was 1.4 to 2 times faster and the worst event-loop stall fell from 116 to 335 ms to 1 to 12 ms.
- **`AsyncAgentSandbox`** and **`SessionPool`**: async agent sessions, and a pool with a pluggable journal
  store, TTL, per-owner cap, LRU and a rollback counter (a stale journal is refused). `InMemoryJournalStore`
  is included; a Redis-protocol store is shown in the docs.
- **`ExecutionResult`** from `AgentSandbox.execute()` and `IsolatedRuntime.execute()`: status, stdout, stderr,
  result, error, error type, with ordered console capture capped by `max_output_bytes` and a result cap that
  fails the run with `ResultTooLarge` while the session stays usable.
- **Crash-safe journals**: `dump()` after a crash, timeout or kill returns the last good journal plus a `lost`
  record; `load()` charges the lost run's tool calls, so a crash cannot refund a tool budget.
- **JSON-Schema tools and a lazy tool catalog** (`SchemaTool`, `tools_catalog=`): only `search_tools` and
  `describe_tool` are declared up front, the declared surface is constant-size, and an undiscovered tool is
  refused with a typed error.
- **`sandbox_status()`**, **`classify_error()`** (25 stable error kinds, only worker crashes are retryable) and
  **`check_source()`** (an advisory pre-check, never a security boundary).
- Docs: upgrade guide, error kinds, async guides, roadmap to 1.0, pinned size and SHA-256 for every vendored bundle.
- A Platforms CI workflow: native Ubuntu (x86_64 and arm64), macOS (arm64 and Intel) and Windows, Python 3.10 to 3.14.

### Changed

- Faster cold start of the isolation worker (about 59 to 55 ms on macOS arm64): the worker runs with `-S`
  (no `site`, so no `.pth` file runs in it) and imports `pydeno` from the parent's own package directory, so
  parent and worker always run the same code; the sandbox module no longer imports `ctypes.util` and
  `platform` (`sandbox_init` and `proc_pidinfo` are looked up in the already loaded libSystem,
  `os.uname()` replaces `platform.machine()`). A worker for a custom `python=` is started as before.
- A JS `Map`, `WeakMap`, `WeakSet` or `Error` result now raises instead of becoming an empty dict.
- `dump()` after a crash returns the last good journal instead of raising.
- The worker's seccomp filter denies `memfd_create` (memory the RSS poll could not see).
- The sandbox self-test refuses to run in an orphaned worker.

### Fixed

- A guest that caught a public pydeno error (wrong arity, unknown catalog tool) made its own session
  unrestorable (`ReplayDivergence` on `load()`); replay no longer redacts a recorded error twice.
- A tool call buffered from a worker that had already died could still run its tool.
- Flaky memory-limit and loop-stall tests.

## 0.6.1 — 2026-10-04

Hotfix.

### Fixed

- **macOS: the sandboxed worker aborted at start on Python 3.10, 3.11 and 3.12** (`LowLevelAlloc
  arithmetic overflow`, SIGABRT). V8's allocator reads the page size through a sysctl, which the
  Seatbelt profile denied; Python 3.13+ happens to have read it already. The profile now allows exactly one
  read-only name, `hw.pagesize_compat`. Nothing else about the sandbox changes.
- When the idle watchdog killed a worker for exceeding `max_memory` or the thread cap, the caller
  could see a bare `killed by SIGKILL` instead of the reason. The reason is now kept.

## 0.6.0 — 2026-10-03

Sandbox hardening round 2 (independent review by three models and Copilot, plus prior-art research),
agent sessions, and a pydantic-ai integration. See [`docs/security-report.md`](docs/security-report.md)
for every finding and its status.

### Changed (behaviour you may notice)

- **`redact_host_errors` now defaults to `True`.** A host function's exception text no longer reaches
  the guest unless you opt out (`redact_host_errors=False`).
- **`max_buffer_bytes` defaults to `max_memory // 4`** when you do not set it, so a typed-array bomb is a
  catchable `RangeError` instead of killing the worker.
- **Signed snapshots are bound to the pydeno release** that made them (format `pydeno-snap2`). A snapshot
  signed by another release is refused before V8 sees it; sign it again with the release you run.
- **`RuntimeConfig(snapshot=...)` is refused by `IsolatedRuntime`** instead of being silently dropped
  (which also dropped its bootstrap).
- Workers get two more V8 flags: a linear-time regex fallback and `--freeze-flags-after-init`.
- `sandbox="auto"` emits a `RuntimeWarning` when the platform's full set of layers did not apply.
- Bind names must be plain identifiers; `IsolatedRuntime` checks them.

### Security

- **macOS:** a sandboxed worker could read its parent's argv **and environment** (and the machine's hardware
  ID, other processes' details and host statistics). The profile no longer allows `sysctl-read` and denies
  the process-info, IOKit, hardware-ID, host-statistics and `F_GETPATH` routes.
- **Startup self-test:** the worker tries the forbidden operations before any guest code exists and
  refuses to start if one works.
- **Bridge:** a guest that replaced `Date.prototype.valueOf`, `Array.prototype.map`, `Object.entries`
  and similar could make the bridge hand a Symbol to the Rust converter, which aborts: a lost worker, or
  a dead host process in a plain `Runtime`. Intrinsics are captured before guest code runs, the bridge is
  strict mode, and host-call arguments are capped (1M values, depth 128) before they are copied.
- **Linux seccomp:** stream-only `socketpair`; `prctl` allow-list; no executable mappings when jitless;
  no `sysinfo`/`getpriority`/`ioprio_get`; the socket ioctl block and `F_SETPIPE_SZ` denied;
  `get_robust_list`/`getpgid`/`getsid` only on ourselves; not dumpable; `RLIMIT_RTPRIO`/`NICE` 0. `uname`
  deliberately stays allowed: V8's x86_64 build calls it while starting.
- A worker with more than 64 threads is killed; memory and threads are supervised while a host function
  is running; the in-flight call cap is runtime-wide.
- A reused or mismatched capability token from the worker ends the session; revoking drops the host
  handler first; resolver, loader and console handlers validate what the worker sends them.
- Forked children forget the parent's workers and never touch them; a command waits for the runtime
  at most its own deadline; `eval_async` uses a thread of its own.
- A `BigInt` result past 4300 digits no longer ends the session; `Temporal.Now` follows the frozen
  clock; a guest can no longer kill the worker's reply-reading thread by not awaiting an async host call.

### Added

- **`AgentSandbox`** (`pydeno.AgentSandbox`): an AI-agent layer on `IsolatedRuntime`. State persists across
  runs; `start()`/`resume()` pause at every tool call (approval flows); a signed deterministic-replay
  journal (`dump()`/`load()`); `describe_tools()` and `typescript_stubs()` generate the prompt and `.d.ts`.
  See `docs/guides/agent-sessions.md`.
- **pydantic-ai integration** (`pydeno.integrations.pydantic_ai`): `JSCodeMode`, the JavaScript
  counterpart of the Monty-based code mode. See `docs/guides/pydantic-ai.md`.
- **Examples:** Monty prepares data and pydeno builds the result: three.js terrain, orbits and a city
  sun analysis; a d3 network; an ECharts dashboard; turf geospatial; a SQL question to a Vega-Lite chart;
  a spreadsheet to a PowerPoint deck. Vendored d3, ECharts and turf bundles (SHA-256 pinned).
- `docs/security-report.md`; `security.yml` (cargo-deny, cargo-audit, pip-audit, OSV).

## 0.5.0 — 2026-10-03

No breaking changes: every new limit is opt-in and `Runtime` is unchanged.

### Changed

- **`deno_core` 0.409 -> 0.412** (still V8 150.4, the newest V8 any `deno_core` supports; see
  `scripts/check_engine.py`). The debug-build overflow workaround in `Cargo.toml` stays: 0.412 still has
  the `source_map` subtraction bug (now at line 221), pinned by a test.
- **Lighter**: the release extension is stripped and link-time optimised (macOS: 60 MB -> 43 MB;
  nearly all of the rest is V8 and its built-in Intl data). `import pydeno` no longer loads the
  parent-side machinery (`asyncio`, `subprocess`, `tempfile`, the tool bridge, snapshot auth):
  those exports are lazy, so a plain `Runtime` user's import dropped from ~51 ms to ~19 ms.
- **Fused native JSON** for the isolation wire: results and call arguments are parsed and decoded
  (and encoded and written) in one native pass, with the GIL released while parsing. A 2 MB frame
  takes ~18 ms each way instead of ~60-80 ms; moving 50k small objects costs ~35 ms of overhead
  (it was ~144 ms). The Python codec remains the reference and fallback, and the two are tested
  against each other, including lone surrogates (valid in JavaScript, refused by `serde_json`).
  The native parser is deliberately stricter than `json.loads` about encodings (no BOM, no
  UTF-16/32, no raw surrogates).
- `scripts/gen_syscall_tables.py` regenerates `tests/data/syscalls.json` from a pinned Linux tag
  (v7.0), so the kernel tables the seccomp numbers are checked against are reproducible.
- **Native wire codec** (`src/runtime/wire.rs`): `IsolatedRuntime` encodes and decodes values in
  Rust. Structured results cost far less to move across the boundary (50k small objects: overhead
  144 ms -> 59 ms). The Python codec stays as the reference and fallback, and
  `tests/test_wire_native.py` checks the two agree on results *and* error messages, including on
  hostile input and with hypothesis-generated trees.

### Fixed

- Dropping a `SnapshotBuilder` without calling `build()` no longer leaks its isolate
  (it is now released on drop).

### Added

- **`pydeno.IsolatedRuntime`**: the same guest in a supervised worker
  *process*, so guest code can no longer abort or wedge the host, and an
  escaped V8 lands somewhere that cannot do much. Measured against
  pydantic/monty's security suite, an in-process `Runtime` is taken down by
  `new Array(2**32-1).fill(0)` and `'a'.repeat(2**28).match(/a/g)` (V8 fatal
  OOM) and ignores `timeout=` for `sort`/`reverse`/`map` on a sparse array (an
  uninterruptible native builtin). Under `IsolatedRuntime` each ends in a
  catchable error. Layers:
  - Copied from Monty's design: length-prefixed frames with a hard cap, a
    bounded decoder that treats the worker as untrusted (fuzzed), an empty
    worker environment, parent-side hard-kill deadlines that pause during host
    callbacks, crash detection, and a `max_host_calls` budget.
  - **OS sandbox** applied in the worker before the isolate exists: macOS
    Seatbelt (deny by default); Linux Landlock plus a seccomp-bpf filter that
    denies new processes, the network, mounts, kernel interfaces, IPC with the
    host user's other processes, file-metadata changes, identity changes and
    acting on any other pid. `sandbox="auto" | "require" | "off"`; read
    `.sandbox` for what is active. Found by a systematic assume-breach sweep
    (`scripts/redteam_syscalls.py`); the filter denies well over a hundred
    syscalls and every one is checked against the kernel's own tables.
  - **No privileges**: a worker started as root drops to `nobody` with an empty
    capability set and bounding set.
  - **Empty root** (Linux, where unprivileged user namespaces are allowed): the
    worker gets private mount, network, IPC and UTS namespaces and an empty
    tmpfs root, so it cannot even tell which host paths exist (`sandbox_extras`,
    `empty_root=False` to skip). Anything newer than the reviewed syscall range
    is denied by default (`ENOSYS`).
  - **`--jitless` V8** by default (`jitless=False` to allow the JIT and
    WebAssembly): no JIT compiler, the source of most V8 exploits.
  - **`max_memory`** (default 1 GiB) enforced by the worker itself every
    ~20ms (dedicated exit code, like Monty's allocator) and by the parent every
    ~50ms; covers WebAssembly and resizable buffers, which no in-process limit
    can. A 60 s hard deadline is also on by default.
  - **Smaller syscall surface** (second pass): NUMA policy (`mbind`, `set_mempolicy`),
    filesystem mutation (`mkdirat`, `unlinkat`, `renameat`, `linkat`, `symlinkat`, `mknodat`),
    file-to-file copies (`splice`, `tee`, `sendfile`, `copy_file_range`), POSIX timers, protection
    keys and re-entering Landlock/seccomp are denied, including x86_64's older path-based spellings
    (`mkdir`, `unlink`, `rename`, ...) that aarch64 never had. Checked against pptxgenjs, three.js,
    Vega-Lite and dagre bundles under the full Linux sandbox.
  - **`prewarm`** (default on): one ready spare worker is kept so the next runtime starts in
    about 15-45 ms instead of about 100 ms (macOS; the low end when the spare is used soon after
    it started). The guest also loses `SharedArrayBuffer`, `Atomics`,
    `WeakRef` and `FinalizationRegistry` (shared-memory timers, observable GC).
  - **`pydeno.WEB_POLYFILLS`**: opt-in, pure-JS browser basics (virtual-time timers, `TextEncoder`,
    `btoa`, `Blob`, `EventTarget`) so real libraries run in the sandbox. Tested with pptxgenjs,
    three.js, Vega-Lite, dagre (`vendor/libs/`), identical to `Runtime`.
  - **Hostile-peer hardening** (from an independent review): the host waits
    on a worker with bounded writes (`write_stall_timeout`, default 10 s), so a
    worker that stops reading cannot freeze the caller; host-callback time no
    longer pauses the deadline forever (`max_host_wait`, default 600 s, plus a
    CPU-time cap of twice the deadline, and `max_inflight_host_calls`, default
    64); hash-flood sets and dicts, ints past 2^53 and oversized argument lists
    are refused by the decoder; an idle worker that grows or spins is killed;
    `fcntl`/`ioctl` signal-owner commands, path `truncate` and process-group or
    user selectors on `setpriority`/`ioprio_set` are denied;
    `sandbox="require"` fails unless *every* layer of the platform applied;
    `redact_host_errors=True` hides host exception text from the guest.
  - **`clock=` and `random_seed=`**: a frozen guest clock (`Date`, `Intl`) and a
    seeded `Math.random`, Monty's `os_policy` for time and entropy.
  - The worker runs with `TZ=UTC`, no core dumps and bounded file size, so the
    host timezone and locale no longer reach the guest.
  Supports `eval`, `eval_async`, `bind_function`, `bind_object`, `revoke_op`,
  `add_static_module`, `set_module_resolver`, `set_module_loader`,
  `eval_module`, `eval_module_async`, `on_console` and `ToolBridge`; streams,
  snapshots, the inspector and function handles are not supported yet.
  Verified on macOS arm64 and Linux (Debian, Ubuntu 22.04/24.04, Fedora,
  AlmaLinux, Amazon Linux; Python 3.10-3.14); CI runs the matrix on x86_64 and
  aarch64 and under simulated kernels without Landlock or seccomp. See
  `docs/guides/advanced/isolation.md`.
- **`pydeno.sign_snapshot` / `verify_snapshot`**: HMAC-authenticate a V8
  snapshot before loading it. V8 deserialises snapshot bytes without validating
  them, so a tampered snapshot is a crash or worse; `verify_snapshot` raises
  `SnapshotAuthenticationError` and never returns bytes that failed the check.
- `SECURITY.md`: how to report a vulnerability, what is in scope, the threat
  model, and the hardening to turn on.
- `pydeno._pydeno._set_v8_flags` (private): process-global V8 flags, refused
  once any `Runtime` exists. Used by the isolated worker.
- **`RuntimeConfig(max_buffer_bytes=...)`** caps live `ArrayBuffer` /
  `SharedArrayBuffer` bytes with a custom V8 allocator
  (`src/runtime/capped_allocator.rs`). V8 does not count that storage against
  `max_heap_size`, so a guest under `max_heap_size=64MB` could allocate
  gigabytes. An over-budget allocation throws a catchable `RangeError`. It is
  opt-in and independent of `max_heap_size`, whose meaning is unchanged.
  `WebAssembly.Memory` and resizable-buffer growth are not covered (V8 offers
  only process-global flags for them); use `IsolatedRuntime(max_memory=...)`.
- `tests/test_monty_parity_security.py` ports the attack categories from
  Monty's security suite. Its strict-`xfail` cases are the sinks an in-process
  `Runtime` cannot contain; `tests/test_isolated_runtime.py` shows each is
  contained by `IsolatedRuntime`.

## 0.4.5 — 2026-09-29

- Publish a manylinux_2_28 `aarch64` wheel, built and tested on a native ARM runner, so Linux ARM installs no longer fall back to the sdist (which needs Rust and a compiler).

## 0.4.4 — 2026-09-28

- Enable fat link-time optimization, a single codegen unit and symbol stripping for smaller release wheels.
- Limit Hyper dependencies to the HTTP/1 inspector server and Tokio adapter, removing unused HTTP/2 dependencies.
- Preserve panic unwinding and the 0.4.3 runtime fixes.

## 0.4.3 — 2026-09-27

- Propagate Python conversion errors to async eval, module and function callers instead of leaving their futures pending.
- Release the GIL during blocking runtime control operations, object binding and stream cleanup so Python callbacks cannot deadlock the caller.
- Preserve `__proto__` dictionary keys as own data properties when sending Python objects into V8.
- Track ToolBridge capabilities per runtime with weak references; detaching one runtime no longer loses revocation tokens for another.
- Reject trailing newlines in tool names and timeouts that cannot fit the command format or platform clock.
- Snapshot binding dictionary entries before releasing the GIL so concurrent mutation cannot panic.
- Replace timing-sensitive concurrency assertions with barrier checks and repair documentation references.
- Use the installed Linux wheel's interpreter for CI report checks and align local Ruff with CI.

## 0.4.2 — internal cleanup, no API changes

An internal refactor with no API or behaviour changes. The Python API, the
`_pydeno` stubs, error messages, and runtime semantics are all unchanged, and
the full test suite (589 tests) passes as it did on 0.4.1. Rust source is down
from 11,749 to 9,210 lines, and the Python package from 713 to 645.

### Changed (internal)

- **`src/runtime/runner.rs` (3,904 lines) is now a `runner/` module** split by
  responsibility: `termination.rs` (`TerminationController`, the deadline
  watchdog), `dispatcher.rs` (event loop, command handling), `jobs.rs` (async
  job state machines), `core.rs` (`RuntimeCoreState`, sync entry points),
  `convert.rs` (V8 ↔ `JSValue`), and `mod.rs` (commands, thread spawn).
  - Command handling shares one set of admission helpers instead of ~20
    copies of the terminated/inspector checks.
  - Sync and async function calls share one call path and error type.
  - A single `Converter` replaces the 4–6 arguments that were threaded
    through every value conversion.
  - Sync and async module evaluation share specifier parsing, loading and
    namespace extraction.
- **`src/runtime/python/runtime.rs`** is split into `runtime.rs`,
  `function.rs` and `stream.rs`. The stats pyclasses are generated from their
  source structs.
- **`handle.rs`** sends every command through generic request/response
  helpers.
- **`ops.rs`, `config.rs`, `loader.rs`, `error.rs`** share their OpState
  lookup, handler call, validation, and loader-call helpers.
- **`js_value.rs`, `stream.rs`, `inspector.rs`, `conversion.rs`, `stats.rs`**:
  repeated match arms and helpers are deduplicated, and call counters are
  indexed by call kind.
- **`python/pydeno`**: `ToolBridge` internals, default-runtime helpers, the
  CLI, and the awaitable adapter are simplified.
- Over-long internal comments are condensed to the reasoning that isn't
  obvious from the code. Python-visible docstrings and `SAFETY` comments are
  kept verbatim.

## 0.4.1

Follow-ups to the 0.4.0 review (`docs/reviews/2026-09-22-0.4.0-review.md`).
No breaking API changes, but three changes a caller can observe:

- A timeout's exception is now `RuntimeTimeout` rather than exactly
  `RuntimeError`, so `except RuntimeError` is unaffected but a check such as
  `type(exc) is RuntimeError` (or matching on `repr(exc)`) is not.
- A timed-out JS *function call* (`fn(...)`, `fn.call_async(...)`) used to
  raise `JavaScriptError: Uncaught null`, which is not a `RuntimeError` at
  all. It now raises `RuntimeTimeout`, so an `except JavaScriptError` that
  happened to catch it no longer does.
- Function calls that used to hang past their `timeout=` (see *Fixed*) now
  raise at the deadline, and an async function call's deadline can now stop
  an unrelated inline sync call (see *Documented*).

### Added

- **`pydeno.RuntimeTimeout`**, raised instead of a bare `RuntimeError` when an
  operation exceeds its `timeout`. Until now a timeout was indistinguishable
  from an internal failure except by matching `"timed out"` in the message,
  which is not an API — rewording the message would have broken every caller
  keying off it. `RuntimeTimeout` subclasses `RuntimeError`, so existing
  `except RuntimeError` handlers keep catching timeouts unchanged:

  ```python
  from pydeno import Runtime, RuntimeConfig, RuntimeTimeout

  with Runtime(RuntimeConfig(timeout=1.0)) as rt:
      try:
          rt.eval("while (true) {}")
      except RuntimeTimeout:
          ...  # only a timeout reaches here
  ```

  It is deliberately not called `TimeoutError`: Python's builtin of that name
  derives from `OSError`, and a same-named subclass of a different base would
  be a trap.

  Every timeout path raises it: `eval`, `eval_async`, `eval_module`,
  `eval_module_async`, `fn(...)`, `fn.call_async(...)`, and the awaited half
  of a `fn(...)` that returned a promise. `tests/test_timeout_every_path.py`
  has one row per path, each asserting the type, a wall-clock bound, and that
  the runtime is still usable and closes afterwards.

### Fixed

- **A JS function call whose JS was still running at its deadline hung, or
  raised the wrong error.** Pre-existing in 0.4.0.

  - `fn.call_async(...)`, and the awaited half of a `fn(...)` that returned a
    promise, armed no watchdog. Their deadline was only checked *between*
    event-loop steps, and the dispatcher's own step watchdog is armed only
    while no job is active, so `async () => { await 0; while (true) {} }`
    hung forever, `timeout=` and `RuntimeConfig.timeout` notwithstanding.
    Both now arm one for the job's lifetime, as `eval_async` does; a resumed
    call arms it for what is left of the original call's clock.
  - `fn(..., timeout=...)` on a runtime without `RuntimeConfig.timeout`
    armed nothing either: the synchronous call only consulted the
    runtime-wide timeout. It now uses the per-call one when given.
  - When a watchdog did stop a function call, the caller got `JavaScriptError:
    Uncaught null` — not `RuntimeTimeout`, not even a `RuntimeError` — because
    V8 reports a terminated `func.call` as a null exception, which the timeout
    mapping did not recognise. It is now reported as `execution terminated`,
    exactly as a terminated `eval` is, so it becomes `RuntimeTimeout` when the
    call's own deadline fired and `RuntimeTerminated` after `terminate()`.

  Because async function calls now have a real deadline, they take part in
  the cross-talk described under *Documented* below, exactly as `eval_async`
  already did: an async call's deadline can stop an unrelated synchronous
  call dispatched inline while it is parked. A function-call victim of that
  reports `execution terminated`, where it used to say `Uncaught null`.

- **An async job that timed out on a pending promise left the runtime
  permanently unusable.** After `await rt.eval_async("new Promise(() => {})",
  timeout=0.3)` raised its `RuntimeTimeout`, every later call on that runtime
  — a plain `rt.eval("1 + 1")` issued long afterwards, with nothing else in
  flight — failed with a bare `execution terminated`. The runtime did not
  error and recover; it stopped working while still reporting itself open.

  The job's own deadline check asked V8 to terminate and nothing cancelled
  that request. The cancel that exists, in `resolve_sync_watchdog`, runs only
  when the job's watchdog token comes back *fired* — and the in-job check
  routinely wins the race against the watchdog thread, since both wake on the
  same deadline and the dispatcher parks until exactly that instant, so
  `disarm` returned `false` and the isolate stayed latched. `JobCommon::expired`
  now records the request it made and `JobCommon::respond` clears it, the
  async counterpart of what the synchronous path already did. Pre-existing
  in 0.4.0.

  The timed-out promise itself stays pending, as before — nothing on the
  Python side awaits it, so nothing hangs.
  `tests/test_timeout_cross_talk.py` asserts reuse (sync and async, and
  across a second timeout).

- **A timeout that raced its own deadline could kill the next, unrelated
  call.** The watchdog thread marked an expired deadline as fired, released
  its lock, and only then asked V8 to terminate. A call that finished on its
  own just past its deadline could be disarmed in that gap — seeing `fired`,
  cancelling a termination that had not been requested yet — and the
  watchdog's late request then latched the isolate, so the *next* call failed
  with a bare `execution terminated`. The watchdog now holds its lock until
  the termination has been issued. Pre-existing in 0.4.0; the window is
  microseconds wide, so it was rare rather than impossible.

- **`Runtime.close()` could hang.** `Watchdog::drop` set the shutdown flag
  without holding the mutex the watchdog thread's condition variable is
  paired with, so the wake-up could be lost and the watchdog thread parked
  forever, blocking the `join` in `close()`. It now holds that mutex across
  the write.

- **The reason attached to a multi-expiry watchdog pass is no longer
  arbitrary.** When several deadlines expired in the same pass, the reason was
  taken from the last fired entry in a `Vec` whose order `swap_remove` makes
  meaningless. It is now the deadline that expired first.

- **`CLAUDE.md`'s streaming example called API that never existed.**
  `rt.create_js_stream_from_python(...)` and the guest global
  `__pydeno_get_stream__(id)` appear only in that example — `git log -S` puts
  both in the initial commit and nowhere else, and the `peno` → `pydeno`
  rename dutifully renamed a symbol that was never real. The example now uses
  `rt.stream_from_async_iterable(...)`, and `tests/test_claude_md_api_references.py`
  checks that every `Runtime` attribute and guest global the file names
  actually resolves.

### Documented

Known limitations are now stated where callers will meet them, and pinned
by tests so they cannot drift silently.

- **`SnapshotBuilder` input is not sandboxed.** Its docstring now says so:
  snapshot scripts run with the raw `Deno.core.ops` table, no timeout and no
  serialization limit, so they are host code, never untrusted JavaScript.

- **A debug build cannot reach the default `max_serialization_depth`.** The
  native stack-headroom backstop that keeps a deeply nested value from
  aborting the process (`Check failed: IsOnCentralStack()`) trips around depth
  22 in an unoptimized build, against ~743 in an optimized one, because an
  unoptimized serializer frame is ~33x larger. Released wheels are optimized
  builds and are unaffected; anyone working on pydeno itself is not. No single
  budget can serve both profiles, so the backstop is unchanged — but its error
  message now names the build profile as the cause instead of reading as a
  fault in the caller's data, `RuntimeConfig.max_serialization_depth`
  documents the ceiling, and `tests/test_serialization_headroom.py` covers the
  22–99 band at the default configuration that nothing exercised before.

- **A fired deadline terminates whatever the isolate is running, not the job
  that timed out.** `terminate_execution` is isolate-wide and the isolate is
  single-threaded, so a synchronous call dispatched while an async job is
  parked on a promise can be stopped by that async job's deadline, and reports
  a bare `execution terminated` error rather than a timeout. There is nothing
  finer to aim at, so the behaviour stands; it is described on
  `RuntimeConfig.timeout` and on `ArmedDeadline`, and pinned by
  `tests/test_timeout_cross_talk.py`. Use a runtime per concurrent job if a
  termination error has to be about the call that raised it.


## 0.4.0

The package was renamed from `peno` to `pydeno`. Import `pydeno`; there is no
compatibility shim under the old name.

### Breaking changes

- **`IsolatePool` and `PooledIsolate` are removed.** `from pydeno import
  IsolatePool` now raises `ImportError`. The pool drove a bare `v8::Isolate`
  with no serialization limits, no timeout and no heap cap, so guest code
  running in a pooled isolate could return an unbounded result and take the
  host process down — a second, unmetered path around every limit `Runtime`
  enforces. It also never supported tools (it had no op registry to bind a
  Python callable into), and it measured ~12x *slower* than simply retaining
  a warm `Runtime`.

  Replace a pooled isolate with a retained `Runtime`:

  ```python
  # before
  pool = IsolatePool(size=4)
  with pool.checkout() as iso:
      iso.eval("1 + 1")

  # after — keep the Runtime alive and reuse it
  rt = Runtime(RuntimeConfig(timeout=5.0))
  rt.eval("1 + 1")
  ```

- **`Deno`, `__bootstrap` and `__infra` are deleted from the guest global.**
  `globalThis.__bootstrap.core.ops` used to survive the bridge's `delete
  globalThis.Deno` and exposed the same raw op table; `op_print` wrote
  arbitrary bytes straight to the host process's stdout, with no timeout and
  no metering. Guest JS that reached for any of these now sees `undefined`.
  The exact guest-visible `Reflect.ownKeys(globalThis)` surface is pinned by
  `tests/test_guest_globals.py`, so a future `deno_core` bump that installs a
  new global fails loudly instead of silently reopening the hole.

  This does not apply to `SnapshotBuilder`, which by design runs host code in
  an unsandboxed isolate — see its docstring.

- **A runaway microtask queue now times out against the call that created
  it.** `eval()` and `eval_module()` drain the microtask queue inside their
  own timeout window. A script such as
  `const f = () => queueMicrotask(f); f();` previously *returned a value*
  immediately and left the queue to hang some later, unrelated, untimed
  operation (the next `eval`, or `close()`). It now raises
  `RuntimeError: ... timed out after Nms` from the call that queued it,
  whenever `RuntimeConfig(timeout=...)` is set. Callers that relied on such a
  script returning normally will now see an exception.

- **`execution_timeout` is enforced around the dispatcher's event-loop step.**
  Work that outlives the job that queued it (a fire-and-forget continuation
  that keeps re-queuing itself) previously ignored `execution_timeout`
  entirely and could wedge the runtime. It is now terminated.

### Fixes

- Recursion in the JS→Python converter is bounded by real native/V8 stack
  headroom, not only by `max_serialization_depth`. Raising that knob past
  what the isolate's stack can sustain used to abort the process; it now
  raises a catchable `RuntimeError`. Note that the check is unconditional and
  the per-frame cost differs by build profile: in an unoptimized build it
  trips around depth 22, in a release build around depth 743.
- Cycle detection walks the current traversal path (`strict_equals`) instead
  of a `get_identity_hash()` set, so two distinct acyclic objects sharing a
  V8 identity hash are no longer reported as a circular reference.

### Performance

- One persistent watchdog thread per runtime replaces the thread spawned and
  joined for every timed call: a timed `eval` costs ~9.5 µs against ~28.5 µs
  before, within noise of an untimed one.
