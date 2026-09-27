"""Regression cases found during the 0.4.3 repository review."""

import asyncio
import subprocess
import sys
import textwrap

import pytest

from pydeno import JavaScriptError, Runtime, RuntimeConfig, ToolBridge


@pytest.mark.parametrize("entry", ["eval", "module", "function"])
async def test_async_conversion_error_reaches_caller(entry):
    with Runtime() as rt:
        if entry == "eval":
            result = rt.eval_async("new Set([{}])")
        elif entry == "module":
            rt.add_static_module("bad", "export const value = new Set([{}]);")
            result = rt.eval_module_async("bad")
        else:
            function = rt.eval("() => new Set([{}])")
            result = function.call_async()
        with pytest.raises(TypeError, match="unhashable"):
            await asyncio.wait_for(result, timeout=2)
        assert await rt.eval_async("42") == 42


def test_bridge_detach_preserves_other_runtime_capabilities():
    bridge = ToolBridge({"answer": lambda: 42})
    with Runtime() as first, Runtime() as second:
        bridge.attach(first)
        bridge.attach(second)
        assert bridge.detach(first) == 1
        assert second.eval("tools.answer()") == 42
        assert bridge.detach(second) == 1
        with pytest.raises(JavaScriptError):
            second.eval("tools.answer()")


@pytest.mark.parametrize("name", ["tool\n", "tools\n"])
def test_bridge_rejects_trailing_newline(name):
    with pytest.raises(ValueError):
        ToolBridge({name: lambda: None})
    with pytest.raises(ValueError):
        ToolBridge({}, namespace=name)


@pytest.mark.parametrize("value", [float(2**64), 1e18])
def test_timeout_rejects_unrepresentable_deadline(value):
    with pytest.raises(ValueError, match="large"):
        RuntimeConfig(timeout=value)
    with Runtime() as rt:
        with pytest.raises(ValueError, match="large"):
            rt.eval_async("42", timeout=value)


def test_bind_object_setter_can_call_python_without_deadlock():
    code = textwrap.dedent("""
        from pydeno import Runtime
        with Runtime() as rt:
            seen = []
            rt.bind_function("record", lambda: seen.append("called"))
            rt.eval('Object.defineProperty(globalThis, "target", {set(v) {record();}})')
            rt.bind_object("target", {"value": 42})
            assert seen == ["called"]
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr


def test_python_dict_keeps_proto_as_own_data():
    with Runtime() as rt:
        inspect = rt.eval("""value => [
            Object.hasOwn(value, "__proto__"),
            Object.getPrototypeOf(value) === Object.prototype,
            value.__proto__.answer
        ]""")
        assert inspect({"__proto__": {"answer": 42}}) == [True, True, 42]


@pytest.mark.parametrize("operation", ["close", "finalize"])
def test_stream_cancellation_can_call_python_without_deadlock(operation):
    code = textwrap.dedent(f"""
        import gc
        from pydeno import Runtime
        with Runtime() as rt:
            seen = []
            rt.bind_function("record", lambda: seen.append("called"))
            stream = rt.eval("new ReadableStream({{cancel() {{record();}}}})")
            if {operation!r} == "close":
                stream.close()
            else:
                del stream
                gc.collect()
            assert seen == ["called"]
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "operation",
    ["register", "bind", "revoke", "resolver", "loader", "module", "terminate"],
)
def test_runtime_control_calls_release_gil(operation):
    code = textwrap.dedent(f"""
        import asyncio
        import threading
        from pydeno import Runtime

        async def main():
            with Runtime() as rt:
                entered = threading.Event()
                release = threading.Event()
                def callback():
                    entered.set()
                    assert release.wait(2)
                    return 42
                token = rt.bind_function("callback", callback)
                pending = asyncio.create_task(rt.eval_async("callback()"))
                assert entered.wait(2)
                timer = threading.Timer(0.05, release.set)
                timer.start()
                operation = {operation!r}
                if operation == "register":
                    rt.register_op("other", lambda: 1)
                elif operation == "bind":
                    rt.bind_function("other", lambda: 1)
                elif operation == "revoke":
                    rt.revoke_op(token)
                elif operation == "resolver":
                    rt.set_module_resolver(lambda s, r: None)
                elif operation == "loader":
                    rt.set_module_loader(lambda s: "export default 1")
                elif operation == "module":
                    rt.add_static_module("other", "export default 1")
                else:
                    try:
                        rt.terminate()
                    except RuntimeError:
                        pass
                timer.join()
                try:
                    await pending
                except Exception:
                    assert operation == "terminate"
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr


def test_bind_object_snapshots_input_before_releasing_gil():
    code = textwrap.dedent("""
        import asyncio
        import threading
        from pydeno import Runtime

        async def main():
            with Runtime() as rt:
                entered = threading.Event()
                release = threading.Event()
                def callback():
                    entered.set()
                    assert release.wait(2)
                rt.bind_function("callback", callback)
                pending = asyncio.create_task(rt.eval_async("callback()"))
                assert entered.wait(2)
                values = {"one": lambda: 1, "two": 2}
                def mutate():
                    values["three"] = 3
                    release.set()
                timer = threading.Timer(0.05, mutate)
                timer.start()
                rt.bind_object("values", values)
                timer.join()
                await pending
                assert rt.eval("values.one() + values.two") == 3
                assert rt.eval("Object.keys(values)") == ["one", "two"]
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
