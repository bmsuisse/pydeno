"""Prompt budgets select complete descriptions without changing tool authority."""

import pytest

from pydeno import SchemaTool, describe_tool_catalog
from pydeno._agent import _PREAMBLE, describe_tools


def small(value: int) -> int:
    """Return the supplied value."""
    return value


def entry(ns, name, tool):
    return (
        f"### {ns}.{name} (entry-local types)\n```typescript\n"
        + describe_tools({name: tool}, namespace=ns)[len(_PREAMBLE) :]
        + "```\n"
    )


def test_zero_budget_still_lists_every_namespace():
    text = describe_tool_catalog({"z": {"small": small}, "a": {}}, max_chars=0)
    assert "PARTIAL: 0 of 1 tools shown" in text
    assert "a: 0 of 0 tools shown" in text
    assert "z: 0 of 1 tools shown" in text
    assert "z.small(" not in text


def test_round_robin_reserves_representation_for_small_namespaces():
    tools = {"a": {"one": small, "two": small, "three": small}, "b": {"one": small}}
    budget = len(entry("a", "one", small)) + len(entry("b", "one", small))
    text = describe_tool_catalog(tools, max_chars=budget)
    assert "a.one(" in text and "b.one(" in text
    assert "a.two(" not in text and "a.three(" not in text
    assert "PARTIAL: 2 of 4 tools shown" in text
    assert len(text.split("## Tool entries\n", 1)[1]) == budget


def test_catalog_is_deterministic_and_chooses_complete_cheapest_entries():
    def verbose(value: int) -> int:
        return value

    verbose.__doc__ = "LONG-DESCRIPTION " * 100
    first = {"z": {"verbose": verbose, "small": small}, "a": {"small": small}}
    budget = len(entry("a", "small", small)) + len(entry("z", "small", small))
    text = describe_tool_catalog(first, max_chars=budget)
    reversed_tools = {
        ns: dict(reversed(list(ts.items()))) for ns, ts in reversed(list(first.items()))
    }
    assert text == describe_tool_catalog(reversed_tools, max_chars=budget)
    assert "LONG-DESCRIPTION" not in text
    assert "Example:" in text


def test_exact_entry_budget_includes_schema_dependencies_without_cutting():
    tool = SchemaTool(
        name="lookup",
        description="Lookup a record",
        input_schema={
            "type": "object",
            "properties": {"item": {"$ref": "#/$defs/Item"}},
            "$defs": {
                "Item": {"type": "object", "properties": {"id": {"type": "string"}}}
            },
        },
        callable=lambda value: value,
    )
    block = entry("api", "lookup", tool)
    text = describe_tool_catalog({"api": {"lookup": tool}}, max_chars=len(block))
    assert text.endswith(block)
    assert "COMPLETE: 1 of 1 tools shown" in text
    shorter = describe_tool_catalog({"api": {"lookup": tool}}, max_chars=len(block) - 1)
    assert "api.lookup(" not in shorter


@pytest.mark.parametrize("budget", [True, 1.5, "10", None])
def test_non_integer_budgets_are_rejected(budget):
    with pytest.raises(TypeError):
        describe_tool_catalog({}, max_chars=budget)


def test_negative_budget_and_invalid_namespace_are_rejected():
    with pytest.raises(ValueError):
        describe_tool_catalog({}, max_chars=-1)
    with pytest.raises(ValueError):
        describe_tool_catalog({"bad.namespace": {"small": small}})


def test_conflicting_schema_type_names_have_explicit_entry_scope():
    def tool(name, kind):
        return SchemaTool(
            name=name,
            description="Lookup",
            input_schema={
                "type": "object",
                "properties": {"item": {"$ref": "#/$defs/Item"}},
                "$defs": {
                    "Item": {"type": "object", "properties": {"id": {"type": kind}}}
                },
            },
            callable=lambda value: value,
        )

    text = describe_tool_catalog(
        {"api": {"first": tool("first", "string"), "second": tool("second", "number")}},
        max_chars=8000,
    )
    assert "Types in each tool entry are local to that entry" in text
    first = text.split("### api.first (entry-local types)\n```typescript\n", 1)[
        1
    ].split("```", 1)[0]
    second = text.split("### api.second (entry-local types)\n```typescript\n", 1)[
        1
    ].split("```", 1)[0]
    assert "interface Item" in first and "id?: string" in first
    assert "interface Item" in second and "id?: number" in second
    assert "api.first(" in first and "api.second(" in second
