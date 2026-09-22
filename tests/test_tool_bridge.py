"""Tests for `pydeno.ToolBridge` -- "give this sandbox N callable tools
safely" out of the box.

`ToolBridge` is a pure-Python layer over `Runtime.bind_object`; it adds the
three things every embedder otherwise hand-rolls: a total call budget,
fail-closed name checking, and typed errors JS can branch on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pydeno import (
    JavaScriptError,
    Runtime,
    ToolBridge,
    ToolBudgetError,
    ToolError,
    ToolNotFoundError,
)


class TestBinding:
    def test_tools_are_callable_under_the_namespace(self) -> None:
        bridge = ToolBridge({"add": lambda a, b: a + b, "upper": str.upper})
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.add(2, 3)") == 5
            assert rt.eval("tools.upper('abc')") == "ABC"

    def test_custom_namespace(self) -> None:
        bridge = ToolBridge({"ping": lambda: "pong"}, namespace="host")
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("host.ping()") == "pong"
            assert rt.eval("typeof tools") == "undefined"

    def test_namespace_none_installs_bare_globals(self) -> None:
        bridge = ToolBridge({"ping": lambda: "pong"}, namespace=None)
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("ping()") == "pong"

    async def test_async_tools_are_awaitable_from_js(self) -> None:
        """The budget shim must preserve sync-vs-async.

        `bind_object` decides the op mode with `inspect.iscoroutinefunction`
        on whatever it is handed, so wrapping an async tool in a sync shim
        would silently register it as a sync op.
        """

        async def fetch(url: str) -> dict[str, Any]:
            await asyncio.sleep(0)
            return {"url": url, "status": 200}

        bridge = ToolBridge({"fetch": fetch})
        with Runtime() as rt:
            bridge.attach(rt)
            result = await rt.eval_async(
                "(async () => (await tools.fetch('http://x')).status)()"
            )
            assert result == 200

    async def test_sync_and_async_tools_coexist_on_one_bridge(self) -> None:
        async def slow() -> str:
            await asyncio.sleep(0)
            return "async-result"

        bridge = ToolBridge({"quick": lambda: "sync-result", "slow": slow})
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.quick()") == "sync-result"
            assert (
                await rt.eval_async("(async () => await tools.slow())()")
                == "async-result"
            )

    def test_one_bridge_attaches_to_several_runtimes(self) -> None:
        """The budget is per-bridge, so it is shared across attachments."""
        bridge = ToolBridge({"ping": lambda: 1}, max_calls=3)
        with Runtime() as a, Runtime() as b:
            bridge.attach(a)
            bridge.attach(b)
            assert a.eval("tools.ping()") == 1
            assert b.eval("tools.ping()") == 1
            assert bridge.calls_made == 2

    def test_structured_arguments_and_return_values(self) -> None:
        def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
            return {"count": len(rows), "names": [r["name"] for r in rows]}

        bridge = ToolBridge({"summarize": summarize})
        with Runtime() as rt:
            bridge.attach(rt)
            out = rt.eval("tools.summarize([{name:'a'},{name:'b'}])")
            assert out == {"count": 2, "names": ["a", "b"]}


class TestCallBudget:
    def test_the_call_past_the_limit_is_blocked(self) -> None:
        """The headline guarantee: the tool is not invoked past the budget."""
        invocations: list[int] = []

        def tool() -> str:
            invocations.append(1)
            return "ran"

        bridge = ToolBridge({"tool": tool}, max_calls=2)
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.tool()") == "ran"
            assert rt.eval("tools.tool()") == "ran"
            name = rt.eval("try { tools.tool(); 'no-throw' } catch (e) { e.name }")

        assert name == "ToolBudgetError"
        # The decisive assertion: the Python side really was not entered.
        assert len(invocations) == 2

    def test_budget_is_total_across_all_tools_not_per_tool(self) -> None:
        bridge = ToolBridge({"a": lambda: 1, "b": lambda: 2}, max_calls=3)
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.a()") == 1
            assert rt.eval("tools.b()") == 2
            assert rt.eval("tools.a()") == 1
            assert rt.eval("try { tools.b(); 'no' } catch (e) { e.name }") == (
                "ToolBudgetError"
            )

    def test_budget_message_names_the_refused_tool(self) -> None:
        bridge = ToolBridge({"get_weather": lambda c: c}, max_calls=0)
        with Runtime() as rt:
            bridge.attach(rt)
            msg = rt.eval("try { tools.get_weather('Zurich') } catch (e) { e.message }")
        assert "get_weather" in msg
        assert "budget" in msg.lower()

    def test_unlimited_by_default(self) -> None:
        bridge = ToolBridge({"ping": lambda: 1})
        assert bridge.calls_remaining is None
        with Runtime() as rt:
            bridge.attach(rt)
            assert (
                rt.eval("let n = 0; for (let i=0;i<50;i++) n += tools.ping(); n") == 50
            )
        assert bridge.calls_made == 50

    def test_counters_track_usage(self) -> None:
        bridge = ToolBridge({"ping": lambda: 1}, max_calls=5)
        with Runtime() as rt:
            bridge.attach(rt)
            rt.eval("tools.ping(); tools.ping()")
        assert bridge.calls_made == 2
        assert bridge.calls_remaining == 3

    def test_reset_budget_allows_calls_again(self) -> None:
        bridge = ToolBridge({"ping": lambda: 1}, max_calls=1)
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.ping()") == 1
            assert rt.eval("try { tools.ping(); 'no' } catch (e) { e.name }") == (
                "ToolBudgetError"
            )
            bridge.reset_budget()
            assert rt.eval("tools.ping()") == 1

    def test_silent_mode_returns_null_instead_of_throwing(self) -> None:
        invocations: list[int] = []

        def tool() -> str:
            invocations.append(1)
            return "ran"

        bridge = ToolBridge({"tool": tool}, max_calls=1, on_exhausted="silent")
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.tool()") == "ran"
            # Refused, but no exception: JS sees a null/undefined result.
            assert rt.eval("tools.tool() === null || tools.tool() === undefined")
        assert len(invocations) == 1

    async def test_budget_applies_to_async_tools_too(self) -> None:
        invocations: list[int] = []

        async def tool() -> str:
            invocations.append(1)
            return "ran"

        bridge = ToolBridge({"tool": tool}, max_calls=1)
        with Runtime() as rt:
            bridge.attach(rt)
            assert await rt.eval_async("(async () => await tools.tool())()") == "ran"
            name = await rt.eval_async(
                "(async () => { try { await tools.tool(); return 'no' } "
                "catch (e) { return e.name } })()"
            )
        assert name == "ToolBudgetError"
        assert len(invocations) == 1


class TestTypedErrors:
    """A tool's exception class must survive the round trip into JS."""

    @pytest.mark.parametrize(
        "exc_type,expected",
        [
            (ToolError, "ToolError"),
            (ToolNotFoundError, "ToolNotFoundError"),
            (ToolBudgetError, "ToolBudgetError"),
            (ValueError, "ValueError"),
            (KeyError, "KeyError"),
            (RuntimeError, "RuntimeError"),
            (PermissionError, "PermissionError"),
        ],
    )
    def test_exception_class_name_reaches_js_as_error_name(
        self, exc_type: type[Exception], expected: str
    ) -> None:
        def failing() -> None:
            raise exc_type("something went wrong")

        bridge = ToolBridge({"failing": failing})
        with Runtime() as rt:
            bridge.attach(rt)
            name = rt.eval("try { tools.failing(); 'no-throw' } catch (e) { e.name }")
        assert name == expected

    def test_js_can_branch_on_the_error_class(self) -> None:
        """The actual use case: distinguish not-found from bad-args in JS."""

        def lookup(key: str) -> str:
            if key == "missing":
                raise ToolNotFoundError(f"no tool named {key}")
            if not key:
                raise ValueError("key must be non-empty")
            return "found"

        bridge = ToolBridge({"lookup": lookup})
        with Runtime() as rt:
            bridge.attach(rt)
            script = """
              const classify = (key) => {
                try { return 'ok:' + tools.lookup(key) }
                catch (e) {
                  if (e.name === 'ToolNotFoundError') return 'not-found';
                  if (e.name === 'ValueError') return 'bad-args';
                  return 'other:' + e.name;
                }
              };
              [classify('x'), classify('missing'), classify('')].join('|')
            """
            assert rt.eval(script) == "ok:found|not-found|bad-args"

    def test_the_error_is_a_real_js_error_instance(self) -> None:
        def failing() -> None:
            raise ToolError("boom")

        bridge = ToolBridge({"failing": failing})
        with Runtime() as rt:
            bridge.attach(rt)
            assert (
                rt.eval("try { tools.failing() } catch (e) { e instanceof Error }")
                is True
            )

    def test_message_survives_without_the_class_prefix(self) -> None:
        def failing() -> None:
            raise ToolError("the sink is down")

        bridge = ToolBridge({"failing": failing})
        with Runtime() as rt:
            bridge.attach(rt)
            msg = rt.eval("try { tools.failing() } catch (e) { e.message }")
        assert msg == "the sink is down"

    def test_a_message_containing_a_colon_is_not_mangled(self) -> None:
        """The class name is split off at the first ': ' -- a message with its
        own colons must come through intact."""

        def failing() -> None:
            raise ValueError("bad url: http://x:8080/y")

        bridge = ToolBridge({"failing": failing})
        with Runtime() as rt:
            bridge.attach(rt)
            out = rt.eval(
                "try { tools.failing() } catch (e) { e.name + '|' + e.message }"
            )
        assert out == "ValueError|bad url: http://x:8080/y"

    def test_an_uncaught_tool_error_reaches_python_with_its_name(self) -> None:
        """Symmetry: the same name/message shape build_js_exception produces."""
        from pydeno import JavaScriptError

        def failing() -> None:
            raise ToolNotFoundError("nope")

        bridge = ToolBridge({"failing": failing})
        with Runtime() as rt:
            bridge.attach(rt)
            with pytest.raises(JavaScriptError) as caught:
                rt.eval("tools.failing()")

        assert caught.value.name == "ToolNotFoundError"
        assert caught.value.message == "nope"

    async def test_async_tool_errors_are_typed_too(self) -> None:
        async def failing() -> None:
            raise ToolNotFoundError("async nope")

        bridge = ToolBridge({"failing": failing})
        with Runtime() as rt:
            bridge.attach(rt)
            name = await rt.eval_async(
                "(async () => { try { await tools.failing(); return 'no' } "
                "catch (e) { return e.name } })()"
            )
        assert name == "ToolNotFoundError"


