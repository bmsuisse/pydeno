"""`ToolBridge` on an `IsolatedRuntime`: the same budget, name check and error behaviour as on a
plain `Runtime`, with the tools themselves still running in the parent."""

from __future__ import annotations

import asyncio

import pytest

from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    Runtime,
    RuntimeConfig,
    ToolBridge,
    ToolError,
)


def _iso(**kwargs: object) -> IsolatedRuntime:
    return IsolatedRuntime(RuntimeConfig(timeout=10.0), **kwargs)  # type: ignore[arg-type]


def _both() -> list[object]:
    return [Runtime(RuntimeConfig(timeout=10.0)), _iso()]


class TestSameBehaviourAsInProcess:
    def test_a_namespaced_tool_returns_its_value(self) -> None:
        for rt in _both():
            bridge = ToolBridge({"add": lambda a, b: a + b}, namespace="tools")
            bridge.attach(rt)
            assert rt.eval("tools.add(40, 2)") == 42  # type: ignore[attr-defined]
            if isinstance(rt, IsolatedRuntime):
                rt.close()

    def test_flat_tools_without_a_namespace(self) -> None:
        for rt in _both():
            ToolBridge({"shout": lambda s: s.upper()}, namespace=None).attach(rt)
            assert rt.eval("shout('hi')") == "HI"  # type: ignore[attr-defined]
            if isinstance(rt, IsolatedRuntime):
                rt.close()

    def test_the_budget_is_shared_across_tools(self) -> None:
        for rt in _both():
            bridge = ToolBridge(
                {"a": lambda: "A", "b": lambda: "B"}, namespace="t", max_calls=3
            )
            bridge.attach(rt)
            assert rt.eval("t.a() + t.b() + t.a()") == "ABA"  # type: ignore[attr-defined]
            with pytest.raises(JavaScriptError):
                rt.eval("t.b()")  # type: ignore[attr-defined]
            assert bridge.calls_made == 3
            assert bridge.calls_remaining == 0
            if isinstance(rt, IsolatedRuntime):
                rt.close()

    def test_budget_exhaustion_is_catchable_by_name_in_js(self) -> None:
        for rt in _both():
            ToolBridge({"x": lambda: 1}, namespace="t", max_calls=1).attach(rt)
            rt.eval("t.x()")  # type: ignore[attr-defined]
            name = rt.eval(  # type: ignore[attr-defined]
                "try { t.x(); 'no' } catch (e) { e.name }"
            )
            assert name == "ToolBudgetError"
            if isinstance(rt, IsolatedRuntime):
                rt.close()

    def test_a_tool_error_is_catchable_by_name_in_js(self) -> None:
        def boom() -> None:
            raise ToolError("nope")

        for rt in _both():
            ToolBridge({"boom": boom}, namespace="t").attach(rt)
            assert rt.eval("try { t.boom() } catch (e) { e.name }") == "ToolError"  # type: ignore[attr-defined]
            if isinstance(rt, IsolatedRuntime):
                rt.close()

    def test_a_missing_tool_is_a_type_error_in_js(self) -> None:
        for rt in _both():
            ToolBridge({"real": lambda: 1}, namespace="t").attach(rt)
            assert (
                rt.eval("try { t.fake() } catch (e) { e.constructor.name }")
                == "TypeError"
            )  # type: ignore[attr-defined]
            if isinstance(rt, IsolatedRuntime):
                rt.close()

    def test_construction_rejects_hostile_names_before_anything_is_bound(self) -> None:
        for bad in ("__proto__", "constructor", "has space", "a.b", "1x", "", "x;y"):
            with pytest.raises((ValueError, TypeError)):
                ToolBridge({bad: lambda: 1})


class _Custom(Exception):
    pass


