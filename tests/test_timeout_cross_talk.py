"""A fired deadline stops whatever is running, not the job that timed out.

The 0.4.0 review flagged this (O2) as possibly a bug. It is not: it is a
property of one isolate on one thread. `v8::Isolate::terminate_execution` is
isolate-wide and has no per-job scope, so when a deadline expires the only
thing the watchdog can do is stop the isolate -- and what the isolate happens
to be running may belong to another job entirely.

This test exists to make that a *stated* contract instead of a surprise
discovered in production. It pins the exact shape of the cross-talk, so that
if a later change ever does attribute deadlines to jobs, this test fails and
has to be rewritten deliberately rather than quietly drifting.

See the `ArmedDeadline` doc comment in `src/runtime/runner.rs` for why
attributing them is the one-isolate-one-thread model with extra steps, and
`RuntimeConfig.timeout` for the caller-facing consequence.
"""

from __future__ import annotations

import asyncio

import pytest

from pydeno import JavaScriptError, Runtime, RuntimeConfig, RuntimeTimeout

# Long enough to still be running when the async job's 0.3 s deadline fires.
_BUSY = "(() => { const end = Date.now() + 1500; while (Date.now() < end) {} return 'done'; })()"


@pytest.mark.asyncio
async def test_async_deadline_terminates_an_unrelated_inline_sync_call() -> None:
    with Runtime(RuntimeConfig()) as rt:
        parked = asyncio.ensure_future(
            rt.eval_async("new Promise(() => {})", timeout=0.3)
        )
        # Let the async job reach the runtime thread and park.
        await asyncio.sleep(0.1)

        # This call sets no deadline of its own, and on its own would finish.
        with pytest.raises(JavaScriptError) as sync_exc:
            rt.eval(_BUSY)

        # The victim reports a bare termination, not a timeout: its own
        # watchdog token never fired, so there is nothing to convert the
        # termination into.
        assert "execution terminated" in str(sync_exc.value)
        assert not isinstance(sync_exc.value, RuntimeTimeout)

        # The job that actually timed out still reports its own timeout
        # correctly -- the cross-talk costs the *other* job, not this one.
        with pytest.raises(RuntimeTimeout) as async_exc:
            await parked
        assert "timed out after 300ms" in str(async_exc.value)


@pytest.mark.asyncio
async def test_runtime_is_unusable_after_an_async_deadline_fires() -> None:
    """Pinned as found, and *not* a design property -- a defect.

    `PendingJob::expired` calls `terminate_execution()` and nothing ever calls
    `cancel_terminate_execution()` for it: only the synchronous path does, in
    `resolve_sync_watchdog`. So an async job that times out on a pending
    promise leaves the isolate with a termination still latched, and every
    later call on that runtime fails with a bare `execution terminated` --
    even one issued long afterwards, with nothing else in flight.

    This is pre-existing (confirmed against this branch's `runner.rs` with the
    0.4.1 changes reverted), out of the scope the 0.4.1 follow-ups were
    defined by, and it is what makes the cross-talk above so visible in
    practice. It is pinned here so it is *known*: whoever fixes it should
    delete this test and assert reuse instead.
    """
    with Runtime(RuntimeConfig()) as rt:
        with pytest.raises(RuntimeTimeout):
            await rt.eval_async("new Promise(() => {})", timeout=0.3)
        with pytest.raises(JavaScriptError, match="execution terminated"):
            rt.eval("2 + 2")


def test_sync_deadline_leaves_the_runtime_usable() -> None:
    """The contrast that shows the above is specific to the async path: a
    synchronous timeout disarms its own watchdog, which cancels the pending
    termination, and the runtime keeps working."""
    with Runtime(RuntimeConfig(timeout=0.3)) as rt:
        with pytest.raises(RuntimeTimeout):
            rt.eval("while (true) {}")
        assert rt.eval("2 + 2") == 4