class TestFailClosedNames:
    """Names are validated up front, not when JS happens to call them."""

    @pytest.mark.parametrize(
        "bad",
        [
            "has space",
            "has-dash",
            "has.dot",
            "1leading_digit",
            "",
            "with/slash",
            "café",
            "a[0]",
        ],
    )
    def test_invalid_tool_names_are_rejected_at_construction(self, bad: str) -> None:
        with pytest.raises(ValueError, match="Invalid tool name"):
            ToolBridge({bad: lambda: 1})

    @pytest.mark.parametrize(
        "reserved", ["__proto__", "constructor", "prototype", "toString"]
    )
    def test_names_that_would_corrupt_js_objects_are_rejected(
        self, reserved: str
    ) -> None:
        with pytest.raises(ValueError, match="shadows a JavaScript"):
            ToolBridge({reserved: lambda: 1})

    def test_invalid_namespace_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid namespace"):
            ToolBridge({"ok": lambda: 1}, namespace="not a name")

    def test_non_string_name_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a string"):
            ToolBridge({1: lambda: 1})  # type: ignore[dict-item]

    def test_non_callable_tool_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="not callable"):
            ToolBridge({"nope": "just a string"})  # type: ignore[dict-item]

    def test_valid_names_are_accepted(self) -> None:
        bridge = ToolBridge(
            {"a": lambda: 1, "_private": lambda: 1, "get_weather_v2": lambda: 1}
        )
        assert set(bridge.tool_names) == {"a", "_private", "get_weather_v2"}

    def test_js_cannot_reach_a_tool_that_was_never_bound(self) -> None:
        bridge = ToolBridge({"allowed": lambda: "yes"})
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("typeof tools.forbidden") == "undefined"
            assert (
                rt.eval(
                    "try { tools.forbidden() } catch (e) { e instanceof TypeError }"
                )
                is True
            )

    def test_invalid_on_exhausted_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="on_exhausted"):
            ToolBridge({"a": lambda: 1}, on_exhausted="explode")

    @pytest.mark.parametrize("bad", [-1, "5", 1.5])
    def test_invalid_max_calls_is_rejected(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            ToolBridge({"a": lambda: 1}, max_calls=bad)  # type: ignore[arg-type]


class TestAttachRequiresARuntime:
    """ToolBridge needs a full Runtime to bind a Python callable into --
    there must be a real op registry (`Deno.core.ops` on a
    `deno_core::JsRuntime`) for the bound function to attach to. `attach`
    rejects anything else immediately and explains itself.
    """

    def test_attaching_to_an_unrelated_object_raises(self) -> None:
        bridge = ToolBridge({"ping": lambda: 1})
        with pytest.raises(TypeError, match="expects a pydeno.Runtime"):
            bridge.attach("not a runtime")


class TestTheBridgeIsALoadBearingBoundary:
    """The namespace and the budget must be more than decoration.

    Through v0.2.0 op ids were sequential integers and dispatch resolved any
    registered id, so a guest could call a bridge's underlying handler
    directly by guessing -- bypassing another bridge's namespace entirely,
    and surviving the budget only because the budgeted shim happened to be
    the thing registered. Op ids are now unguessable capability tokens and
    dispatch is gated on what a bind actually exposed.
    """

    def test_a_guest_cannot_reach_another_bridges_tools(self) -> None:
        privileged_calls: list[int] = []
        privileged = ToolBridge(
            {"deleteEverything": lambda: privileged_calls.append(1) or "done"},
            namespace="admin",
        )
        public = ToolBridge({"ping": lambda: "pong"}, namespace="tools")

        with Runtime() as rt:
            privileged.attach(rt)
            public.attach(rt)

            hits = rt.eval("""
              const found = [];
              for (let id = 0; id < 512; id++) {
                try { found.push(__host_op_sync__(id)); } catch (e) {}
              }
              JSON.stringify(found)
            """)

        assert hits == "[]", f"low op ids reached host handlers: {hits}"
        assert privileged_calls == []

    def test_the_budget_cannot_be_bypassed_by_addressing_the_op_directly(
        self,
    ) -> None:
        """The budget is charged in the shim, and the shim is all there is."""
        bridge = ToolBridge({"ping": lambda: "pong"}, max_calls=2)
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.ping()") == "pong"
            assert rt.eval("tools.ping()") == "pong"
            with pytest.raises(JavaScriptError):
                rt.eval("tools.ping()")
            # And the low-id sweep finds no unbudgeted way in.
            assert (
                rt.eval("""
                  let reached = 0;
                  for (let id = 0; id < 512; id++) {
                    try { __host_op_sync__(id); reached++; } catch (e) {}
                  }
                  reached
                """)
                == 0
            )
        assert bridge.calls_made == 2

    def test_detach_revokes_every_capability_the_bridge_installed(self) -> None:
        calls: list[str] = []
        bridge = ToolBridge(
            {"a": lambda: calls.append("a") or 1, "b": lambda: calls.append("b") or 2},
            namespace="tools",
        )
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.a()") == 1
            rt.eval("globalThis.captured = tools.b")

            assert bridge.detach(rt) == 2

            with pytest.raises(JavaScriptError):
                rt.eval("tools.a()")
            # Even a reference captured before the revoke is inert.
            with pytest.raises(JavaScriptError):
                rt.eval("captured()")
        assert calls == ["a"]

    def test_detach_revokes_bare_globals_too(self) -> None:
        bridge = ToolBridge({"ping": lambda: "pong"}, namespace=None)
        with Runtime() as rt:
            bridge.attach(rt)
            assert rt.eval("ping()") == "pong"
            assert bridge.detach(rt) == 1
            with pytest.raises(JavaScriptError):
                rt.eval("ping()")

    def test_detach_on_a_never_attached_bridge_is_a_no_op(self) -> None:
        bridge = ToolBridge({"ping": lambda: "pong"})
        with Runtime() as rt:
            assert bridge.detach(rt) == 0


def test_repr_is_informative() -> None:
    bridge = ToolBridge({"a": lambda: 1}, max_calls=10)
    text = repr(bridge)
    assert "ToolBridge" in text
    assert "0/10" in text


def test_tool_error_hierarchy() -> None:
    assert issubclass(ToolBudgetError, ToolError)
    assert issubclass(ToolNotFoundError, ToolError)
    assert issubclass(ToolError, Exception)


def test_an_unexposed_tool_name_is_a_plain_js_type_error() -> None:
    """Pins what actually happens, since the docs used to claim otherwise.

    `ToolNotFoundError` documented itself as "raised when JS asks for a tool
    the bridge does not expose", and nothing raised it -- there is no code path
    that could. A name the bridge refused was never installed, so it is not a
    property on the namespace and guest JS gets V8's own TypeError. That is the
    right answer; this test is here so the docs and the behaviour cannot drift
    apart again.
    """
    bridge = ToolBridge({"allowed": lambda: 1})
    with Runtime() as rt:
        bridge.attach(rt)
        assert rt.eval("typeof tools.allowed") == "function"
        assert rt.eval("typeof tools.nope") == "undefined"
        caught = rt.eval(
            "(() => { try { tools.nope(); } catch (e) { return e.name; } })()"
        )
        assert caught == "TypeError"


def test_tools_mapping_is_copied_not_aliased() -> None:
    """Mutating the caller's dict afterwards must not change the bridge."""
    tools: dict[str, Any] = {"a": lambda: 1}
    bridge = ToolBridge(tools)
    tools["b"] = lambda: 2
    assert bridge.tool_names == ("a",)
