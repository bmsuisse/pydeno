"""Regression test for a runaway async chain that outlives the job that
queued it.

An active job's own watchdog (armed for the job's whole lifetime, falling
back to `RuntimeConfig(timeout=...)` when no per-call timeout is given)
bounds every `poll_event_loop` call made while that job is active. But a
script can queue work that is not tied to the promise the job is waiting
on: a *fire-and-forget* async call (started but never returned/awaited)
whose continuation only runs once the async op resolves -- which can happen
well after the job that started it has already completed and been cleared
from `active_job`. If that continuation kicks off a self-perpetuating
microtask chain (`queueMicrotask` re-queuing itself), nothing was arming a
deadline around the `poll_event_loop` call that discovers it, so
`RuntimeConfig(timeout=...)` was silently not enforced and the dispatcher
-- and therefore the whole runtime, including every later call -- hung
indefinitely.

`RuntimeDispatcher::run` now arms a deadline around `poll_event_loop`
whenever no job currently holds one (`active_job.is_none()`), using the
same persistent per-runtime watchdog P1 introduced, so this case is bounded
by `execution_timeout` exactly like a runaway inside an active job already
was.
"""

from __future__ import annotations

import asyncio

import pytest

from pydeno import Runtime, RuntimeConfig


@pytest.mark.asyncio
async def test_runaway_after_job_completes_is_bounded_by_execution_timeout():
    with Runtime(RuntimeConfig(timeout=0.5)) as rt:

        async def slow_host_call():
            # Resolves well after the eval_async job below has already
            # completed and `active_job` has been cleared.
            await asyncio.sleep(0.1)
            return 1

        rt.bind_function("slowHostCall", slow_host_call)

        # `slowHostCall()` is started but never returned/awaited by this
        # script's own promise, so the eval_async job it belongs to
        # completes immediately with `1`. Only once the host call resolves
        # -- independently, with no job tracking it -- does the `.then`
        # continuation start a self-requeuing microtask loop.
        source = """
        (() => {
          slowHostCall().then(() => {
            globalThis.__started = true;
            const inner = () => {
              globalThis.__ticks = (globalThis.__ticks || 0) + 1;
              queueMicrotask(inner);
            };
            inner();
          });
          return 1;
        })()
        """

        result = await rt.eval_async(source)
        assert result == 1

        # Give the fire-and-forget call time to resolve and the runaway to
        # start, with no active job present -- this is the window the fix
        # covers.
        await asyncio.sleep(0.3)

        # Before the fix, this hung indefinitely: the dispatcher was wedged
        # inside a `poll_event_loop` call draining a microtask queue that
        # never empties, with nothing arming a deadline around it.
        second = await asyncio.wait_for(rt.eval_async("2 + 2"), timeout=5.0)
        assert second == 4

        # The runaway did actually start (and ran for a while) before being
        # cut off -- this isn't passing because the callback never fired.
        assert rt.eval("!!globalThis.__started") is True
        assert rt.eval("globalThis.__ticks || 0") > 0

        # The runtime must stay usable afterward.
        assert rt.eval("1 + 1") == 2
