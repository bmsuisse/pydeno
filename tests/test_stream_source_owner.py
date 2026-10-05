"""A stream source belongs to the runtime that created it (issue #98).

Stream ids are allocated per runtime, so the first source of every runtime has the same id. A
source from runtime B handed to runtime A (returned by A's host function, passed to one of A's
functions from the main thread, bound into A, or yielded by one of A's streams) used to be read
through A's own stream of the same id: A's guest silently received A's data. The transfer is now
refused with a fixed error, also once the owning runtime is closed. The same runtime is
unaffected, and so is `IsolatedRuntime`.

Each case runs in a child process with a hard timeout, so a regression shows up as a failed test,
not a dead or hung pytest.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

REFUSED = "this stream source belongs to a different runtime"
CLOSED = "the runtime that created this stream source has been closed"

_CHILD = r"""
import asyncio
import sys

from pydeno import IsolatedRuntime, Runtime

case = sys.argv[1]


def gen(tag):
    async def it():
        for i in range(3):
            yield f"{tag}{i}"
    return it()


READ_GIVEN = '''(async () => {
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

READ_GLOBAL = '''(async () => {
    const reader = globalThis.s.getReader();
    const out = [];
    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        out.push(value);
    }
    return out;
})()'''


async def report(label, coro):
    try:
        print(label, "OK", await coro)
    except Exception as exc:
        print(label, "ERROR", type(exc).__name__, exc)


def report_sync(label, fn):
    try:
        print(label, "OK", fn())
    except Exception as exc:
        print(label, "ERROR", type(exc).__name__, exc)


async def cross_callback(async_tool):
    with Runtime() as a, Runtime() as b:
        src_a = a.stream_from_async_iterable(gen("a"))
        src_b = b.stream_from_async_iterable(gen("b"))
        if async_tool:
            async def give_a():
                return src_b
            async def give_b():
                return src_a
        else:
            def give_a():
                return src_b
            def give_b():
                return src_a
        a.bind_function("give", give_a)
        b.bind_function("give", give_b)
        await report("A", a.eval_async(READ_GIVEN, timeout=10))
        await report("B", b.eval_async(READ_GIVEN, timeout=10))
        # Each runtime still reads its own source.
        a.bind_function("give", lambda: src_a)
        b.bind_function("give", lambda: src_b)
        await report("A_OWN", a.eval_async(READ_GIVEN, timeout=10))
        await report("B_OWN", b.eval_async(READ_GIVEN, timeout=10))
        print("AFTER", a.eval("1 + 1"), b.eval("2 + 2"))


async def main_thread_setter():
    with Runtime() as a, Runtime() as b:
        src_a = a.stream_from_async_iterable(gen("a"))
        src_b = b.stream_from_async_iterable(gen("b"))
        setter = a.eval("(s) => { globalThis.s = s; return 'set'; }")
        report_sync("SET", lambda: setter(src_b))
        report_sync("BIND", lambda: a.bind_object("cfg", {"s": src_b}))
        report_sync("SET_OWN", lambda: setter(src_a))
        await report("READ_OWN", a.eval_async(READ_GLOBAL, timeout=10))
        print("AFTER", a.eval("1 + 1"))


async def async_setter():
    with Runtime() as a, Runtime() as b:
        src_b = b.stream_from_async_iterable(gen("b"))
        setter = a.eval("(s) => { globalThis.s = s; return 'set'; }")
        async def call(value):
            # The refusal may come from the call itself or from awaiting it.
            return await setter.call_async(value)

        await report("SET_ASYNC", call(src_b))
        src_a = a.stream_from_async_iterable(gen("a"))
        await report("SET_ASYNC_OWN", call(src_a))
        await report("READ_OWN", a.eval_async(READ_GLOBAL, timeout=10))
        print("AFTER", a.eval("1 + 1"))


async def nested_chunk():
    with Runtime() as a, Runtime() as b:
        src_b = b.stream_from_async_iterable(gen("b"))

        async def outer():
            yield src_b

        src_a = a.stream_from_async_iterable(outer())
        a.bind_function("give", lambda: src_a)
        await report("NESTED", a.eval_async(READ_GIVEN, timeout=10))
        print("AFTER", a.eval("1 + 1"))


async def after_close():
    with Runtime() as a:
        b = Runtime()
        src_b = b.stream_from_async_iterable(gen("b"))
        b.close()
        setter = a.eval("(s) => { globalThis.s = s; return 'set'; }")
        report_sync("SET_CLOSED", lambda: setter(src_b))
        a.bind_function("give", lambda: src_b)
        await report("GIVE_CLOSED", a.eval_async(READ_GIVEN, timeout=10))
        print("AFTER", a.eval("1 + 1"))


def isolated():
    with IsolatedRuntime() as rt:
        rt.bind_function("add", lambda x, y: x + y)
        print("ISO", rt.eval("add(2, 3)"))


if case == "cross_sync":
    asyncio.run(cross_callback(False))
elif case == "cross_async":
    asyncio.run(cross_callback(True))
elif case == "main_thread":
    asyncio.run(main_thread_setter())
elif case == "async_setter":
    asyncio.run(async_setter())
elif case == "nested_chunk":
    asyncio.run(nested_chunk())
elif case == "after_close":
    asyncio.run(after_close())
elif case == "isolated":
    isolated()
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


def _refused(line: str, label: str, text: str = REFUSED) -> None:
    assert line.startswith(f"{label} ERROR "), line
    assert text in line, line
    for internal in ("PyStreamSource", "_pydeno", "owner", "stream id"):
        assert internal not in line, line


@pytest.mark.parametrize("case", ["cross_sync", "cross_async"])
def test_a_source_returned_by_another_runtimes_host_function_is_refused(
    case: str,
) -> None:
    lines = _child(case)
    assert len(lines) == 5, lines
    _refused(lines[0], "A")
    _refused(lines[1], "B")
    assert lines[2] == "A_OWN OK ['a0', 'a1', 'a2']"
    assert lines[3] == "B_OWN OK ['b0', 'b1', 'b2']"
    assert lines[4] == "AFTER 2 4"


def test_a_source_from_another_runtime_is_refused_on_the_main_thread() -> None:
    lines = _child("main_thread")
    assert len(lines) == 5, lines
    _refused(lines[0], "SET")
    _refused(lines[1], "BIND")
    assert lines[2] == "SET_OWN OK set"
    assert lines[3] == "READ_OWN OK ['a0', 'a1', 'a2']"
    assert lines[4] == "AFTER 2"


def test_a_source_from_another_runtime_is_refused_on_the_async_path() -> None:
    lines = _child("async_setter")
    assert len(lines) == 4, lines
    _refused(lines[0], "SET_ASYNC")
    assert lines[1] == "SET_ASYNC_OWN OK set"
    assert lines[2] == "READ_OWN OK ['a0', 'a1', 'a2']"
    assert lines[3] == "AFTER 2"


def test_a_source_from_another_runtime_yielded_as_a_chunk_is_refused() -> None:
    lines = _child("nested_chunk")
    assert len(lines) == 2, lines
    _refused(lines[0], "NESTED")
    assert lines[1] == "AFTER 2"


def test_a_source_from_a_closed_runtime_is_refused() -> None:
    lines = _child("after_close")
    assert len(lines) == 3, lines
    _refused(lines[0], "SET_CLOSED", CLOSED)
    _refused(lines[1], "GIVE_CLOSED", CLOSED)
    assert lines[2] == "AFTER 2"


def test_isolated_runtime_is_unaffected() -> None:
    assert _child("isolated") == ["ISO 5"]
