# Design note: a sandboxed process for host tools (deferred)

Issue #45 asks, as its fifth item, for host tools to run in a process of their own, with a per-call
deadline and result caps, "so a tool bug is not in the parent's address space". This note records
what was evaluated for 0.10, why it was not built in the same change as the seccomp allow-list, and
what a sound version would look like. It is a plan, not a description of shipped behaviour.

## Where tools run today

A host function bound to an `IsolatedRuntime` (`bind_function`, `bind_object`, `ToolBridge`, the
`AgentSandbox` and `Pydeno` tools) runs **in the parent**, with the parent's full authority:

- a synchronous handler runs on the thread that drives the command (`_run_sync_handler`); an async
  one runs on the caller's event loop (`asyncio.run_coroutine_threadsafe`);
- what the worker sends is validated before a handler sees it (frame size, depth, node budget,
  closed set of value tags; an unknown host-function id ends the session);
- what bounds a call today: `max_inflight_host_calls` (64 outstanding, runtime-wide),
  `max_host_calls` (count per runtime), `max_host_wait` (total time a command may wait on host
  calls, 600 s by default; the worker's CPU cap keeps running meanwhile), the reply frame cap
  (16 MiB) and the decoder budgets on the way back, error redaction (`redact_host_errors=True`);
- what is *not* bounded per call: how long one call may take (only the per-command total), how much
  memory or CPU the handler itself uses, and what it can do to the parent if it crashes (a segfault
  in a C extension it calls takes the parent with it).

So the guest cannot reach a tool's internals, but a *tool's own* bug or resource use is the
parent's problem.

## What "a sandboxed tool process" would have to be

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

## Why it was deferred

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

## A smaller step that could ship first

An opt-in **per-call deadline** for tools that already run in the parent (`tool_timeout=`): the
guest gets an error when the deadline passes, the handler's thread or task is abandoned (async
tasks are cancelled; a synchronous handler cannot be interrupted, only outlasted), and a counter of
abandoned calls stops the session past a threshold (the same rule `AgentSandbox` already applies to
abandoned tool calls). That gives the guest-visible half of item 5 without the process boundary,
and it is a natural first PR before the tool host.
