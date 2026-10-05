"""Objects used from inside a host function (issue #58).

A host function bound with `bind_function` runs on the runtime's own thread. Two things a host
function could do there used to abort the whole process instead of raising:

- return a `PyStreamSource` (from `Runtime.stream_from_async_iterable`): the object was tied to
  the thread that created it, and converting the return value on the runtime thread failed a
  thread check inside Rust;
- call a `Runtime` method (`rt.eval`, ...) on the runtime that is calling it: `Runtime` is tied
  to its creating thread, and the failed check came back out of the host call as a Rust panic.

A stream source can now be returned from a host function and read by the guest. Calling the
`Runtime` from inside its own host function raises a `RuntimeError` in the guest, which the guest
can catch, and the runtime stays usable.

Each case runs in a child process so a regression shows up as a failed test, not a dead pytest.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_CHILD = r"""
import asyncio
import sys

from pydeno import Runtime

case = sys.argv[1]


async def gen():
    for i in range(3):
        yield i


READ_ALL = '''(async () => {
    const s = await give();
    const reader = s.getReader();
    const out = [];
    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        out.push(value);
    }
    return out;
})()'''


async def stream_case(async_tool):
    with Runtime() as rt:
        src = rt.stream_from_async_iterable(gen())
        if async_tool:
            async def give():
                return src
        else:
            def give():
                return src
        rt.bind_function("give", give)
        print("RESULT", await rt.eval_async(READ_ALL, timeout=10))
        print("AFTER", rt.eval("1 + 1"))


if case == "reenter":
    with Runtime() as rt:
        rt.bind_function("inner", lambda: rt.eval("1"))
        try:
            rt.eval("inner()")
            print("RESULT no error")
        except Exception as exc:
            print("ERROR", type(exc).__name__, exc)
        print("AFTER", rt.eval("1 + 1"))
elif case == "reenter_caught_by_guest":
    with Runtime() as rt:
        rt.bind_function("inner", lambda: rt.eval("1"))
        print("RESULT", rt.eval("try { inner(); 'no error' } catch (e) { e.name }"))
        print("AFTER", rt.eval("1 + 1"))
elif case == "stream_sync_tool":
    asyncio.run(stream_case(False))
elif case == "stream_async_tool":
    asyncio.run(stream_case(True))
"""


def _child(case: str) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, case],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"child died ({proc.returncode}): {proc.stderr[-800:]}"
    return proc.stdout.strip().splitlines()


def test_calling_the_runtime_from_its_own_host_function_raises() -> None:
    lines = _child("reenter")
    assert lines[0].startswith("ERROR "), lines
    assert "RuntimeError" in lines[0]
    assert "runtime thread" in lines[0]
    assert lines[-1] == "AFTER 2"


def test_the_guest_can_catch_the_reentry_error() -> None:
    lines = _child("reenter_caught_by_guest")
    assert lines == ["RESULT RuntimeError", "AFTER 2"]


@pytest.mark.parametrize("case", ["stream_sync_tool", "stream_async_tool"])
def test_a_host_function_can_return_a_stream_source(case: str) -> None:
    lines = _child(case)
    assert lines == ["RESULT [0, 1, 2]", "AFTER 2"]
