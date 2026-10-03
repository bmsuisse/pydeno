"""`examples/monty_sql_charts.py`: an agent answers questions over SQLite, Monty and pydeno, both sandboxed.

These tests hold the example to its claims: the answers equal an independent computation with plain
sqlite3 and Python, the SQL tool refuses everything that is not a bounded SELECT (and the data is
untouched afterwards), one call budget stops both sandboxes, the output is deterministic, and
neither sandbox can reach outside.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import statistics
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "monty_sql_charts.py"
)
ROWS = 1500
TASKS = ("revenue_by_month", "top_customers_by_region", "cohort_retention")


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_sql_charts", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> Any:
    return _load_example()


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.SqlChartPipeline(rows=ROWS)
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> dict[str, tuple[str, dict[str, Any], dict[str, float]]]:
    return {task: pipe.render(task) for task in TASKS}


def _all(pipe: Any, sql: str) -> list[tuple[Any, ...]]:
    """The reference: the trusted writable connection, no sandbox, no tool."""
    return pipe.source.execute(sql).fetchall()


def _fingerprint(pipe: Any) -> tuple[Any, ...]:
    return tuple(
        tuple(_all(pipe, f"SELECT * FROM {t} ORDER BY 1"))
        for t in ("customers", "products", "orders")
    )


# ---------------------------------------------------------------------------------------------
# The pipeline and its output
# ---------------------------------------------------------------------------------------------


def test_the_module_constants_and_shape(example: Any) -> None:
    assert example.PIPELINE is example.SqlChartPipeline
    assert example.SIZES == sorted(example.SIZES) and len(example.SIZES) >= 3
    assert set(example.TASKS) == set(TASKS)
    assert "describe_schema" in example.TASKS["top_customers_by_region"]["python"]


def test_every_chart_is_well_formed_svg_with_data_in_it(
    rendered: dict[str, tuple[str, dict[str, Any], dict[str, float]]],
) -> None:
    expected_text = {
        "revenue_by_month": "Monthly revenue",
        "top_customers_by_region": "Top three customers",
        "cohort_retention": "retention",
    }
    for task, (svg, stats, timings) in rendered.items():
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")
        text = " ".join(t for t in root.itertext() if t.strip())
        assert expected_text[task] in text
        assert len(list(root.iter())) > stats["datums"]  # at least a mark per datum
        assert set(timings) == {"monty_prepare", "js_build", "total"}
        assert timings["total"] >= timings["monty_prepare"] + timings["js_build"] - 1e-3
        assert stats["task"] == task


def test_the_dataset_is_two_years_and_deterministic(example: Any, pipe: Any) -> None:
    first, last, n = _all(
        pipe, "SELECT MIN(ordered_at), MAX(ordered_at), COUNT(*) FROM orders"
    )[0]
    assert (first[:4], last[:4], n) == ("2023", "2024", ROWS)
    months = {r[0] for r in _all(pipe, "SELECT substr(ordered_at,1,7) FROM orders")}
    assert len(months) == 24
    again = example.build_database(ROWS)
    assert again.serialize() == pipe.source.serialize()
    assert example.build_database(ROWS, seed=8).serialize() != again.serialize()


def test_tool_description_is_generated_from_the_real_tools(
    example: Any, pipe: Any
) -> None:
    text = pipe.tool_description
    assert (
        text == example.TOOL_DESCRIPTION
    )  # the prompt does not depend on the dataset size
    stubs = example.tool_stubs(pipe.tools)
    names = {n.name for n in ast.parse(stubs).body if isinstance(n, ast.FunctionDef)}
    assert names == {
        "describe_schema",
        "query",
    }  # and valid Python, so a type checker can read it
    for needle in (
        "def query(sql: str",
        "orders(",
        "customers(",
        "ONE tool-call budget",
    ):
        assert needle in text


# ---------------------------------------------------------------------------------------------
# The answers equal an independent computation
# ---------------------------------------------------------------------------------------------


def test_revenue_by_month_matches_plain_python(
    pipe: Any, rendered: dict[str, tuple[str, dict[str, Any], dict[str, float]]]
) -> None:
    revenue: dict[str, int] = defaultdict(int)
    for amount, day in _all(pipe, "SELECT amount_cents, ordered_at FROM orders"):
        revenue[day[:7]] += amount
    months = sorted(revenue)
    got = pipe.prepare("revenue_by_month")
    assert [m["month"] for m in got["months"]] == months
    assert [m["revenue"] for m in got["months"]] == [revenue[m] / 100 for m in months]
    slope, intercept = statistics.linear_regression(
        range(len(months)), [revenue[m] / 100 for m in months]
    )
    assert got["summary"]["slope_per_month"] == pytest.approx(slope, rel=1e-9)
    assert got["months"][5]["trend"] == pytest.approx(intercept + slope * 5, rel=1e-9)
    ys = [revenue[m] / 100 for m in months]
    assert got["months"][7]["avg3"] == pytest.approx(sum(ys[5:8]) / 3, rel=1e-9)
    assert got["summary"]["total"] == pytest.approx(sum(ys))
    assert got["summary"]["best_month"] == max(months, key=lambda m: revenue[m])
    assert rendered["revenue_by_month"][1]["datums"] == 24


def test_top_customers_by_region_match_plain_python(pipe: Any) -> None:
    per_customer: dict[int, int] = defaultdict(int)
    for cid, amount, day in _all(
        pipe, "SELECT customer_id, amount_cents, ordered_at FROM orders"
    ):
        if day >= "2024-01-01":
            per_customer[cid] += amount
    info = {
        cid: (name, region)
        for cid, name, region in _all(pipe, "SELECT id, name, region FROM customers")
    }
    expected = []
    for region in sorted({r for _, r in info.values()}):
        ranked = sorted(
            (c for c in per_customer if info[c][1] == region),
            key=lambda c: (-per_customer[c], c),
        )
        expected += [
            (region, rank, info[c][0], per_customer[c] / 100)
            for rank, c in enumerate(ranked[:3], start=1)
        ]
    got = pipe.prepare("top_customers_by_region")
    assert [
        (b["region"], b["rank"], b["customer"], b["revenue"]) for b in got["bars"]
    ] == expected
    assert len(expected) == 12
    totals: dict[str, int] = defaultdict(int)
    for cid, cents in per_customer.items():
        totals[info[cid][1]] += cents
    assert got["summary"]["region_totals"] == {k: v / 100 for k, v in totals.items()}


def test_cohort_retention_matches_plain_python(pipe: Any) -> None:
    signup = dict(_all(pipe, "SELECT id, signup_month FROM customers"))
    active: dict[tuple[str, int], set[int]] = defaultdict(set)

    def idx(month: str) -> int:
        return int(month[:4]) * 12 + int(month[5:7]) - 1

    for cid, day in _all(pipe, "SELECT customer_id, ordered_at FROM orders"):
        offset = idx(day[:7]) - idx(signup[cid])
        if 0 <= offset <= 12:
            active[(signup[cid], offset)].add(cid)
    size: dict[str, int] = defaultdict(int)
    for month in signup.values():
        size[month] += 1
    expected = [
        (c, k, round(100 * len(ids) / size[c], 1), len(ids))
        for (c, k), ids in sorted(active.items())
    ]
    got = pipe.prepare("cohort_retention")
    assert [
        (c["cohort"], c["offset"], c["retention"], c["active"]) for c in got["cells"]
    ] == expected
    assert all(c["retention"] == 100.0 for c in got["cells"] if c["offset"] == 0)
    assert got["summary"]["customers"] == len(signup)
    assert got["summary"]["curve"][0] == {"offset": 0, "mean": 100.0}


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    for task in TASKS:
        assert pipe.render(task)[0] == pipe.render(task)[0]


def test_tool_calls_are_counted_across_both_sandboxes(
    rendered: dict[str, tuple[str, dict[str, Any], dict[str, float]]],
) -> None:
    # revenue: 1 in Monty + 1 in JS; top customers: 2 in Monty + 0... plus JS none; cohorts: 2 + 1
    assert rendered["revenue_by_month"][1]["tool_calls"] == 2
    assert rendered["top_customers_by_region"][1]["tool_calls"] == 2
    assert rendered["cohort_retention"][1]["tool_calls"] == 3


# ---------------------------------------------------------------------------------------------
# The SQL tool is safe in its own right
# ---------------------------------------------------------------------------------------------


def test_the_model_cannot_write_attach_or_load_anything(
    example: Any, pipe: Any
) -> None:
    before = _fingerprint(pipe)
    attempts = list(example.NASTY_SQL) + [
        "DROP TABLE customers",
        "DROP TABLE IF EXISTS products",
        "ALTER TABLE orders ADD COLUMN x",
        "CREATE VIEW v AS SELECT 1",
        "CREATE TRIGGER t AFTER INSERT ON orders BEGIN SELECT 1; END",
        "REPLACE INTO products VALUES (1, 'x', 'y', 1)",
        "VACUUM",
        "VACUUM INTO '/tmp/stolen.db'",
        "PRAGMA writable_schema = ON",
        "PRAGMA table_info(orders)",
        "PRAGMA journal_mode = DELETE",
        "SELECT * FROM orders; DROP TABLE orders",
        "WITH x AS (SELECT 1) DELETE FROM orders",
        "BEGIN",
        "COMMIT",
        "SELECT hex(randomblob(10))",
        "SELECT load_extension('x')",
        "SELECT sqlite_version()",
    ]
    for sql in attempts:
        with pytest.raises(example.SqlRefused):
            pipe.db.query(sql)
    assert _fingerprint(pipe) == before
    assert not pathlib.Path("/tmp/evil.db").exists()
    assert not pathlib.Path("/tmp/stolen.db").exists()
    # and the legitimate path still works
    assert pipe.db.query("SELECT COUNT(*) AS n FROM orders") == [{"n": ROWS}]


def test_even_a_writable_statement_would_not_land_because_the_copy_is_query_only(
    example: Any, pipe: Any
) -> None:
    import sqlite3

    conn = pipe.db._conn  # noqa: SLF001
    conn.set_authorizer(
        None
    )  # take the authorizer away: query_only is the second layer
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM orders")
    finally:
        conn.set_authorizer(example._authorizer)  # noqa: SLF001
    assert pipe.db.query("SELECT COUNT(*) AS n FROM orders") == [{"n": ROWS}]
    with pytest.raises(example.SqlRefused, match="not authorized"):
        pipe.db.query("DELETE FROM orders")


def test_results_are_capped_and_runaway_statements_are_stopped(
    example: Any, pipe: Any
) -> None:
    with pytest.raises(example.SqlRefused, match="more than"):
        pipe.db.query("SELECT * FROM orders")  # 1500 > 1000
    assert len(pipe.db.query("SELECT * FROM orders LIMIT 1000")) == 1000
    start = time.perf_counter()
    with pytest.raises(example.SqlRefused, match="interrupt"):
        pipe.db.query(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
        )
    assert time.perf_counter() - start < 5
    with pytest.raises(example.SqlRefused, match="interrupt"):
        pipe.db.query(
            "SELECT COUNT(*) FROM orders a, orders b, orders c, orders d"  # 5e12 row pairs
        )
    # the tool still works after being interrupted
    assert pipe.db.query("SELECT 1 AS one") == [{"one": 1}]


def test_parameters_are_bound_not_interpolated(example: Any, pipe: Any) -> None:
    evil = "x' OR '1'='1"
    assert pipe.db.query(
        "SELECT COUNT(*) AS n FROM customers WHERE region = ?", [evil]
    ) == [{"n": 0}]
    named = pipe.db.query(
        "SELECT COUNT(*) AS n FROM customers WHERE region = :r", {"r": "North"}
    )
    assert (
        named[0]["n"]
        == _all(pipe, "SELECT COUNT(*) FROM customers WHERE region='North'")[0][0]
    )
    for bad in (object(), [object()], [[1]], "North"):
        with pytest.raises(example.SqlRefused):
            pipe.db.query("SELECT ?", bad)


def test_the_refusals_reach_both_sandboxes_as_catchable_errors(
    example: Any, pipe: Any
) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    pipe.new_turn()
    with pipe.monty.checkout() as session:
        caught = session.feed_run(
            """
