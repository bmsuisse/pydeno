# Design note: a supervised process for host tools (experimental, 0.12)

Issue #45 asks, as its fifth item, for host tools to run in a process of their own, with a per-call
deadline and result caps, "so a tool bug is not in the parent's address space". This note records
what was evaluated for 0.10, why it was not built in the same change as the seccomp allow-list, the
requirements a sound version has to meet, and, in "What shipped in 0.12", how `ToolProcess` meets
them and what is still open. The per-call `tool_timeout` shipped in 0.11; `ToolProcess` is phase 1
of the process boundary and is **experimental** (see the user guide, "Host tools in a child
process", in `docs/guides/advanced/isolation.md`).

## Where tools run today

A host function bound to an `IsolatedRuntime` (`bind_function`, `bind_object`, `ToolBridge`, the
`AgentSandbox` and `Pydeno` tools) runs **in the parent**, with the parent's full authority:

- a synchronous handler runs on the thread that drives the command (`_run_sync_handler`); an async
  one runs on the caller's event loop (`asyncio.run_coroutine_threadsafe`);
- what the worker sends is validated before a handler sees it (frame size, depth, node budget,
  closed set of value tags; an unknown host-function id ends the session);
- what bounds a call today: `max_inflight_host_calls` (64 outstanding, runtime-wide),
  `max_host_calls` (count per runtime), `max_host_wait` (total time a command may wait on host
  calls, 60 s by default; the worker's CPU cap keeps running meanwhile), the reply frame cap
  (16 MiB) and the decoder budgets on the way back, error redaction (`redact_host_errors=True`);
- what is *not* bounded per call: how long one call may take (only the per-command total), how much
  memory or CPU the handler itself uses, and what it can do to the parent if it crashes (a segfault
  in a C extension it calls takes the parent with it).

So the guest cannot reach a tool's internals, but a *tool's own* bug or resource use is the
parent's problem.

## What a tool process has to be (the requirements)

1. **A supervised tool host.** A child process (not the JavaScript worker) that imports the tool
   functions and serves calls over the same framed wire (`_wire`), started and killed by the parent
   like a worker: `start_new_session`, an empty environment unless the caller passes one, a kill on
   a per-call deadline, a restart afterwards.
2. **Per-call limits.** A deadline per call (kill and restart the tool host on overrun; the guest
   gets an error), a cap on the result's encoded size before it is sent, a memory ceiling
   (`RLIMIT_DATA` plus the RSS poll the worker already has) and a CPU cap.
3. **Tools must be importable, not closures.** A tool host cannot receive a lambda over the
   parent's database connection. Tools would be named as `module:function` (or a module that
   registers them), and their state would live in the tool host. This is the real design decision,
   because it changes how people write tools: today any callable works.
4. **Confinement is optional and per tool.** A tool that fetches URLs needs the network; one that
   reads a file needs that file. A useful version takes an explicit capability description per tool
   (Landlock paths, TCP ports), applied with the same machinery as the worker's sandbox. Without
   that it is crash isolation and resource limits, not a sandbox, and should be named so.
5. **Semantics that do not change silently.** Exceptions keep their class name and (redacted)
   message; async tools keep running concurrently; `ToolBridge` budgets and the journal (which
   records each call's outcome for replay) treat "the tool host died" as a recorded failure of that
   call, never as "the call did not happen", because it may have had side effects.

## Why it was deferred from 0.10

- **It is a new public API, not a hardening.** Item 3 alone (importable tools instead of
  callables) changes how every tool is written. Shipping it inside a security PR whose other four
  items change no API would hide a design decision in a large diff.
- **Its main risk is semantic, and the tests for it do not exist yet.** The journal and replay
  rules (agent sessions, `SessionPool`, the `Pydeno` front door) assume a tool call either returned
  or raised in the parent. A tool host that can die mid-call adds a third outcome that every one of
  those paths must record and replay identically; getting that wrong would lose or duplicate tool
  side effects, which is worse than the crash it prevents.
- **The guest-facing boundary does not depend on it.** A guest that escapes V8 is confined by the
  worker's OS sandbox whatever the tools do; this item protects the host from its own tools.

## The smaller step shipped: `tool_timeout=`

The opt-in per-call deadline for tools that run in the parent shipped in 0.11 (see "Per-call tool
deadline" in `docs/guides/advanced/isolation.md`). The guest gets a catchable `TimeoutError`
("host function timed out"), async handlers are cancelled, a synchronous handler is abandoned on its
thread (with the option set it runs on a thread of its own) and its late result is discarded, and
past 8 abandoned calls still running the session ends. Agent sessions journal a timed-out call as a
failed call, so a replay sends the same failure and never re-runs the tool. That is the
guest-visible half of item 5; what remains is the process boundary: crash isolation and memory
limits for a tool's own bugs, importable tools, and per-tool confinement.

## What shipped in 0.12: `ToolProcess` (experimental)

`pydeno.ToolProcess` (`python/pydeno/_toolproc.py` in the parent, `_toolhost.py` in the child)
meets requirements 1 to 5 as follows. The tests are `tests/test_tool_process.py`.

### The API, and why this one

```python
tools = ToolProcess(call_timeout=60, max_result_bytes=1 << 20, max_memory=None, cpu_seconds=None,
                    env=None, path=None, sandbox=None)
rt.bind_function("lookup", tools.tool("myapp.tools:lookup"))
```

Alternatives considered: a `process=` option on `bind_function` / `bind_object`; an
`IsolatedRuntime.bind_tool_process(...)`; a `tool_process=` constructor argument. They put the
feature in every entry point (`IsolatedRuntime`, `AsyncIsolatedRuntime`, `SandboxPool`,
`AgentSandbox`, `Pydeno`, the pydantic-ai integration), each needing its own tests, and they would
make the runtime own a child process whose lifetime is not the runtime's. A separate object whose
`tool()` returns an **ordinary async callable** adds *no* parameter to any existing class and needs
no change in any of them: everything that accepts a tool already handles a callable that returns
or raises, which is what makes the semantic requirement (5) hold by construction instead of by
patching each path. One `ToolProcess` may serve several runtimes (tool state is then shared, which
is what "state lives in the tool host" means), and its lifetime is explicit (`close()`, a `with`
block, a finalizer).

### Decisions

1. **Supervised tool host.** The child is `python -I -c ...` running `pydeno._toolhost`, not the
   JavaScript worker (it never sees guest code, only a call's arguments). It is started with
   `start_new_session=True` and `env={}` unless `env=` is given, killed with `killpg(SIGKILL)`, and
   restarted lazily by the next call. Two parent threads per tool host: a reader (frames from the
   child; on EOF it fails what is in flight and releases the pipes) and a supervisor (every 25 ms:
   deadlines, resident memory, CPU). Neither holds the `ToolProcess`, so a forgotten one is
   collected and its host killed; `atexit` kills the rest; the child exits on stdin EOF and on a
   changed parent pid (the same two witnesses as the worker). The wire is `_wire`'s frames and
   decoder budgets; the child's stdout is re-pointed away from the protocol fd as in the worker.
   The child gets the parent's `sys.path` (so `module:function` is importable there) rather than
   `-S`, because a tool's module is arbitrary user code.
2. **Limits.** `call_timeout` (default 60 s, `None` off) covers queueing and start-up and is
   enforced by the supervisor, so it holds when the awaiting task was cancelled; on overrun the host
   is killed and the call gets `tool_timeout_error()`, the same `TimeoutError` class and text
   `tool_timeout` produces (the flag that exempts it from redaction is on the class, because asyncio
   rebuilds `TimeoutError` across threads, found in 0.11 CI on Python 3.12). `max_result_bytes`
   (default 1 MiB) is checked in the child on the encoded frame before it is written, so an
   oversized result never crosses the pipe (`ToolResultTooLarge`, the host stays alive); the
   parent's reader additionally caps a frame at that size plus 256 KiB. `max_memory` is the RSS poll
   plus `RLIMIT_DATA` (Linux) at `max_memory` plus 96 MiB plus the thread stacks, applied in the
   child before tools import; a private allocation past it fails as `MemoryError` in the tool
   (an ordinary tool error) or is killed by the poll, whichever is first. `cpu_seconds` is CPU of the
   whole host since the call began, so concurrent calls count each other's (conservative); there is
   no cumulative `RLIMIT_CPU`, which would kill a healthy long-lived host.
3. **Importable tools only.** `tool()` accepts `'module:function'` (dotted attributes allowed) or a
   module-level function whose module attribute *is* that function. Lambdas, closures,
   `__main__` functions, bound methods, partials and callable objects raise `TypeError`/`ValueError`
   saying why and what to do. A passed function lends its name, docstring and signature to the
   handler (`functools.update_wrapper`), so catalogs and TypeScript stubs describe it. A string is
   only syntax-checked in the parent (importing it there would defeat the point); an import failure
   is the call's error (`ModuleNotFoundError`, redacted like any tool error).
4. **Confinement is optional and whole-host.** `sandbox="auto" | "require"` runs the worker's
   machinery in the child (`harden_process`, `limit_data`-style rlimits, `apply(empty_root=True)`,
   `missing_layers`, the same "root that could not drop privileges" refusal), after pre-importing
   every registered tool and warming the native codec's lazy imports (they would fail on a closed
   filesystem), and before any thread is created (Landlock and the user-namespace layer need a
   single-threaded process). `"require"` refuses to start with `ToolProcessStartError` naming the
   missing layers; `start()` surfaces it eagerly. The seccomp allow-list needed no change for
   asyncio or `ThreadPoolExecutor` (verified on Linux x86_64). Without `sandbox=` it is documented,
   in the docstring, the guide and here, as crash isolation and resource limits, not a sandbox.
   `attest()` (the worker's self-test of forbidden operations) is *not* run in the tool host.
5. **Semantics do not change silently.**
   - *Exceptions.* The child sends `type(exc).__name__` and `str(exc)`; the parent raises a plain
     exception class of that name with that message, and the runtime redacts it per
     `redact_host_errors` exactly as for a tool in the parent (parity is a test, for both settings).
     An exception named like pydeno's own (`ToolProcessDied`, `TimeoutError`) raised *by a tool* is
     not trusted as pydeno's: the exemption from redaction is by class, and the child's names only
     ever map to plain classes.
   - *Async.* `tool()` always returns `async def`, so the guest always gets a Promise, and calls run
     concurrently in the child (one event loop for `async` tools, up to 32 threads for sync ones).
     This is a deliberate difference from a plain synchronous tool, documented.
   - *Death is a recorded failure.* A tool host that dies mid-call (signal, exit, limit, close)
     fails every call in flight with `ToolProcessDied` (public text with the signal name or exit
     code, nothing from the tool, which could hold secrets). Because the handler *raises*, the agent
     journal records it as `["ans", "e", "ToolProcessDied", message]`, the same record shape as a
     `TimeoutError`, and a replay answers the guest from the journal without calling the handler,
     so no tool host starts and the tool does not run again (tested for `AgentSandbox` and
     `AsyncAgentSandbox`, including loading an async journal into the sync class). Calls that were
     merely in flight next to an overrunning call also die (collateral of killing the process) and
     are recorded as deaths of those calls, never as not-run.

### What is not done

- **No per-tool capability grants** (Landlock paths, TCP ports): `sandbox=` is all-or-nothing for
  the host, so a tool that needs a file or the network cannot be confined yet.
- **No per-call isolation.** Calls share the host: one call can read another's state, and one
  runaway call kills its neighbours. A pool of tool hosts or one host per tool is the next step.
- **No cgroup caps** and no `attest()` self-test in the tool host.
- **`ToolBridge` budgets** count a call the guest made, including ones that died; there is no
  separate budget for restarts, so a guest can make the host restart as often as its call budget
  allows (each restart costs an interpreter start-up).
- **Windows**: unsupported, like the rest of the isolated runtimes. **macOS** (Seatbelt in the
  tool host, `usage()` via `proc_pidinfo`) and **aarch64** are untested; only Linux x86_64 was run.
- **`ToolProcessDied` messages are not stable API text**; the class name is.
- The `Pydeno` front door and `SandboxPool` accept the handler like any tool, but have no
  dedicated tests beyond the `IsolatedRuntime`, `AsyncIsolatedRuntime`, `ToolBridge` and agent paths.