class TestHostExceptionsLookTheSameToTheGuest:
    """A host function that raises reaches guest JS as a catchable error. The guest can branch
    on its `name` and read its `message`; that must not change because the function now runs
    on the other side of a process boundary."""

    @pytest.mark.parametrize(
        "make",
        [
            lambda: ValueError("bad value"),
            lambda: KeyError("missing"),
            lambda: TypeError("wrong type"),
            lambda: RuntimeError("runtime"),
            lambda: ZeroDivisionError("division by zero"),
            lambda: _Custom("custom failure"),
            lambda: ToolError("tool failed"),
            lambda: ValueError(""),
            lambda: ValueError("héllo wörld 日本語 😀"),
            lambda: ValueError("line one\nline two"),
            lambda: ValueError("x" * 5000),
            lambda: OSError(13, "permission denied"),
            lambda: AssertionError("assertion"),
            lambda: NotImplementedError("todo"),
        ],
    )
    def test_name_and_message_match_the_in_process_runtime(self, make) -> None:  # type: ignore[no-untyped-def]
        def fn() -> None:
            raise make()

        probe = "try { fn(); 'returned' } catch (e) { [e.name, e.message] }"
        plain = Runtime(RuntimeConfig(timeout=10.0))
        plain.bind_function("fn", fn)
        expected = plain.eval(probe)
        with _iso(redact_host_errors=False) as rt:
            rt.bind_function("fn", fn)
            assert rt.eval(probe) == expected

    def test_the_same_for_an_async_host_function(self) -> None:
        async def fn() -> None:
            raise _Custom("async failure")

        probe = "fn().then(() => 'returned', e => [e.name, e.message])"

        async def go() -> tuple[object, object]:
            plain = Runtime(RuntimeConfig(timeout=10.0))
            plain.bind_function("fn", fn)
            expected = await plain.eval_async(probe)
            with _iso(redact_host_errors=False) as rt:
                rt.bind_function("fn", fn)
                return expected, await rt.eval_async(probe)

        expected, actual = asyncio.run(go())
        assert actual == expected
        assert actual[0] == "_Custom"

    def test_a_class_name_that_is_not_an_identifier_cannot_inject_anything(
        self,
    ) -> None:
        """The exception class name crosses the boundary as text and is rebuilt as a class on the
        worker side; it must be treated as untrusted text, not code."""
        evil = type("x; globalThis.pwned = 1; //", (Exception,), {})

        def fn() -> None:
            raise evil("boom")

        with _iso(redact_host_errors=False) as rt:
            rt.bind_function("fn", fn)
            name = rt.eval("try { fn() } catch (e) { e.name }")
            assert isinstance(name, str)
            assert rt.eval("typeof globalThis.pwned") == "undefined"

    def test_a_flood_of_distinct_exception_classes_cannot_grow_the_worker_without_bound(
        self,
    ) -> None:
        classes = [type(f"Err{i}", (Exception,), {}) for i in range(600)]
        counter = {"n": 0}

        def fn() -> None:
            counter["n"] += 1
            raise classes[counter["n"] % len(classes)]("x")

        with _iso(redact_host_errors=False) as rt:
            rt.bind_function("fn", fn)
            assert (
                rt.eval(
                    "let n = 0; for (let i = 0; i < 600; i++) { try { fn() } catch (e) { n++ } } n"
                )
                == 600
            )


