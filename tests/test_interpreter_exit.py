"""Interpreter exit and stream cancellation: each case runs in a child with a hard timeout.

pydeno registers an `atexit` hook at import that stops background threads from entering Python
while the interpreter finalizes. Work started after that hook (by an `atexit` handler registered
before `import pydeno`, which runs later, or after a manual `atexit._run_exitfuncs()`) must still
complete: refusing its results would hang the handler forever. A hang shows up here as a
`TimeoutExpired`, so a regression fails instead of stalling the suite.

Closing a stream source hands `aclose()` to the source's loop; what that raises (a cancelled
read in flight, a generator whose `finally` raises) is the closer's to ignore, not a stray
"Task exception was never retrieved" traceback on stderr.
"""

from __future__ import annotations

import subprocess
import sys

_TIMEOUT = 30

_CASES = {
    "atexit_handler_before_import": r"""
import asyncio
import atexit

state = {}


def late():
    rt = state["rt"]

    async def go():
        return await rt.eval_async("Promise.resolve(41 + 1)")

    try:
        print(asyncio.run(go()), flush=True)
    finally:
        rt.close()


atexit.register(late)  # registered first, so it runs after pydeno's hook
import pydeno  # noqa: E402

state["rt"] = pydeno.Runtime()
""",
    "run_exitfuncs_then_eval_async": r"""
import asyncio
import atexit

import pydeno

atexit._run_exitfuncs()
with pydeno.Runtime() as rt:
    async def go():
        return await rt.eval_async("Promise.resolve(7)")

    print(asyncio.run(go()), flush=True)
""",
    "close_with_read_in_flight": r"""
import asyncio
import sys

from pydeno import Runtime

how = sys.argv[1]
log = []


async def gen():
    try:
        yield 0
        await asyncio.sleep(0.5)
        yield 1
    finally:
        log.append("finally")


async def main():
    rt = Runtime()
    src = rt.stream_from_async_iterable(gen())
    rt.eval("(s) => { globalThis.s = s; }")(src)
    first = await rt.eval_async(
        "(async () => { globalThis.r = s.getReader(); return (await r.read()).value })()"
    )
    rt.eval("globalThis.p = r.read(); 1")  # second pull: the generator is now sleeping
    await asyncio.sleep(0.1)
    if how == "source":
        src.close()
    rt.close()
    await asyncio.sleep(0.8)
    print(first, log, flush=True)


asyncio.run(main())
""",
    "finally_raises": r"""
import asyncio

from pydeno import Runtime

log = []


async def gen():
    try:
        for i in range(10):
            yield i
    finally:
        log.append("finally")
        raise ValueError("boom in finally")


async def main():
    rt = Runtime()
    src = rt.stream_from_async_iterable(gen())
    rt.eval("(s) => { globalThis.s = s; }")(src)
    first = await rt.eval_async("(async () => (await s.getReader().read()).value)()")
    rt.close()
    await asyncio.sleep(0.3)
    print(first, log, flush=True)


asyncio.run(main())
""",
}


def _run(case: str, *args: str) -> tuple[str, str]:
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _CASES[case], *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout.strip(), proc.stderr


def test_eval_async_in_an_atexit_handler_registered_before_import_completes() -> None:
    stdout, _ = _run("atexit_handler_before_import")
    assert stdout == "42"


def test_eval_async_after_a_manual_run_of_the_exit_functions_completes() -> None:
    stdout, _ = _run("run_exitfuncs_then_eval_async")
    assert stdout == "7"


def _assert_no_stray_traceback(stderr: str) -> None:
    assert "Task exception was never retrieved" not in stderr, stderr
    assert "Traceback" not in stderr, stderr


def test_closing_a_runtime_with_a_read_in_flight_leaves_no_stray_traceback() -> None:
    stdout, stderr = _run("close_with_read_in_flight", "runtime")
    assert stdout == "0 ['finally']"
    _assert_no_stray_traceback(stderr)


def test_closing_a_source_with_a_read_in_flight_leaves_no_stray_traceback() -> None:
    stdout, stderr = _run("close_with_read_in_flight", "source")
    assert stdout == "0 ['finally']"
    _assert_no_stray_traceback(stderr)


def test_a_generator_whose_finally_raises_leaves_no_stray_traceback() -> None:
    stdout, stderr = _run("finally_raises")
    assert stdout == "0 ['finally']"
    _assert_no_stray_traceback(stderr)
