"""Timeouts are enforced when guest code customises `Error.prototype` (or `Error` itself).

When a deadline stops a loop, V8 builds an "execution terminated" error and deno_core converts
it, cancelling the termination first and then reading properties of that error (`constructor`
and `name` up its prototype chain, `cause`, `stack` through `Error.prepareStackTrace`,
`Symbol.for("errorAdditionalPropertyKeys")`). That error's prototype is the guest's
`Error.prototype`, so a getter that loops there used to run with no deadline left: the timed
call never returned. The watchdog now keeps stopping the isolate until the call is done.

Plain `Runtime` cases run in a subprocess with a hard cap, so a regression fails the test instead
of hanging the suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from pydeno import IsolatedRuntime, RuntimeConfig, RuntimeTimeout

POISON = {
    "constructor-getter": (
        "Object.defineProperty(Error.prototype, 'constructor', "
        "{get() { for (;;) {} }, configurable: true});"
    ),
    "cause-getter": (
        "Object.defineProperty(Error.prototype, 'cause', "
        "{get() { for (;;) {} }, configurable: true});"
    ),
    "error-name-getter": (
        "Object.defineProperty(Error, 'name', {get() { for (;;) {} }, configurable: true});"
    ),
    "prepare-stack-trace": "Error.prepareStackTrace = () => { for (;;) {} };",
    "additional-keys-getter": (
        "Object.defineProperty(Error.prototype, Symbol.for('errorAdditionalPropertyKeys'), "
        "{get() { for (;;) {} }, configurable: true});"
    ),
}

SHAPES = ["eval", "eval_module", "eval_async_sync_loop", "eval_async_after_await"]

_SCRIPT = textwrap.dedent(
    """
    import asyncio, time
    from pydeno import Runtime, RuntimeConfig, RuntimeTimeout
    rt = Runtime(RuntimeConfig(timeout=1.0))
    rt.eval({poison!r} + " 0")
    shape = {shape!r}
    start = time.monotonic()
    try:
        if shape == "eval":
            rt.eval("for (;;) {{}}")
        elif shape == "eval_module":
            rt.add_static_module("spin", "for (;;) {{}}")
            rt.eval_module("spin")
        elif shape == "eval_async_sync_loop":
            async def go():
                return await rt.eval_async("for (;;) {{}}", timeout=1.0)
            asyncio.run(go())
        else:
            async def go():
                return await rt.eval_async("(async () => {{ await null; for (;;) {{}} }})()", timeout=1.0)
            asyncio.run(go())
        outcome = "returned"
    except RuntimeTimeout:
        outcome = "timeout"
    except Exception as exc:
        outcome = type(exc).__name__ + ": " + str(exc)[:80]
    elapsed = time.monotonic() - start
    usable = rt.eval("1 + 1") == 2
    print(outcome, round(elapsed, 2), usable)
    """
)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("poison", list(POISON))
def test_a_timed_call_returns_on_runtime(poison: str, shape: str) -> None:
    try:
        done = subprocess.run(
            [sys.executable, "-c", _SCRIPT.format(poison=POISON[poison], shape=shape)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("the timed call was still running after 15 s") from None
    parts = done.stdout.split()
    assert len(parts) == 3, (done.stdout, done.stderr[-400:])
    outcome, elapsed, usable = parts
    assert outcome == "timeout", done.stdout
    assert float(elapsed) < 4.0
    assert usable == "True"


def test_a_looping_cause_on_a_thrown_error_reports_the_timeout() -> None:
    """Converting a guest's own thrown error ran past the deadline and came back as the guest's
    error rather than the timeout."""
    script = textwrap.dedent(
        """
        from pydeno import Runtime, RuntimeConfig, RuntimeTimeout
        rt = Runtime(RuntimeConfig(timeout=1.0))
        try:
            rt.eval("throw Object.defineProperty(new Error('x'), 'cause', "
                    "{get() { for (;;) {} }})")
        except RuntimeTimeout:
            print("timeout")
        except Exception as exc:
            print(type(exc).__name__)
        """
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("still running after 15 s") from None
    assert done.stdout.strip() == "timeout", (done.stdout, done.stderr[-400:])


ISOLATED_CASES = [
    ("constructor-getter", "for (;;) {}", False),
    ("cause-getter", "(async () => { await null; for (;;) {} })()", True),
    ("prepare-stack-trace", "for (;;) {}", True),
]


@pytest.mark.parametrize(("poison", "code", "is_async"), ISOLATED_CASES)
async def test_a_timed_call_returns_on_the_isolated_worker(
    poison: str, code: str, is_async: bool
) -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=1.0)) as rt:
        rt.eval(POISON[poison] + " 0")
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout):
            if is_async:
                await rt.eval_async(code, timeout=1.0)
            else:
                rt.eval(code)
        assert time.monotonic() - start < 5.0
        assert rt.eval("1 + 1") == 2


def test_an_ordinary_timeout_still_reports_a_timeout_and_leaves_the_runtime_usable() -> (
    None
):
    from pydeno import Runtime

    with Runtime(RuntimeConfig(timeout=0.5)) as rt:
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout):
            rt.eval("for (;;) {}")
        assert time.monotonic() - start < 2.0
        assert rt.eval("1 + 1") == 2
        with pytest.raises(Exception, match="boom"):
            rt.eval("throw new Error('boom')")