class TestOnTheIsolatedRuntime:
    def test_the_tool_runs_in_the_parent_not_the_worker(self) -> None:
        import os

        with _iso() as rt:
            ToolBridge({"whoami": lambda: os.getpid()}, namespace="t").attach(rt)
            assert rt.eval("t.whoami()") == os.getpid()
            assert rt._proc.pid != os.getpid()  # noqa: SLF001

    def test_a_tool_can_reach_what_the_guest_cannot(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The point of a tool: the host decides what authority crosses. The sandboxed worker
        cannot read this file; the tool can, and hands over only what it chooses."""
        secret = tmp_path / "notes.txt"
        secret.write_text("line one\nline two\n")

        def read_first_line(name: str) -> str:
            if name != "notes":  # the host validates the argument, as it must
                raise ToolError("unknown note")
            return secret.read_text().splitlines()[0]

        with _iso() as rt:
            ToolBridge({"read": read_first_line}, namespace="t").attach(rt)
            assert rt.eval("t.read('notes')") == "line one"
            assert (
                rt.eval("try { t.read('../../etc/passwd') } catch (e) { e.name }")
                == "ToolError"
            )

    def test_async_tools_run_concurrently(self) -> None:
        async def slow(n: int) -> int:
            await asyncio.sleep(0.4)
            return n

        async def go() -> list[int]:
            with _iso() as rt:
                ToolBridge({"slow": slow}, namespace="t").attach(rt)
                return await rt.eval_async(
                    "Promise.all([t.slow(1), t.slow(2), t.slow(3)])"
                )

        import time

        start = time.monotonic()
        assert asyncio.run(go()) == [1, 2, 3]
        assert time.monotonic() - start < 1.2

    def test_detach_revokes_in_the_worker_and_forgets_in_the_parent(self) -> None:
        with _iso() as rt:
            bridge = ToolBridge({"a": lambda: 1, "b": lambda: 2}, namespace="t")
            bridge.attach(rt)
            assert rt.eval("t.a() + t.b()") == 3
            handlers_before = len(rt._handlers)  # noqa: SLF001
            assert bridge.detach(rt) == 2
            assert len(rt._handlers) == handlers_before - 2  # noqa: SLF001
            with pytest.raises(JavaScriptError):
                rt.eval("t.a()")

    def test_two_bridges_are_two_capability_sets(self) -> None:
        with _iso() as rt:
            one = ToolBridge({"x": lambda: "one"}, namespace="one")
            two = ToolBridge({"x": lambda: "two"}, namespace="two")
            one.attach(rt)
            two.attach(rt)
            one.detach(rt)
            assert rt.eval("two.x()") == "two"
            with pytest.raises(JavaScriptError):
                rt.eval("one.x()")

    def test_the_budget_survives_a_failed_tool_call(self) -> None:
        def flaky(n: int) -> int:
            if n == 0:
                raise ToolError("zero")
            return n

        with _iso() as rt:
            bridge = ToolBridge({"f": flaky}, namespace="t", max_calls=3)
            bridge.attach(rt)
            assert rt.eval("try { t.f(0) } catch (e) { 'failed' }") == "failed"
            assert rt.eval("t.f(5)") == 5
            assert bridge.calls_made == 2

    def test_reset_budget_allows_more_calls(self) -> None:
        with _iso() as rt:
            bridge = ToolBridge({"x": lambda: 1}, namespace="t", max_calls=1)
            bridge.attach(rt)
            rt.eval("t.x()")
            with pytest.raises(JavaScriptError):
                rt.eval("t.x()")
            bridge.reset_budget()
            assert rt.eval("t.x()") == 1

    def test_the_budget_and_max_host_calls_are_independent(self) -> None:
        """`ToolBridge` budgets tools; `max_host_calls` budgets every callback, tools included."""
        rt = IsolatedRuntime(RuntimeConfig(timeout=10.0), max_host_calls=5)
        ToolBridge({"x": lambda: 1}, namespace="t", max_calls=1000).attach(rt)
        from pydeno import WorkerCrashed

        with pytest.raises(WorkerCrashed, match="max_host_calls"):
            rt.eval("for (let i = 0; i < 100; i++) t.x()")

    def test_attaching_to_something_that_is_not_a_runtime_is_refused(self) -> None:
        with pytest.raises(TypeError, match="Runtime"):
            ToolBridge({"x": lambda: 1}).attach(object())

    def test_a_tool_returning_an_unsendable_value_is_an_error_not_a_crash(self) -> None:
        with _iso() as rt:
            ToolBridge({"obj": lambda: object()}, namespace="t").attach(rt)
            assert (
                rt.eval("try { t.obj(); 'got' } catch (e) { 'refused' }") == "refused"
            )
            assert rt.eval("1 + 1") == 2

    def test_tool_arguments_arrive_exactly_as_they_do_in_process(self) -> None:
        """Whatever pydeno's own conversion makes of each JS value (JS `null` and `undefined`
        both arrive as `JsUndefined`, for instance), the process boundary must not change it."""
        js = "t.keep(1, 'two', [3], {four: 4}, null, undefined, true, 2n ** 70n, new Uint8Array([9]))"
        seen: dict[str, list[object]] = {"in": [], "iso": []}
        plain = Runtime(RuntimeConfig(timeout=10.0))
        ToolBridge({"keep": lambda *a: seen["in"].append(a)}, namespace="t").attach(
            plain
        )
        plain.eval(js)
        with _iso() as rt:
            ToolBridge(
                {"keep": lambda *a: seen["iso"].append(a)}, namespace="t"
            ).attach(rt)
            rt.eval(js)
        assert seen["iso"] == seen["in"]
        assert seen["iso"][0][0] == 1 and seen["iso"][0][7] == 2**70
