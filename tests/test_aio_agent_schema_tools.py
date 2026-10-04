"""Schema tools and the lazy catalog on `AsyncAgentSandbox`.

The async port of `TestCalling`/`TestLazyCatalog` in `test_agent_schema_tools.py` (the schema
validation and TypeScript generation are shared and tested there). Proved here: the async session
declares exactly what the sync one declares, calls schema tools (sync and async callables) with
the argument object, gates catalog tools on discovery, and replays discovery from a journal that
either class wrote.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    JavaScriptError,
    SchemaTool,
)
from pydeno._agent import Done, JournalError, ToolCall, describe_tools, typescript_stubs

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def weather(args: dict[str, Any]) -> dict[str, Any]:
    return {"city": args["city"], "temp": 21}


WEATHER = {
    "name": "get_weather",
    "description": "Current weather for a city.",
    "inputSchema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
    "outputSchema": {"type": "object", "properties": {"temp": {"type": "number"}}},
    "callable": weather,
}


def catalog_of(n: int) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "name": f"widget_{i}",
            "description": f"Operate widget number {i}.",
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
            },
            "callable": (lambda i: lambda args: {"widget": i, **args})(i),
        }
        for i in range(n)
    ]
    tools.append(
        {
            "name": "translate_text",
            "description": "Translate text into another language.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "to": {"type": "string"}},
                "required": ["text", "to"],
            },
            "callable": lambda args: f"[{args['to']}] {args['text']}",
        }
    )
    return tools


def session(tools: Any = None, **kwargs: Any) -> AsyncAgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 3)
    return AsyncAgentSandbox(tools if tools is not None else {}, **kwargs)


async def guest_error(s: AsyncAgentSandbox, call: str) -> str:
    return await s.run(
        f"try {{ await {call}; return 'no error'; }} "
        "catch (e) { return e.name + ': ' + e.message; }"
    )


class TestPromptHelpers:
    def test_same_declarations_as_the_sync_class(self) -> None:
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        tools = {"add": add, "get_weather": WEATHER}
        s = session(tools, namespace="api", tools_catalog=catalog_of(3))
        stubs = s.typescript_stubs()
        assert "function get_weather(args: {" in stubs
        assert "widget" not in stubs and "ToolNotDiscoveredError" in stubs
        sync = AgentSandbox.__new__(AgentSandbox)
        sync._configure(  # noqa: SLF001 - no worker: only the declarations are compared
            tools,
            max_tool_calls=None,
            namespace="api",
            tools_catalog=catalog_of(3),
            clock=CLOCK,
            random_seed=3,
            max_journal_bytes=1 << 20,
            max_output_bytes=1 << 16,
            max_result_bytes=1 << 20,
            runtime_options={},
        )
        assert stubs == sync.typescript_stubs()
        assert s.describe_tools() == sync.describe_tools()
        assert session([WEATHER]).describe_tools() == describe_tools([WEATHER])
        assert session([WEATHER]).typescript_stubs() == typescript_stubs([WEATHER])

    def test_the_declared_surface_does_not_grow_with_the_catalog(self) -> None:
        small = session(tools_catalog=catalog_of(3))
        big = session(tools_catalog=catalog_of(500))
        assert small.typescript_stubs() == big.typescript_stubs()
        assert len(big.catalog_names) == 501


class TestCalling:
    async def test_the_callable_gets_the_argument_object(self) -> None:
        seen: list[Any] = []

        def lookup(args: dict[str, Any]) -> Any:
            seen.append(args)
            return {"found": args.get("q")}

        async with session([SchemaTool("lookup", "", {"type": "object"}, lookup)]) as s:
            got = await s.run(
                "return await lookup({q: 'x', skip: undefined, n: [1, undefined]})"
            )
            assert got == {"found": "x"}
            assert await s.run("return await lookup()") == {"found": None}
        assert seen == [{"q": "x", "n": [1, None]}, {}]

    async def test_an_async_callable(self) -> None:
        async def slow(args: dict[str, Any]) -> int:
            await asyncio.sleep(0)
            return args["n"] * 2

        async with session([SchemaTool("double", "", {"type": "object"}, slow)]) as s:
            assert await s.run("return await double({n: 21})") == 42

    async def test_wrong_arguments_tell_the_guest_how_to_call(self) -> None:
        async with session([WEATHER]) as s:
            message = await guest_error(s, "get_weather(1, 2)")
        assert message.startswith("TypeError: get_weather takes one object argument")
        assert "2 arguments" in message

    async def test_a_pause_hands_over_the_argument_object(self) -> None:
        async with session([WEATHER]) as s:
            step = await s.start("return await get_weather({city: 'Oslo'})")
            assert isinstance(step, ToolCall)
            assert (step.name, step.args) == ("get_weather", ({"city": "Oslo"},))
            done = await s.resume(step, await s.call(step))
            assert done == Done({"city": "Oslo", "temp": 21})
            with pytest.raises(TypeError, match="ToolCall from this session"):
                await s.call("not a call")  # type: ignore[arg-type]

    async def test_journal_round_trip_with_the_sync_class(self) -> None:
        async with session([WEATHER]) as s:
            await s.run("globalThis.w = await get_weather({city: 'Rome'})")
            blob = await s.dump(KEY)

        def sync_side() -> str:
            with AgentSandbox.load(blob, KEY, [WEATHER]) as t:
                return t.run("return w.city")

        assert await asyncio.to_thread(sync_side) == "Rome"


class TestLazyCatalog:
    async def test_an_unfound_tool_throws_a_typed_error(self) -> None:
        async with session(tools_catalog=catalog_of(5), max_tool_calls=10) as s:
            message = await guest_error(
                s, "tools.translate_text({text: 'hi', to: 'de'})"
            )
            assert message.startswith("ToolNotDiscoveredError: ")
            assert "search_tools(query)" in message
            assert (await guest_error(s, "tools.no_such_tool({})")).startswith(
                "ToolNotDiscoveredError"
            )
            assert s.calls_made == 0  # refused calls are not charged
            with pytest.raises(JavaScriptError, match="ToolNotDiscoveredError"):
                await s.run("await tools.widget_1({})")
            r = await s.execute("await tools.widget_1({})")
            assert r.error_type == "ToolNotDiscoveredError"

    async def test_search_then_call_and_describe_then_call(self) -> None:
        async with session(tools_catalog=catalog_of(50)) as s:
            found = await s.run("return await search_tools('translate language')")
            assert found[0]["name"] == "translate_text"
            assert "translate_text" in s.discovered_tools
            got = await s.run(
                "return await tools.translate_text({text: 'hi', to: 'de'})"
            )
            assert got == "[de] hi"
            info = await s.run("return await describe_tool('widget_7')")
            assert info["typescript"].startswith("declare namespace tools {")
            assert await s.run("return await tools.widget_7({x: 1})") == {
                "widget": 7,
                "x": 1,
            }
            assert (await guest_error(s, "tools.widget_8({})")).startswith(
                "ToolNotDiscoveredError"
            )

    async def test_the_catalog_lives_on_the_namespace(self) -> None:
        async with session(
            [WEATHER], namespace="api", tools_catalog=catalog_of(3)
        ) as s:
            assert await s.run("return (await api.get_weather({city: 'X'})).temp") == 21
            await s.run("await api.search_tools('widget_2')")
            assert await s.run("return await api.widget_2({})") == {"widget": 2}
            assert await s.run("return typeof api.then") == "undefined"

    async def test_declared_tools_cannot_be_reached_through_the_catalog(self) -> None:
        async with session(
            {"add": lambda a, b: a + b}, tools_catalog=catalog_of(2)
        ) as s:
            assert (await guest_error(s, "tools.add(1, 2)")).startswith(
                "ToolNotDiscoveredError"
            )
            assert (
                await s.run("return typeof globalThis.__pydeno_agent_catalog")
                == "undefined"
            )

    async def test_the_budget_covers_catalog_calls(self) -> None:
        async with session(tools_catalog=catalog_of(3), max_tool_calls=2) as s:
            await s.run("await search_tools('widget_0')")
            await s.run("await tools.widget_0({})")
            assert (await guest_error(s, "search_tools('x')")).startswith(
                "ToolBudgetError"
            )

    async def test_a_driver_answering_search_decides_what_is_found(self) -> None:
        async with session(tools_catalog=catalog_of(3)) as s:
            step = await s.start(
                "await search_tools('widget'); return await tools.widget_2({x: 5})"
            )
            assert isinstance(step, ToolCall) and step.name == "search_tools"
            step = await s.resume(
                step, [{"name": "widget_2", "description": "d"}, {"name": "fake"}]
            )
            assert isinstance(step, ToolCall)
            assert (step.name, step.args) == ("widget_2", ({"x": 5},))
            assert await s.resume(step, await s.call(step)) == Done(
                {"widget": 2, "x": 5}
            )
            assert s.discovered_tools == {"widget_2"}

    async def test_discovery_is_replayed_from_a_journal_of_either_class(self) -> None:
        catalog = catalog_of(4)
        async with session(tools_catalog=catalog) as s:
            await s.run("await describe_tool('widget_3')")
            blob = await s.dump(KEY)
        t = await AsyncAgentSandbox.load(blob, KEY, {}, tools_catalog=catalog)
        try:
            assert t.discovered_tools == {"widget_3"}
            assert await t.run("return await tools.widget_3({})") == {"widget": 3}
        finally:
            await t.close()

        def sync_side() -> frozenset[str]:
            with AgentSandbox.load(blob, KEY, {}, tools_catalog=catalog) as u:
                return u.discovered_tools

        assert await asyncio.to_thread(sync_side) == {"widget_3"}
        with pytest.raises(JournalError, match="tools_catalog"):
            await AsyncAgentSandbox.load(blob, KEY, {}, tools_catalog=catalog_of(5))

    @pytest.mark.parametrize(
        ("tools", "catalog", "match"),
        [
            ({"search_tools": lambda q: q}, catalog_of(1), "session's own tools"),
            ([WEATHER], [WEATHER], "both declared and in the catalog"),
            ({"tools": lambda: 1}, catalog_of(1), "would hide them"),
            ({}, {"widget": lambda a: a}, "must be a SchemaTool"),
        ],
        ids=["reserved", "overlap", "namespace", "plain-callable"],
    )
    def test_configuration_mistakes(self, tools: Any, catalog: Any, match: str) -> None:
        with pytest.raises((ValueError, TypeError), match=match):
            AsyncAgentSandbox(tools, tools_catalog=catalog)
