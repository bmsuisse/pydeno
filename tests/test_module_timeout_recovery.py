"""The runtime stays usable after a module evaluation times out.

A module with top-level `await` whose evaluation is cut short by a deadline (or that awaits a
promise that never settles) leaves a module evaluation deno_core will never see finish. Every
later event-loop poll reported it as "Top-level await promise never resolved", and the
dispatcher handled that report by looping straight back to the poll: commands and termination
requests were never serviced again and the runtime thread spun at 100% CPU.

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

SPIN_AFTER_AWAIT = "export const c = 3; await null; for (;;) {}"
SPIN_AFTER_MICROTASK = (
    "export const c = 3; await new Promise((r) => queueMicrotask(r)); for (;;) {}"
)
NEVER_SETTLES = "export const c = 3; await new Promise(() => {});"

# (module body, how it is evaluated)
SHAPES = {
    "async-spin-after-await": (SPIN_AFTER_AWAIT, "async"),
    "async-spin-after-microtask": (SPIN_AFTER_MICROTASK, "async"),
    "async-never-settles": (NEVER_SETTLES, "async"),
    "sync-spin-after-await": (SPIN_AFTER_AWAIT, "sync"),
    "sync-never-settles": (NEVER_SETTLES, "sync"),
    "dynamic-import-spin": (SPIN_AFTER_AWAIT, "import"),
    "dynamic-import-never-settles": (NEVER_SETTLES, "import"),
}

_SCRIPT = textwrap.dedent(
    """
    import asyncio, time
    from pydeno import Runtime, RuntimeConfig, RuntimeTerminated

    BODY = {body!r}
    HOW = {how!r}
    ROUNDS = {rounds!r}

    async def evaluate(rt, name):
        try:
            if HOW == "async":
                await rt.eval_module_async(name, timeout=0.2)
            elif HOW == "sync":
                rt.eval_module(name)
            else:
                code = "(async () => {{ await import(%r) }})()" % name
                await rt.eval_async(code, timeout=0.2)
        except Exception:
            pass  # a timeout or a stalled-await error; what matters is what comes next

    async def one_round(i, last):
        # One main module per runtime (a deno_core rule), so each round is a fresh runtime; a
        # dynamic import can repeat inside one.
        rt = Runtime(RuntimeConfig(timeout=0.2))
        names = ["m%d" % k for k in range(5 if HOW == "import" else 1)]
        for name in names:
            rt.add_static_module(name, BODY)
            await evaluate(rt, name)
            start = time.monotonic()
            assert rt.eval("40 + 2") == 42
            assert time.monotonic() - start < 2.0, "slow command after round %d" % i
        # a later, ordinary module still evaluates
        rt.add_static_module("plain", "export const y = await Promise.resolve(2);")
        assert await rt.eval_async("import('plain').then((m) => m.y)", timeout=2.0) == 2
        if not last:
            rt.close()
            return None
        # idle: the runtime thread must not spin
        cpu = time.process_time()
        time.sleep(1.0)
        idle_cpu = time.process_time() - cpu
        # an async job still completes normally
        assert await rt.eval_async("Promise.resolve(7)", timeout=2.0) == 7
        # and an off-thread termination is still observed
        handle = rt.termination_handle()
        handle.terminate()
        try:
            rt.eval("1")
            terminated = "not terminated"
        except RuntimeTerminated as exc:
            terminated = "timed out" if "timed out" in str(exc) else "terminated"
        except Exception as exc:
            terminated = type(exc).__name__
        rt.close()
        return idle_cpu, terminated

    async def main():
        for i in range(ROUNDS):
            result = await one_round(i, i == ROUNDS - 1)
        idle_cpu, terminated = result
        print("ok", round(idle_cpu, 2), terminated)

    asyncio.run(main())
    """
)


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_runtime_stays_usable_after_a_module_evaluation_times_out(
    shape: str,
) -> None:
    body, how = SHAPES[shape]
    try:
        done = subprocess.run(
            [sys.executable, "-c", _SCRIPT.format(body=body, how=how, rounds=50)],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("the runtime stopped answering") from None
    parts = done.stdout.split()
    assert len(parts) == 3 and parts[0] == "ok", (done.stdout, done.stderr[-600:])
    assert float(parts[1]) < 0.3, "the idle runtime thread is spinning"
    # A deadline that fired earlier must not be reported as the reason for this termination.
    assert parts[2] == "terminated", parts


@pytest.mark.parametrize("shape", ["async-spin-after-await", "sync-spin-after-await"])
async def test_the_isolated_worker_stays_usable_after_a_module_evaluation_times_out(
    shape: str,
) -> None:
    body, how = SHAPES[shape]
    with IsolatedRuntime(RuntimeConfig(timeout=0.5)) as rt:
        for i in range(5):
            rt.add_static_module(f"m{i}", body)
            with pytest.raises(Exception):  # noqa: B017 - a timeout, whatever the wording
                if how == "async":
                    await rt.eval_module_async(f"m{i}", timeout=0.5)
                else:
                    rt.eval_module(f"m{i}")
            start = time.monotonic()
            assert rt.eval("40 + 2") == 42
            assert time.monotonic() - start < 2.0


def test_re_evaluating_a_timed_out_module_explains_itself() -> None:
    """The second evaluation of a module whose first one was cut short used to fail with
    'Uncaught null'."""
    script = textwrap.dedent(
        """
        import asyncio
        from pydeno import Runtime, RuntimeConfig
        async def main():
            rt = Runtime(RuntimeConfig(timeout=0.2))
            rt.add_static_module("m", {body!r})
            for _ in range(2):
                try:
                    await rt.eval_module_async("m", timeout=0.2)
                except Exception as exc:
                    print(type(exc).__name__ + ": " + str(exc).replace(chr(10), " ")[:160])
        asyncio.run(main())
        """
    ).format(body="export const c = 3; for (;;) {}")
    try:
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("the runtime stopped answering") from None
    lines = done.stdout.strip().splitlines()
    assert len(lines) == 2, (done.stdout, done.stderr[-400:])
    assert lines[0].startswith("RuntimeTimeout"), lines
    assert "Uncaught null" not in lines[1], lines
    assert "m" in lines[1] and "evaluat" in lines[1], lines


def test_a_termination_after_a_timeout_reports_its_own_reason() -> None:
    from pydeno import Runtime, RuntimeTerminated

    with Runtime(RuntimeConfig(timeout=0.2)) as rt:
        with pytest.raises(RuntimeTimeout):
            rt.eval("for (;;) {}")
        rt.termination_handle().terminate()
        with pytest.raises(RuntimeTerminated) as info:
            rt.eval("1")
        assert "timed out" not in str(info.value)
