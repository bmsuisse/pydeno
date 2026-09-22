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
async def test_runtime_survives_an_async_deadline_on_a_pending_promise() -> None:
    """The async timeout is recoverable, like the sync one below.

    This replaces `test_runtime_is_unusable_after_an_async_deadline_fires`,
    which pinned the opposite as a known defect: `JobCommon::expired` asked V8
    to terminate and nothing cancelled it, because the only cancel was
    conditional on the job's *watchdog token* coming back fired -- and this
    check routinely beats the watchdog thread to the same deadline, so
    `disarm` returned `false`. The isolate stayed latched and every later
    call, with nothing in flight, died with `execution terminated`.

    `respond` now clears the termination it latched, so the runtime is handed
    back usable -- synchronously and asynchronously, and it still closes.
    """
    with Runtime(RuntimeConfig()) as rt:
        with pytest.raises(RuntimeTimeout):
            await rt.eval_async("new Promise(() => {})", timeout=0.3)

        assert rt.eval("1 + 1") == 2
        assert await rt.eval_async("Promise.resolve(41 + 1)") == 42

        # A second timeout must leave it just as usable as the first.
        with pytest.raises(RuntimeTimeout):
            await rt.eval_async("new Promise(() => {})", timeout=0.3)
        assert rt.eval("1 + 1") == 2

    # Leaving the `with` block closed the runtime; if `close()` hung, this
    # line is never reached.
    assert rt.is_closed()


def test_sync_deadline_leaves_the_runtime_usable() -> None:
    """The contrast that shows the above is specific to the async path: a
    synchronous timeout disarms its own watchdog, which cancels the pending
    termination, and the runtime keeps working."""
    with Runtime(RuntimeConfig(timeout=0.3)) as rt:
        with pytest.raises(RuntimeTimeout):
            rt.eval("while (true) {}")
        assert rt.eval("2 + 2") == 4
