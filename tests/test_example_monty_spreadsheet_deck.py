"""`examples/monty_spreadsheet_deck.py`: Monty analyses a spreadsheet, pptxgenjs builds the deck.

The tests hold the example to its claims: the .pptx is a real OOXML package with the expected
slides and native chart parts, the findings equal an independent plain-Python computation, the
narrative names the real top item, the analysis is deterministic, one tool budget stops both
sandboxes, and neither can reach outside.
"""

from __future__ import annotations

import importlib.util
import io
import math
import pathlib
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "examples"
    / "monty_spreadsheet_deck.py"
)
SIZE = 300

Rendered = tuple[bytes, dict[str, Any], dict[str, float]]


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_spreadsheet_deck", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> Any:
    return _load_example()


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.PIPELINE()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> Rendered:
    return pipe.render(SIZE)


def _reference(example: Any, count: int) -> dict[str, Any]:
    """The findings, computed independently in plain CPython."""
    rows = example.make_rows(count)
    by_month: dict[str, float] = {}
    by_item: dict[str, float] = {}
    for r in rows:
        by_month[r["month"]] = by_month.get(r["month"], 0.0) + r["amount"]
        by_item[r["item"]] = by_item.get(r["item"], 0.0) + r["amount"]
    amounts = [r["amount"] for r in rows]
    mean = sum(amounts) / len(amounts)
    std = math.sqrt(sum((a - mean) ** 2 for a in amounts) / len(amounts))
    months = sorted(by_month)
    ranked = sorted(by_item, key=lambda k: (-by_item[k], k))
    return {
        "total": round(sum(amounts), 2),
        "monthly": [{"month": m, "total": round(by_month[m], 2)} for m in months],
        "growth": [
            {
                "month": months[i],
                "pct": round(
                    (by_month[months[i]] - by_month[months[i - 1]])
                    / by_month[months[i - 1]]
                    * 100.0,
                    1,
                ),
            }
            for i in range(1, len(months))
        ],
        "ranked": ranked,
        "item_totals": {k: round(by_item[k], 2) for k in ranked},
        "outliers": [r for r in rows if r["amount"] > mean + 4.0 * std],
    }


def test_the_deck_is_a_valid_pptx_with_slides_and_native_charts(
    rendered: Rendered,
) -> None:
    pptx, stats, _ = rendered
    zf = zipfile.ZipFile(io.BytesIO(pptx))
    assert zf.testzip() is None
    names = zf.namelist()
    slides = [
        n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")
    ]
    charts = [
        n for n in names if n.startswith("ppt/charts/chart") and n.endswith(".xml")
    ]
    assert len(slides) == stats["slides"] == 5
    assert len(charts) == stats["charts"] == 2
    content_types = zf.read("[Content_Types].xml").decode()
    assert content_types.count("drawingml.chart+xml") == 2
    for n in range(1, 6):
        assert f"/ppt/slides/slide{n}.xml" in content_types
    assert zf.read("ppt/charts/chart1.xml").count(b"<c:ser>") >= 1
    assert zf.read("ppt/slides/slide5.xml").count(b"<a:tbl>") == 1  # the table slide


def test_findings_equal_an_independent_computation(
    example: Any, rendered: Rendered
) -> None:
    _, stats, _ = rendered
    f = stats["findings"]
    ref = _reference(example, SIZE)
    assert f["rows"] == SIZE
    assert f["total"] == pytest.approx(ref["total"], abs=0.01)
    assert [m["month"] for m in f["monthly"]] == [m["month"] for m in ref["monthly"]]
    for got, want in zip(f["monthly"], ref["monthly"], strict=True):
        assert got["total"] == pytest.approx(want["total"], abs=0.01)
    for got, want in zip(f["growth"], ref["growth"], strict=True):
        assert got["month"] == want["month"]
        assert got["pct"] == pytest.approx(want["pct"], abs=0.11)
    assert [i["item"] for i in f["items"]] == ref["ranked"]
    for entry in f["items"]:
        assert entry["total"] == pytest.approx(
            ref["item_totals"][entry["item"]], abs=0.01
        )
    assert f["top"] == f["items"][:5]
    assert f["bottom"] == f["items"][-3:]
    assert [(o["month"], o["item"], o["amount"]) for o in f["outliers"]] == [
        (r["month"], r["item"], r["amount"]) for r in ref["outliers"]
    ]
    assert len(f["outliers"]) == 1  # the planted one


