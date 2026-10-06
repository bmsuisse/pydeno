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


def collected_on_runtime_thread(kind):
    import gc

    class Holder:
        pass

    rt = Runtime()
    gc.disable()  # the cycle must be collected inside the host function, not before
    holder = Holder()
    holder.cycle = holder
    holder.value = rt.eval(
        "(x) => x + 1" if kind == "fn" else "new ReadableStream({start(c) { c.enqueue(1); }})"
    )
    del holder

    def collect():
        gc.collect()  # runs the wrapper's finalizer here, on the runtime thread
        return 1

    rt.bind_function("collect", collect)
    print("RESULT", rt.eval("collect()"))
    print("AFTER", rt.eval("1 + 1"))
    rt.close()
    print("CLOSED")


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
elif case in ("guest_text_eval", "guest_text_jsfn_call", "guest_text_jsfn_return"):
    with Runtime() as rt:
        fn = rt.eval("(x) => x + 1")
        tool = {
            "guest_text_eval": lambda: rt.eval("1"),
            "guest_text_jsfn_call": lambda: fn(1),
            "guest_text_jsfn_return": lambda: fn,
        }[case]
        rt.bind_function("inner", tool)
        text = rt.eval(
            "try { String(inner()) } catch (e) { e.name + ': ' + e.message + '\\n' + e.stack }"
        )
        print("RESULT", repr(text))
        print("AFTER", rt.eval("1 + 1"))
elif case == "stream_sync_tool":
    asyncio.run(stream_case(False))
elif case == "stream_async_tool":
    asyncio.run(stream_case(True))
elif case == "stream_fresh_unreferenced":
    async def fresh():
        with Runtime() as rt:
            async def give():
                # Nothing keeps this source alive once it is returned.
                return rt.stream_from_async_iterable(gen())
            rt.bind_function("give", give)
            try:
                print("RESULT", await rt.eval_async(READ_ALL, timeout=10))
            except Exception as exc:
                print("ERROR", type(exc).__name__, exc)
            print("AFTER", rt.eval("1 + 1"))
    asyncio.run(fresh())
elif case in ("collected_fn", "collected_stream"):
    collected_on_runtime_thread(case.removeprefix("collected_"))
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


@pytest.mark.parametrize(
    "case", ["guest_text_eval", "guest_text_jsfn_call", "guest_text_jsfn_return"]
)
def test_the_guest_error_text_names_no_rust_internals(case: str) -> None:
    """The panic text (Rust type paths, the failed assertion) goes to the log, not the guest."""
    lines = _child(case)
    assert lines[-1] == "AFTER 2"
    text = lines[0]
    assert "RuntimeError: this object cannot be used from the runtime thread" in text
    for internal in ("_pydeno::", "left == right", "unsendable", "ThreadId"):
        assert internal not in text, text


@pytest.mark.parametrize("case", ["stream_sync_tool", "stream_async_tool"])
def test_a_host_function_can_return_a_stream_source(case: str) -> None:
    lines = _child(case)
    assert lines == ["RESULT [0, 1, 2]", "AFTER 2"]


def test_a_stream_source_nobody_keeps_is_gone_before_the_guest_reads_it() -> None:
    """Pins current behaviour: the source's finalizer cancels the stream once the host drops its
    last reference, so a source created inside the tool and returned without being kept is gone
    by the time the guest reads it. The documented rule is to keep a reference until the guest is
    done. This must stay an error, never an abort."""
    lines = _child("stream_fresh_unreferenced")
    assert lines[0].startswith("ERROR JavaScriptError"), lines
    assert "Unknown Python stream id" in lines[0]
    assert lines[-1] == "AFTER 2"


@pytest.mark.parametrize("case", ["collected_fn", "collected_stream"])
def test_a_wrapper_collected_inside_a_host_function_does_not_hang(case: str) -> None:
    """A `JsFunction` or `JsStream` whose last reference is a cycle collected while a host
    function runs: its finalizer then runs on the runtime thread, where waiting for the runtime
    thread never ends (the stream finalizer hung the process; the function finalizer raised a
    panic). It now releases the handle without waiting, and the runtime stays usable."""
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, case],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"child died ({proc.returncode}): {proc.stderr[-800:]}"
    assert proc.stdout.strip().splitlines() == ["RESULT 1", "AFTER 2", "CLOSED"]
    assert "Panic" not in proc.stderr, proc.stderr[-800:]