try:
    query("DROP TABLE orders")
    out = "ran"
except ValueError as e:
    out = "refused"
out
""",
            external_lookup=pipe._external(),  # noqa: SLF001
        )
        assert caught == "refused"
        with pytest.raises(MontyRuntimeError, match="not authorized"):
            session.feed_run(
                "query('ATTACH DATABASE \":memory:\" AS x')",
                external_lookup=pipe._external(),  # noqa: SLF001
            )
    with pytest.raises(JavaScriptError, match="host function failed"):
        pipe.rt.eval("query('DROP TABLE orders')")
    with pytest.raises(JavaScriptError, match="host function failed"):
        pipe.rt.eval("query('PRAGMA query_only = OFF')")
    assert pipe.rt.eval("query('SELECT COUNT(*) AS n FROM orders')[0].n") == ROWS


# ---------------------------------------------------------------------------------------------
# One budget, two sandboxes
# ---------------------------------------------------------------------------------------------


def test_the_shared_budget_stops_javascript_after_monty_spends_it(
    pipe: Any,
) -> None:
    from pydeno import JavaScriptError

    budget = pipe.new_turn()
    budget.left = 2
    with pipe.monty.checkout() as session:
        session.feed_run(
            "query('SELECT 1 AS a')\nquery('SELECT 2 AS a')",
            external_lookup=pipe._external(),  # noqa: SLF001
        )
    assert budget.left == 0
    with pytest.raises(JavaScriptError, match="host function failed"):
        pipe.rt.eval("query('SELECT 1 AS a')")
    with pytest.raises(JavaScriptError, match="host function failed"):
        pipe.rt.eval("describe_schema()")


def test_the_shared_budget_stops_monty_after_javascript_spends_it(pipe: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    budget = pipe.new_turn()
    budget.left = 2
    assert pipe.rt.eval("query('SELECT 1 AS a'); describe_schema(); 1") == 1
    assert budget.left == 0
    with pipe.monty.checkout() as session:
        with pytest.raises(MontyRuntimeError, match="budget exhausted"):
            session.feed_run(
                "query('SELECT 1 AS a')",
                external_lookup=pipe._external(),  # noqa: SLF001
            )
    # a new turn, a new budget
    pipe.new_turn()
    assert pipe.rt.eval("query('SELECT 1 AS a')[0].a") == 1


def test_a_task_that_overspends_fails_in_the_language_that_hit_the_limit(
    example: Any,
) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    # revenue_by_month needs one call in Monty and one in JavaScript
    with example.SqlChartPipeline(rows=300, budget_calls=1) as one:
        with pytest.raises(JavaScriptError, match="host function failed"):
            one.render("revenue_by_month")
        assert one.budget.left == 0
        # cohort_retention needs two calls in Monty alone
        with pytest.raises(MontyRuntimeError, match="budget exhausted"):
            one.render("cohort_retention")
    with example.SqlChartPipeline(rows=300, budget_calls=2) as two:
        svg, stats, _ = two.render("revenue_by_month")
        assert stats["tool_calls"] == 2 and svg.startswith("<svg")


def test_neither_sandbox_can_reach_outside(example: Any, pipe: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    pipe.new_turn()
    with pipe.monty.checkout() as session:
        with pytest.raises(MontyRuntimeError):
            session.feed_run(example.NASTY_PYTHON)
        for attempt in (
            "import os\nos.system('id')",
            "import sqlite3",
            "import subprocess",
            "__import__('os')",
        ):
            with pytest.raises(MontyRuntimeError):
                session.feed_run(attempt, external_lookup=pipe._external())  # noqa: SLF001
        # the only host objects Monty sees are the two tools, not the database handle
        with pytest.raises(MontyRuntimeError):
            session.feed_run("db", external_lookup=pipe._external())  # noqa: SLF001
    for attempt in (
        example.NASTY_JAVASCRIPT,
        "process.env",
        "require('fs')",
        "new XMLHttpRequest()",
        "Deno.readTextFileSync('/etc/passwd')",
        "tools.db",
    ):
        with pytest.raises(JavaScriptError):
            pipe.rt.eval(attempt)
    assert pipe.rt.sandbox in ("seatbelt", "landlock+seccomp")
