# Async agent lifecycle regression coverage

The existing async agent tests cover basic call budgets and cancellation at one blocked tool.
The new focused tests extend coverage to un-awaited calls, rejected Promise.all with an outstanding
call, Promise.race losers, queued bursts under a small runtime in-flight cap, and cancellation
that executes the host coroutine finalizer and reaps the worker/shim tasks.

This is a coverage gap, not a verified production defect. The existing shared prelude drains
outstanding calls with Promise.allSettled. Python host tools are driven serially by run();
a runtime in-flight cap controls guest/host shims, and is not a promise of parallel host tools.
Standard JavaScript Promise.race must not cancel losing promises. No production changes proposed.

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
