"""`on_unserializable` (#145): a function in a result no longer has to fail the whole run."""

from __future__ import annotations

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    IsolatedRuntime,
    RuntimeConfig,
    SandboxPool,
)

pytestmark = pytest.mark.full_sandbox

NESTED = "({a: () => 1, b: 2, c: [1, () => 2, {d: () => 3, e: 4}], s: new Set([1])})"


def test_default_is_the_secure_error() -> None:
    with IsolatedRuntime(RuntimeConfig()) as rt:
        with pytest.raises(TypeError, match="cannot cross the isolation boundary"):
            rt.eval(NESTED)
        assert rt.eval("1 + 1") == 2  # the runtime survives


def test_explicit_error_is_the_default() -> None:
    with IsolatedRuntime(RuntimeConfig(), on_unserializable="error") as rt:
        with pytest.raises(TypeError, match="cannot cross"):
            rt.eval("(() => 1)")


def test_drop_removes_members_and_nulls_items() -> None:
    with IsolatedRuntime(RuntimeConfig(), on_unserializable="drop") as rt:
        assert rt.eval(NESTED) == {"b": 2, "c": [1, None, {"e": 4}], "s": {1}}
        assert rt.eval("(() => 1)") is None
        assert rt.eval("[1, function () {}]") == [1, None]


def test_stringify_names_the_type_and_nothing_else() -> None:
    with IsolatedRuntime(RuntimeConfig(), on_unserializable="stringify") as rt:
        got = rt.eval("({f: function secretName() { return 'secret body' }, n: 1})")
        assert got == {"f": "[JsFunction]", "n": 1}
        assert rt.eval("[() => 1]") == ["[JsFunction]"]
        assert rt.eval("(() => 1)") == "[JsFunction]"


def test_serializable_values_are_untouched() -> None:
    code = "({u: undefined, n: null, i: 10n ** 20n, b: new Uint8Array([1, 2]), d: new Date(0)})"
    with IsolatedRuntime(RuntimeConfig()) as plain:
        expected = plain.eval(code)
        bare = plain.eval("undefined")
    with IsolatedRuntime(RuntimeConfig(), on_unserializable="drop") as rt:
        assert rt.eval(code) == expected
        assert rt.eval("undefined") is bare


def test_applies_to_eval_async_execute_and_modules() -> None:
    import asyncio

    with IsolatedRuntime(RuntimeConfig(), on_unserializable="stringify") as rt:
        assert asyncio.run(rt.eval_async("Promise.resolve({f: () => 1})")) == {
            "f": "[JsFunction]"
        }
        assert rt.execute("({f: () => 1})").result == {"f": "[JsFunction]"}


def test_unknown_value_is_refused() -> None:
    with pytest.raises(ValueError, match="on_unserializable"):
        IsolatedRuntime(RuntimeConfig(), on_unserializable="ignore")


async def test_async_runtime() -> None:
    async with AsyncIsolatedRuntime(RuntimeConfig()) as rt:
        with pytest.raises(TypeError, match="cannot cross"):
            await rt.eval(NESTED)
    async with AsyncIsolatedRuntime(RuntimeConfig(), on_unserializable="drop") as rt:
        assert await rt.eval("({a: () => 1, b: 2})") == {"b": 2}
        assert await rt.eval_async("Promise.resolve([() => 1])") == [None]
    async with AsyncIsolatedRuntime(
        RuntimeConfig(), on_unserializable="stringify"
    ) as rt:
        assert await rt.eval("[() => 1]") == ["[JsFunction]"]


def test_pool_checkout_can_set_it() -> None:
    with SandboxPool(size=1) as pool:
        with pool.checkout(on_unserializable="drop") as rt:
            assert rt.eval("({a: () => 1, b: 2})") == {"b": 2}
        with pool.checkout() as rt:  # the next checkout is back to the default
            with pytest.raises(TypeError, match="cannot cross"):
                rt.eval("({a: () => 1})")


def test_agent_sandbox_default_and_option() -> None:
    with AgentSandbox({}) as s:
        result = s.execute("return {f: () => 1}")
        assert result.status == "Failed"
        assert "cannot cross" in str(result.error)
        assert s.run("return 3") == 3
    with AgentSandbox({}, on_unserializable="drop") as s:
        assert s.run("return {f: () => 1, ok: [1, () => 2]}") == {"ok": [1, None]}
    with AgentSandbox({}, on_unserializable="stringify") as s:
        assert s.run("return {f: () => 1}") == {"f": "[JsFunction]"}


async def test_async_agent_sandbox() -> None:
    async with AsyncAgentSandbox({}, on_unserializable="drop") as s:
        assert await s.run("return {f: () => 1, n: 1}") == {"n": 1}
    async with AsyncAgentSandbox({}) as s:
        result = await s.execute("return {f: () => 1}")
        assert result.status == "Failed"
