"""Tools described by JSON Schema, their TypeScript, and the lazy tool catalog.

Issue #16. `SchemaTool` (or a plain mapping with its keys) is a tool that takes ONE object
argument, MCP style; `typescript_stubs()` declares it from its schemas. With ``tools_catalog=``
only ``search_tools``/``describe_tool`` are declared, whatever the catalog's size, and a catalog
tool is callable once one of them has shown it to the guest; before that, a call throws a
`ToolNotDiscoveredError` telling the model to search first. The host decides what is callable;
the guest-side proxy only spells the call.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from pydeno import JavaScriptError, SchemaTool, ToolNotDiscoveredError
from pydeno._agent import (
    AgentSandbox,
    Done,
    JournalError,
    ToolCall,
    describe_tools,
    typescript_stubs,
)
from pydeno._schema import as_schema_tool

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

ADDRESS_SCHEMAS = {
    "type": "object",
    "properties": {
        "customer": {"$ref": "#/$defs/Customer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "priority": {"enum": ["low", "high"]},
        "id": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        "note": {"type": ["string", "null"]},
        "legacy": {"type": "number", "nullable": True},
        "kind": {"oneOf": [{"const": "a"}, {"const": 1}]},
    },
    "required": ["customer", "priority"],
    "$defs": {
        "Customer": {
            "type": "object",
            "description": "A customer.",
            "properties": {
                "name": {"type": "string", "description": "Full name."},
                "address": {"$ref": "#/$defs/Address"},
                "referrer": {"$ref": "#/$defs/Customer"},
            },
            "required": ["name"],
        },
        "Address": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "additionalProperties": False,
        },
    },
}


def create_order(args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "args": args}


ORDER = SchemaTool(
    name="create_order",
    description="Create an order.",
    input_schema=ADDRESS_SCHEMAS,
    callable=create_order,
    output_schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "id": {"type": "integer"}},
        "required": ["ok"],
    },
)


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
    tools = [
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


def session(tools: Any = None, **kwargs: Any) -> AgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 3)
    return AgentSandbox(tools if tools is not None else {}, **kwargs)


# ---------------------------------------------------------------------------
# SchemaTool
# ---------------------------------------------------------------------------


class TestSchemaToolValidation:
    def test_a_mapping_is_accepted_with_mcp_spellings(self) -> None:
        tool = as_schema_tool(WEATHER)
        assert tool.name == "get_weather"
        assert tool.input_schema["required"] == ["city"]
        assert tool.output_schema == WEATHER["outputSchema"]

    @pytest.mark.parametrize(
        ("kwargs", "error", "match"),
        [
            ({"name": "bad-name"}, ValueError, "Invalid tool name"),
            ({"name": "constructor"}, ValueError, "shadows"),
            ({"input_schema": {"type": "string"}}, ValueError, "object"),
            ({"input_schema": []}, TypeError, "mapping"),
            ({"input_schema": {"default": object()}}, TypeError, "plain JSON"),
            ({"output_schema": "x"}, TypeError, "mapping"),
            ({"description": 3}, TypeError, "description"),
            ({"callable": "nope"}, TypeError, "not callable"),
        ],
        ids=[
            "name",
            "reserved",
            "non-object",
            "not-mapping",
            "not-json",
            "output",
            "description",
            "callable",
        ],
    )
    def test_bad_tools_are_refused(
        self, kwargs: dict[str, Any], error: type[Exception], match: str
    ) -> None:
        base: dict[str, Any] = {
            "name": "t",
            "description": "",
            "input_schema": {"type": "object"},
            "callable": lambda a: a,
        }
        with pytest.raises(error, match=match):
            SchemaTool(**{**base, **kwargs})

    def test_mapping_mistakes(self) -> None:
        with pytest.raises(TypeError, match="unknown keys"):
            as_schema_tool({**WEATHER, "parameters": {}})
        with pytest.raises(TypeError, match="callable"):
            as_schema_tool({"name": "x"})
        with pytest.raises(ValueError, match="one name"):
            AgentSandbox({"other": WEATHER})
        with pytest.raises(ValueError, match="twice"):
            AgentSandbox([WEATHER, WEATHER])

    def test_an_oversized_schema_is_refused(self) -> None:
        huge = {"type": "object", "description": "x" * 300_000}
        with pytest.raises(ValueError, match="larger"):
            SchemaTool("t", "", huge, lambda a: a)


# ---------------------------------------------------------------------------
# TypeScript from schemas
# ---------------------------------------------------------------------------


class TestTypescript:
    def test_objects_arrays_enums_unions_nullable_and_nested_refs(self) -> None:
        stubs = typescript_stubs([ORDER], namespace="tools")
        assert stubs == (
            "// Tools provided by the host. Every call returns a Promise.\n"
            "\n"
            "declare namespace tools {\n"
            "  /** A customer. */\n"
            "  interface Customer {\n"
            "    /** Full name. */\n"
            "    name: string;\n"
            "    address?: Address;\n"
            "    referrer?: Customer;\n"
            "  }\n"
            "\n"
            "  interface Address {\n"
            "    city?: string;\n"
            "  }\n"
            "\n"
            "  /** Create an order. */\n"
            "  function create_order(args: {\n"
            "    customer: Customer;\n"
            "    tags?: string[];\n"
            '    priority: "low" | "high";\n'
            "    id?: string | number;\n"
            "    note?: string | null;\n"
            "    legacy?: number | null;\n"
            '    kind?: "a" | 1;\n'
            "  }): Promise<{\n"
            "    ok: boolean;\n"
            "    id?: number;\n"
            "  }>;\n"
            "}\n"
        )

    def test_globals_use_declare(self) -> None:
        stubs = typescript_stubs([ORDER])
        assert "declare interface Customer {" in stubs
        assert "declare interface Address {\n  city?: string;\n}" in stubs
        assert "declare function create_order(args: {" in stubs

    def test_a_named_non_object_type_is_a_type_alias(self) -> None:
        tool = SchemaTool(
            "set_level",
            "",
            {
                "type": "object",
                "properties": {"level": {"$ref": "#/$defs/Level"}},
                "required": ["level"],
                "$defs": {
                    "Level": {"enum": ["debug", "info"], "description": "How loud."}
                },
            },
            lambda a: a,
        )
        assert typescript_stubs([tool]) == (
            "// Tools provided by the host. Every call returns a Promise.\n\n"
            "/** How loud. */\n"
            'declare type Level = "debug" | "info";\n\n'
            "declare function set_level(args: {\n  level: Level;\n}): Promise<unknown>;\n"
        )

    def test_shared_definitions_are_declared_once(self) -> None:
        other = SchemaTool(
            "find_customer",
            "Find one.",
            {"type": "object", "properties": {"q": {"type": "string"}}},
            lambda a: a,
            output_schema={
                "$ref": "#/$defs/Customer",
                "$defs": ADDRESS_SCHEMAS["$defs"],
            },
        )
        stubs = typescript_stubs([ORDER, other], namespace="t")
        assert stubs.count("interface Customer {") == 1
        assert (
            "function find_customer(args?: {\n    q?: string;\n  }): Promise<Customer>;"
            in stubs
        )

    def test_without_an_output_schema_the_result_is_unknown(self) -> None:
        tool = SchemaTool("ping", "", {"type": "object"}, lambda a: a)
        assert (
            "declare function ping(args?: Record<string, unknown>): Promise<unknown>;"
            in (typescript_stubs([tool]))
        )

    def test_mixed_with_python_tools(self) -> None:
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        stubs = typescript_stubs({"add": add, "get_weather": WEATHER})
        assert "declare function add(a: number, b: number): Promise<number>;" in stubs
        assert (
            "declare function get_weather(args: {\n  city: string;\n}): Promise<{\n  temp?: number;\n}>;"
            in stubs
        )

    def test_describe_tools_shows_a_schema_tool_with_an_example(self) -> None:
        text = describe_tools([WEATHER], namespace="api")
        assert "api.get_weather(args: {\n  city: string;\n}): Promise<{" in text
        assert (
            '    Example: const result = await api.get_weather({ city: "city" });'
            in text
        )
        assert "    Current weather for a city." in text

    def test_the_session_methods_match_the_module_functions(self) -> None:
        with session([WEATHER, ORDER], namespace="api") as s:
            assert s.typescript_stubs() == typescript_stubs(
                [WEATHER, ORDER], namespace="api"
            )
            assert s.describe_tools() == describe_tools(
                [WEATHER, ORDER], namespace="api"
            )

    @pytest.mark.needs_pydantic_ai
    def test_the_integration_still_exports_schema_tools_to_dts(self) -> None:
        from pydeno.integrations.pydantic_ai import js_tool_names, schema_tools_to_dts

        assert js_tool_names(["a-b"]) == {"a-b": "a_b"}
        assert "function create_order(" in schema_tools_to_dts(
            [ORDER.tool_definition()]
        )


# ---------------------------------------------------------------------------
# calling schema tools
# ---------------------------------------------------------------------------


class TestCalling:
    def test_the_callable_gets_the_argument_object(self) -> None:
        seen: list[Any] = []

        def lookup(args: dict[str, Any]) -> Any:
            seen.append(args)
            return {"found": args.get("q")}

        tool = SchemaTool("lookup", "", {"type": "object"}, lookup)
        with session([tool]) as s:
            assert s.run(
                "return await lookup({q: 'x', skip: undefined, n: [1, undefined]})"
            ) == {"found": "x"}
            assert s.run("return await lookup()") == {"found": None}
        assert seen == [{"q": "x", "n": [1, None]}, {}]

    def test_an_async_callable(self) -> None:
        async def slow(args: dict[str, Any]) -> int:
            await asyncio.sleep(0)
            return args["n"] * 2

        with session([SchemaTool("double", "", {"type": "object"}, slow)]) as s:
            assert s.run("return await double({n: 21})") == 42

    @pytest.mark.parametrize(
        ("call", "got"),
        [("get_weather(1, 2)", "2 arguments"), ("get_weather('Paris')", "got str")],
        ids=["two-args", "not-an-object"],
    )
    def test_wrong_arguments_tell_the_guest_how_to_call(
        self, call: str, got: str
    ) -> None:
        with session([WEATHER]) as s:  # redact_host_errors is on: this message is ours
            message = s.run(
                f"try {{ await {call}; }} catch (e) {{ return e.name + ': ' + e.message; }}"
            )
        assert message.startswith("TypeError: get_weather takes one object argument")
        assert got in message

    def test_a_pause_hands_over_the_argument_object(self) -> None:
        with session([WEATHER]) as s:
            step = s.start("return await get_weather({city: 'Oslo'})")
            assert isinstance(step, ToolCall)
            assert (step.name, step.args) == ("get_weather", ({"city": "Oslo"},))
            assert s.resume(step, s.call(step)) == Done({"city": "Oslo", "temp": 21})

    def test_journal_round_trip(self) -> None:
        with session([WEATHER]) as s:
            s.run("globalThis.w = await get_weather({city: 'Rome'})")
            blob = s.dump(KEY)
        with AgentSandbox.load(blob, KEY, [WEATHER]) as t:
            assert t.run("return w.city") == "Rome"


# ---------------------------------------------------------------------------
# the lazy catalog
# ---------------------------------------------------------------------------


def guest_error(s: AgentSandbox, call: str) -> str:
    return s.run(
        f"try {{ await {call}; return 'no error'; }} catch (e) {{ return e.name + ': ' + e.message; }}"
    )


class TestLazyCatalog:
    def test_the_declared_surface_does_not_grow_with_the_catalog(self) -> None:
        with (
            session(tools_catalog=catalog_of(3)) as small,
            session(tools_catalog=catalog_of(500)) as big,
        ):
            assert small.typescript_stubs() == big.typescript_stubs()
            assert small.describe_tools() == big.describe_tools()
            stubs = big.typescript_stubs()
            assert "widget" not in stubs and "translate_text" not in stubs
            assert (
                "declare function search_tools(query: string, limit?: number)" in stubs
            )
            assert "declare function describe_tool(name: string)" in stubs
            assert "ToolNotDiscoveredError" in big.describe_tools()
            assert len(big.catalog_names) == 501
            # The script installed in the guest is the same size too.
            from pydeno._agent import _prelude

            names = list(big.tool_names)
            assert _prelude(names, None, "tools") == _prelude(names, None, "tools")
            assert "widget" not in _prelude(names, None, "tools")

    def test_an_unfound_tool_throws_a_typed_error_telling_the_model_to_search(
        self,
    ) -> None:
        with session(tools_catalog=catalog_of(5), max_tool_calls=10) as s:
            message = guest_error(s, "tools.translate_text({text: 'hi', to: 'de'})")
            assert message.startswith("ToolNotDiscoveredError: ")
            assert "search_tools(query)" in message
            assert "translate_text" in message
            # Names that do not exist at all get the same answer.
            assert guest_error(s, "tools.no_such_tool({})").startswith(
                "ToolNotDiscoveredError"
            )
            assert s.calls_made == 0  # refused calls are not charged
            with pytest.raises(JavaScriptError, match="ToolNotDiscoveredError"):
                s.run("await tools.widget_1({})")
            assert (
                s.execute("await tools.widget_1({})").error_type
                == "ToolNotDiscoveredError"
            )

    def test_search_then_call(self) -> None:
        with session(tools_catalog=catalog_of(50)) as s:
            found = s.run("return await search_tools('translate language')")
            assert found[0] == {
                "name": "translate_text",
                "description": "Translate text into another language.",
            }
            assert "translate_text" in s.discovered_tools
            assert (
                s.run("return await tools.translate_text({text: 'hi', to: 'de'})")
                == "[de] hi"
            )
            # Found stays found, across runs.
            assert (
                s.run("return await tools.translate_text({text: 'a', to: 'fr'})")
                == "[fr] a"
            )
            assert guest_error(s, "tools.widget_7({})").startswith(
                "ToolNotDiscoveredError"
            )

    def test_describe_then_call(self) -> None:
        with session(tools_catalog=catalog_of(10)) as s:
            info = s.run("return await describe_tool('widget_7')")
            assert info["name"] == "widget_7"
            assert info["input_schema"] == {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
            }
            assert info["output_schema"] is None
            assert (
                "function widget_7(args?: {\n    x?: number;\n  }): Promise<unknown>;"
                in (info["typescript"])
            )
            assert info["typescript"].startswith("declare namespace tools {")
            assert s.run("return await tools.widget_7({x: 1})") == {"widget": 7, "x": 1}

    def test_search_results_are_bounded_and_validated(self) -> None:
        with session(tools_catalog=catalog_of(80)) as s:
            assert len(s.run("return await search_tools('widget')")) == 10
            assert len(s.run("return await search_tools('widget', 50)")) == 50
            assert s.run("return await search_tools('zzz-nothing')") == []
            assert len(s.run("return await search_tools('')")) == 10
            for bad in (
                "search_tools('w', 0)",
                "search_tools('w', 51)",
                "search_tools(5)",
            ):
                assert guest_error(s, bad).startswith(
                    "TypeError: search_tools(query, limit?)"
                )
            assert guest_error(s, "describe_tool('nope')") == (
                "ToolNotFoundError: no such tool in the catalog; search_tools(query) lists the "
                "ones that exist"
            )
            assert "nope" not in s.discovered_tools

    def test_the_catalog_lives_on_the_namespace(self) -> None:
        with session([WEATHER], namespace="api", tools_catalog=catalog_of(3)) as s:
            assert s.run("return (await api.get_weather({city: 'X'})).temp") == 21
            s.run("await api.search_tools('widget_2')")
            assert s.run("return await api.widget_2({})") == {"widget": 2}
            assert s.run("return typeof api.toString") == "function"  # not proxied
            assert s.run("return typeof api.then") == "undefined"  # awaitable-safe

    def test_declared_tools_cannot_be_reached_through_the_catalog(self) -> None:
        with session({"add": lambda a, b: a + b}, tools_catalog=catalog_of(2)) as s:
            assert guest_error(s, "tools.add(1, 2)").startswith(
                "ToolNotDiscoveredError"
            )
            assert guest_error(s, "tools.search_tools('x')").startswith(
                "ToolNotDiscoveredError"
            )
            assert (
                s.run("return typeof globalThis.__pydeno_agent_catalog") == "undefined"
            )

    def test_tampering_with_the_proxy_grants_nothing(self) -> None:
        with session(tools_catalog=catalog_of(3)) as s:
            s.run("globalThis.tools = {widget_1: () => 'fake'}")
            assert (
                s.run("return tools.widget_1()") == "fake"
            )  # the guest only fools itself
            assert s.calls_made == 0

    def test_the_budget_covers_catalog_calls(self) -> None:
        with session(tools_catalog=catalog_of(3), max_tool_calls=2) as s:
            s.run("await search_tools('widget_0')")
            s.run("await tools.widget_0({})")
            assert guest_error(s, "search_tools('x')").startswith("ToolBudgetError")
            assert s.calls_remaining == 0

    def test_a_driver_answering_search_decides_what_is_found(self) -> None:
        with session(tools_catalog=catalog_of(3)) as s:
            step = s.start(
                "await search_tools('widget'); return await tools.widget_2({x: 5})"
            )
            assert isinstance(step, ToolCall) and step.name == "search_tools"
            # Only what the answer names becomes callable, whoever produced it.
            step = s.resume(
                step, [{"name": "widget_2", "description": "d"}, {"name": "fake"}]
            )
            assert isinstance(step, ToolCall)
            assert (step.name, step.args) == ("widget_2", ({"x": 5},))
            assert s.resume(step, s.call(step)) == Done({"widget": 2, "x": 5})
            assert s.discovered_tools == {"widget_2"}

    def test_a_new_session_starts_with_nothing_found(self) -> None:
        tools_catalog = catalog_of(3)
        with session(tools_catalog=tools_catalog) as s:
            s.run("await search_tools('widget_1')")
        with session(tools_catalog=tools_catalog) as t:
            assert guest_error(t, "tools.widget_1({})").startswith(
                "ToolNotDiscoveredError"
            )

    def test_discovery_is_replayed_from_the_journal(self) -> None:
        tools_catalog = catalog_of(4)
        with session(tools_catalog=tools_catalog) as s:
            s.run("await describe_tool('widget_3')")
            blob = s.dump(KEY)
        with AgentSandbox.load(blob, KEY, {}, tools_catalog=tools_catalog) as t:
            assert t.discovered_tools == {"widget_3"}
            assert t.run("return await tools.widget_3({})") == {"widget": 3}
        with pytest.raises(JournalError, match="tools_catalog"):
            AgentSandbox.load(blob, KEY, {}, tools_catalog=catalog_of(5))
        with pytest.raises(JournalError):
            AgentSandbox.load(blob, KEY, {})

    @pytest.mark.parametrize(
        ("tools", "catalog", "match"),
        [
            ({"search_tools": lambda q: q}, catalog_of(1), "session's own tools"),
            ([WEATHER], [WEATHER], "both declared and in the catalog"),
            ({"tools": lambda: 1}, catalog_of(1), "would hide them"),
            ({}, {"widget": lambda a: a}, "must be a SchemaTool"),
            (
                {},
                [{"name": "isPrototypeOf", "callable": lambda a: a}],
                "cannot be a catalog tool name",
            ),
        ],
        ids=["reserved", "overlap", "namespace", "plain-callable", "object-member"],
    )
    def test_configuration_mistakes(self, tools: Any, catalog: Any, match: str) -> None:
        with pytest.raises((ValueError, TypeError), match=match):
            AgentSandbox(tools, tools_catalog=catalog)

    def test_with_a_namespace_a_tool_named_tools_is_fine(self) -> None:
        with session(
            {"tools": lambda: 1}, namespace="ns", tools_catalog=catalog_of(1)
        ) as s:
            assert s.run("return await ns.tools()") == 1


# ---------------------------------------------------------------------------
# JSCodeMode (pydantic-ai)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pai() -> SimpleNamespace:
    import pydantic_ai
    import pydantic_ai.messages as msg
    import pydantic_ai.models.function

    from pydeno.integrations import pydantic_ai as integration

    return SimpleNamespace(
        Agent=pydantic_ai.Agent,
        ModelResponse=msg.ModelResponse,
        TextPart=msg.TextPart,
        ToolCallPart=msg.ToolCallPart,
        ToolReturnPart=msg.ToolReturnPart,
        FunctionModel=pydantic_ai.models.function.FunctionModel,
        integration=integration,
    )


def _scripted(pai: SimpleNamespace, code: str, seen: list[Any]) -> Any:
    queue = [code]

    def respond(messages: list[Any], info: Any) -> Any:
        seen.append(info)
        if queue:
            return pai.ModelResponse(
                parts=[pai.ToolCallPart("run_javascript", {"code": queue.pop(0)})]
            )
        return pai.ModelResponse(parts=[pai.TextPart("done")])

    return pai.FunctionModel(respond)


def _run_js_returns(pai: SimpleNamespace, result: Any) -> list[Any]:
    return [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, pai.ToolReturnPart) and part.tool_name == "run_javascript"
    ]


@pytest.mark.needs_pydantic_ai
class TestJSCodeMode:
    def test_schema_tools_are_declared_and_callable_from_javascript(
        self, pai: SimpleNamespace
    ) -> None:
        seen: list[Any] = []
        calls: list[Any] = []

        def lookup(args: dict[str, Any]) -> dict[str, Any]:
            calls.append(args)
            return {"temp": 7}

        tool = {**WEATHER, "callable": lookup}
        agent = pai.Agent(
            _scripted(pai, "return await tools.get_weather({city: 'Bern'})", seen),
            capabilities=[pai.integration.JSCodeMode(schema_tools=[tool])],
        )
        result = agent.run_sync("weather?")
        description = next(
            t for t in seen[0].function_tools if t.name == "run_javascript"
        ).description
        assert (
            "function get_weather(args: {\n    city: string;\n  }): Promise<{\n    temp?: number;\n  }>;"
            in description
        )
        assert "/** Current weather for a city. */" in description
        assert _run_js_returns(pai, result) == [{"temp": 7}]
        assert calls == [{"city": "Bern"}]

    def test_schema_toolset_on_its_own(self, pai: SimpleNamespace) -> None:
        seen: list[Any] = []

        async def double(args: dict[str, Any]) -> int:
            return args["n"] * 2

        toolset = pai.integration.schema_toolset(
            {
                "double": {
                    "description": "Twice n.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                    },
                    "output_schema": {"type": "integer"},
                    "callable": double,
                }
            }
        )
        agent = pai.Agent(
            _scripted(pai, "return await tools.double({n: 4})", seen),
            toolsets=[toolset],
            capabilities=[pai.integration.JSCodeMode()],
        )
        result = agent.run_sync("go")
        assert _run_js_returns(pai, result) == [8]
        description = next(
            t for t in seen[0].function_tools if t.name == "run_javascript"
        ).description
        assert "): Promise<number>;" in description

    def test_a_bad_schema_tool_fails_at_construction(
        self, pai: SimpleNamespace
    ) -> None:
        with pytest.raises(ValueError, match="object"):
            pai.integration.JSCodeMode(
                schema_tools=[
                    {"name": "x", "input_schema": {"type": "array"}, "callable": len}
                ]
            )


def test_schema_tools_round_trip_through_json() -> None:
    """A schema handed to the guest by `describe_tool` is plain JSON (no shared references)."""
    tool = as_schema_tool(WEATHER)
    assert json.loads(json.dumps(tool.tool_definition()))["name"] == "get_weather"


def test_tool_not_discovered_error_is_exported() -> None:
    from pydeno._tools import ToolError

    assert issubclass(ToolNotDiscoveredError, ToolError)


def test_a_tool_raising_tool_not_discovered_is_still_redacted() -> None:
    """Only pydeno's own refusal skips redaction, not the class name in a host tool's hands."""

    def leaky() -> None:
        raise ToolNotDiscoveredError("secret: /srv/keys")

    with session({"leaky": leaky}) as s:
        assert guest_error(s, "leaky()") == (
            "ToolNotDiscoveredError: host function failed"
        )
