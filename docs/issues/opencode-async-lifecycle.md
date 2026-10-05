# Async agent lifecycle regression coverage

The existing async agent tests cover basic call budgets and cancellation at one blocked tool.
The new focused tests extend coverage to un-awaited calls, rejected Promise.all with an outstanding
call, Promise.race losers, queued bursts under a small runtime in-flight cap, and cancellation
that executes the host coroutine finalizer and reaps the worker/shim tasks.

The initial scope was a coverage gap; subsequent testing found the late-dispatch defect below. The existing shared prelude drains
outstanding calls with Promise.allSettled. Python host tools are driven serially by run();
a runtime in-flight cap controls guest/host shims, and is not a promise of parallel host tools.
Standard JavaScript Promise.race must not cancel losing promises. Only the verified late-dispatch guard below changes production code.

Acceptance:
- All new cases pass against a compatible native Linux ARM64 wheel with current Python overlay.
- Outstanding host calls settle before the next run; rejected/raced programs do not lose calls.
- A twelve-call burst succeeds under max_inflight_host_calls=3, preserves call order and consumes
  the twelve-call session budget without transient cap errors.
- Cancellation executes the pending async tool's finally block, exits the actual worker and
  removes outstanding runtime shim tasks within bounded test deadlines.
- Existing async agent/recovery/result suites remain green; no Promise.race semantic changes.

Source inspiration (read-only review of OpenCode dev, no local checkout or source execution):
- https://github.com/anomalyco/opencode/blob/dev/packages/codemode/src/interpreter/runtime.ts#L674
- https://github.com/anomalyco/opencode/blob/dev/packages/codemode/test/promise.test.ts

OpenCode's CodeMode deliberately interrupts Promise.race losers; its interpreter is in the host
process and does not replace pydeno's OS sandbox or killable worker. Transfer the lifecycle test
matrix rather than that differing Promise.race behavior.

## Verified late-dispatch cleanup defect

Native ARM64 Linux testing subsequently reproduced a closed-session callback failure: a runtime
shim scheduled before cancellation could enter `_Core.on_tool_call` after `release_calls()`.
The callback created a fresh future and appended it to `abandoned`, leaving nobody to answer it.
A deterministic regression closes a real session and invokes this late callback; before the fix
it timed out. The guard now rejects a closed session with WorkerCrashed before allocating a
future or spending tool budget. Calls parked between commands while the session remains open
keep their existing behavior. No Promise.race semantics changed.

This reproduction used a previously built native ARM64 Linux extension with current Python
source overlaid, not a final release-artifact run. Native green verification is performed by the
coordinator because this agent's Podman socket access is denied.

Verification: the coordinator's native Linux ARM64 CPython 3.12 run reproduced the deterministic
late callback regression before the guard (1 failed, 5 passed). After the guard, 172 tests passed
in 79.44 seconds: all six new lifecycle cases plus async agent, recovery, results, isolated runtime
and backpressure coverage. The compiled extension came from existing 0771370 artifacts with the
current Python overlay; this is not a final-wheel verification claim. Independent coordinator
review found no blocker in the closed-session guard.