def test_the_narrative_mentions_the_real_top_item(
    example: Any, rendered: Rendered
) -> None:
    _, stats, _ = rendered
    narrative = stats["findings"]["narrative"]
    ref = _reference(example, SIZE)
    assert isinstance(narrative, list) and all(isinstance(s, str) for s in narrative)
    assert any(ref["ranked"][0] in s and "largest" in s for s in narrative)
    assert any(ref["ranked"][-1] in s and "smallest" in s for s in narrative)
    assert any("outlier" in s for s in narrative)


def test_the_top_item_is_in_the_deck_itself(example: Any, rendered: Rendered) -> None:
    pptx, _, _ = rendered
    zf = zipfile.ZipFile(io.BytesIO(pptx))
    top = _reference(example, SIZE)["ranked"][0]
    assert top.encode() in zf.read("ppt/slides/slide2.xml")  # findings
    assert top.encode() in zf.read("ppt/slides/slide5.xml")  # table


def test_the_analysis_is_deterministic(pipe: Any) -> None:
    first = pipe.prepare(SIZE)
    second = pipe.prepare(SIZE)
    assert first == second
    a, _, _ = pipe.render(SIZE)
    b, _, t = pipe.render(SIZE)
    assert set(t) == {"monty_prepare", "js_build", "total"}
    # Slide XML is identical. (Whole-file bytes are not: pptxgenjs keeps global counters, e.g. for
    # embedded workbook names, so a reused runtime numbers its parts further on each deck.)
    za, zb = zipfile.ZipFile(io.BytesIO(a)), zipfile.ZipFile(io.BytesIO(b))
    slides = [n for n in za.namelist() if n.startswith("ppt/slides/slide")]
    assert slides and slides == [
        n for n in zb.namelist() if n.startswith("ppt/slides/slide")
    ]
    for n in slides:
        if n.endswith(".xml"):
            assert za.read(n) == zb.read(n)


def test_the_sheet_wrapper_is_read_only_and_paged(example: Any) -> None:
    sheet = example.Sheet(example.make_rows(250))
    assert sheet.info()["rows"] == 250
    assert len(sheet.rows(0, 10_000)) == example.PAGE
    page = sheet.rows(0, 5)
    page[0]["amount"] = -1  # a copy: the sheet is unaffected
    assert sheet.rows(0, 1)[0]["amount"] != -1
    assert not hasattr(sheet, "__setitem__")


def test_one_budget_stops_both_sandboxes(example: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    # Monty alone needs 3 calls for 200 rows (info + 2 pages); the JS half needs 2 more.
    tight = example.DeckPipeline(budget_calls=4)
    try:
        tight.prepare(200)
        assert tight.budget.left == 1  # Monty spent 3 of the shared 4
        with pytest.raises(JavaScriptError):
            import asyncio

            asyncio.run(tight.rt.eval_async(example.MODEL_JAVASCRIPT, timeout=60))
        assert tight.budget.left == 0
        with pytest.raises(MontyRuntimeError, match="budget"):
            tight.prepare(200)  # now Monty is cut off too
    finally:
        tight.close()


def test_a_budget_too_small_for_monty_stops_monty(example: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    tiny = example.DeckPipeline(budget_calls=2)
    try:
        with pytest.raises(MontyRuntimeError, match="budget"):
            tiny.prepare(1000)  # needs 11 calls
    finally:
        tiny.close()


def test_neither_sandbox_can_escape(pipe: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    with pipe.monty.checkout() as session:
        with pytest.raises(MontyRuntimeError):
            session.feed_run("open('/etc/passwd').read()")
        with pytest.raises(MontyRuntimeError):
            session.feed_run("import os\nos.environ")
    for attempt in ("fetch('https://example.com')", "process.env", "require('fs')"):
        with pytest.raises(JavaScriptError):
            pipe.rt.eval(attempt)
    assert pipe.rt.sandbox in ("seatbelt", "landlock+seccomp")
