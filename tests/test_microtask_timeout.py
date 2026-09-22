"""Regression test for a runaway microtask queue escaping its own timeout.

`execute_script` only runs a script's top-level statements; anything the
script queues with `queueMicrotask` (including a microtask that re-queues
itself, forever) is left pending afterward. Before this fix, neither
`eval_sync` nor `eval_module_sync` drained that queue, so a recursive
`queueMicrotask` call did not hang *that* call -- it returned immediately --
and instead hung whatever *later*, unrelated, untimed operation on the same
runtime thread eventually triggered a microtask checkpoint (the next `eval`,
or `close()`). `RuntimeConfig(timeout=...)` covered the call that queued the
runaway microtask, but not the call that ended up draining it, so the
deadline that was supposed to bound this was silently irrelevant.

`eval_sync`/`eval_module_sync` now call `perform_microtask_checkpoint()`
themselves, still inside their own `start_sync_watchdog` window, so a
runaway drain is interrupted and reported against the call that actually
caused it, exactly like a runaway script would be.
"""

from __future__ import annotations

import time

import pytest

from pydeno import Runtime, RuntimeConfig

# `f` re-queues itself as a microtask forever. The outer script itself
# returns immediately (`execute_script` only runs top-level code), so this
# is purely a test of whatever drains the microtask queue afterward.
RECURSIVE_MICROTASK = "(() => { const f = () => queueMicrotask(f); f(); return 1; })()"


def test_recursive_microtask_times_out_within_its_own_call() -> None:
    with Runtime(RuntimeConfig(timeout=0.3)) as rt:
        start = time.monotonic()
        with pytest.raises(Exception, match="timed out"):
            rt.eval(RECURSIVE_MICROTASK)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, (
            f"runaway microtask queue was not bounded by its own timeout: "
            f"took {elapsed:.2f}s"
        )

        # The runtime must be immediately usable afterward, with no leftover
        # hang from the queue that was just discarded.
        second_start = time.monotonic()
        assert rt.eval("2") == 2
        assert time.monotonic() - second_start < 0.5, (
            "a later eval() hung, meaning the runaway microtask queue leaked "
            "past the call that created it"
        )

    # `close()` on a runtime that just had a runaway microtask queue
    # discarded must also return promptly, not hang.
    close_start = time.monotonic()
    rt.close()
    assert time.monotonic() - close_start < 1.0


def test_recursive_microtask_via_module_sync_also_times_out() -> None:
    """`eval_module_sync` also drains microtasks after its own event-loop
    poll finishes, so a module whose top-level code queues a runaway
    microtask outside of what the module's own evaluation awaits is bounded
    the same way."""
    with Runtime(RuntimeConfig(timeout=0.3)) as rt:
        rt.add_static_module(
            "leaky",
            "export const x = (() => { "
            "const f = () => queueMicrotask(f); f(); return 1; "
            "})();",
        )
        start = time.monotonic()
        with pytest.raises(Exception, match="timed out"):
            rt.eval_module("leaky")
        assert time.monotonic() - start < 0.5

        assert rt.eval("2") == 2
