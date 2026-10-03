"""`examples/monty_echarts_dashboard.py`: Monty analyses the data, ECharts draws the dashboard.

An example that is not tested is a screenshot. These tests hold it to its claims: every chart is
well-formed SVG, the page is inert (no scripts, no requests), Monty computes exactly what CPython
would for the same program, the analysis agrees with an independent reference written here, every
injected anomaly is found, the vendored bundle is the pinned one, and neither half can reach outside.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import pathlib
import re
import statistics
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "monty_echarts_dashboard.py"
DAYS = 180

ECHARTS_SHA256 = "b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0"

Rendered = tuple[str, dict[str, Any], dict[str, float]]


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_echarts_dashboard", EXAMPLE)
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
    pipeline.load_echarts()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> Rendered:
    return pipe.render(DAYS)


@pytest.fixture(scope="module")
def data(pipe: Any, rendered: Rendered) -> dict[str, Any]:
    return pipe._data  # noqa: SLF001 - what Monty handed over


def _svgs(page: str) -> list[str]:
    return re.findall(r"<svg\b.*?</svg>", page, flags=re.S)


def test_the_bundle_is_the_pinned_one(example: Any) -> None:
    raw = (example.LIBS / example.ECHARTS_BUNDLE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ECHARTS_SHA256
    assert (ROOT / "vendor" / "libs" / "LICENSES" / "echarts.LICENSE").is_file()


def test_sizes_and_pipeline_are_exposed(example: Any) -> None:
    assert example.SIZES == sorted(example.SIZES)
    assert all(s >= 60 for s in example.SIZES)
    assert example.PIPELINE is example.DashboardPipeline


def test_every_chart_is_well_formed_svg(rendered: Rendered) -> None:
    page, stats, _ = rendered
    svgs = _svgs(page)
    assert len(svgs) == stats["charts"] == 4
    for svg in svgs:
        root = ET.fromstring(svg)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert len(list(root.iter())) > 20  # real drawing, not an empty shell
    titles = " ".join("".join(ET.fromstring(s).itertext()) for s in svgs)
    for text in ("moving average", "Weekly totals", "Correlation", "Share of total"):
        assert text in titles
    assert stats["echarts"] == "6.1.0"


def test_the_page_is_inert(rendered: Rendered) -> None:
    page, _, _ = rendered
    assert not re.search(r"<script", page, re.I)
    assert not re.search(r"\bon\w+\s*=", page, re.I)  # no event-handler attributes
    assert "<link" not in page and "<img" not in page and "@import" not in page
    urls = re.findall(r"https?://[^\s\"'<>)]+", page)
    assert urls  # the SVG namespaces are there
    assert set(urls) <= {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"}
    for url in urls:  # and only ever as a namespace declaration
        assert re.search(rf'xmlns(:\w+)?="{re.escape(url)}"', page)


def test_monty_computes_exactly_what_cpython_computes(
    example: Any, data: dict[str, Any]
) -> None:
    """The data half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {"DAYS": DAYS}
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "days"', '\nresult = {\n    "days"'
    )
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, run as the reference
    assert namespace["result"] == data


def test_moving_average_matches_a_reference(data: dict[str, Any]) -> None:
    for series, ma in zip(data["series"], data["ma"], strict=True):
        assert len(ma) == DAYS
        assert ma[:3] == [None] * 3 and ma[-3:] == [None] * 3
        for d in range(3, DAYS - 3):
            assert ma[d] == pytest.approx(
                statistics.fmean(series[d - 3 : d + 4]), abs=0.006
            )


def test_zscores_and_anomalies_match_a_reference(data: dict[str, Any]) -> None:
    flagged: dict[tuple[int, int], float] = {}
    for r, (series, ma) in enumerate(zip(data["series"], data["ma"], strict=True)):
        resid = [(series[d] - ma[d]) / ma[d] for d in range(3, DAYS - 3)]
        mu, sd = statistics.fmean(resid), statistics.pstdev(resid)
        for i, x in enumerate(resid):
            z = (x - mu) / sd
            if abs(z) > 3.0:
                flagged[(r, i + 3)] = z
    reported = {(r, d): z for r, d, _, z in data["anomalies"]}
    assert reported.keys() == flagged.keys()
    for key, z in flagged.items():
        assert reported[key] == pytest.approx(z, abs=0.01)
    for r, d, value, _ in data["anomalies"]:
        assert value == data["series"][r][d]


def test_all_injected_anomalies_are_found_and_nothing_else(
    data: dict[str, Any],
) -> None:
    injected = {(r, d) for r, d, _ in data["injected"]}
    found = {(r, d) for r, d, _, _ in data["anomalies"]}
    assert len(injected) == 5
    assert found == injected
    for r, d, factor in data["injected"]:  # spikes are positive z, dips negative
        z = next(a[3] for a in data["anomalies"] if (a[0], a[1]) == (r, d))
        assert (z > 0) == (factor > 1)


def test_correlations_trend_weekly_and_share_match_a_reference(
    data: dict[str, Any],
) -> None:
    series = data["series"]
    n = len(series)
    for i in range(n):
        for j in range(n):
            expected = statistics.correlation(series[i], series[j])
            assert data["correlation"][i][j] == pytest.approx(expected, abs=1e-4)
        assert data["correlation"][i][i] == 1.0
        assert data["correlation"][i] == [data["correlation"][k][i] for k in range(n)]
    total = [sum(col) for col in zip(*series, strict=True)]
    fit = statistics.linear_regression(list(range(DAYS)), total)
    assert data["trend"]["slope"] == pytest.approx(fit.slope, abs=1e-4)
    assert data["trend"]["intercept"] == pytest.approx(fit.intercept, abs=0.01)
    assert data["trend"]["slope"] > 0  # the synthetic data trends up
    r2 = statistics.correlation(list(range(DAYS)), total) ** 2
    assert data["trend"]["r2"] == pytest.approx(r2, abs=1e-4)
    for r, row in enumerate(series):
        weeks = [round(sum(row[w * 7 : w * 7 + 7]), 2) for w in range(DAYS // 7)]
        assert data["weekly"][r] == pytest.approx(weeks, abs=0.01)
        assert data["share"][r] == pytest.approx(sum(row), abs=0.01)
    assert math.isclose(sum(data["share"]), sum(total), rel_tol=1e-9)


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    first, _, _ = pipe.render(DAYS)
    second, _, _ = pipe.render(DAYS)
    # ECharts numbers its CSS classes and clip/gradient ids (`zr8-cls-58`, `zr8-c0`, `zr8-g1`) from
    # a process-wide counter, so the two pages differ only in those labels; everything else, every
    # coordinate and colour, is identical.
    scrub = re.compile(r"zr\d+-(?:cls-|c|g)\d+")
    assert scrub.sub("CLS", first) == scrub.sub("CLS", second)


def test_stats_and_timings_are_reported(rendered: Rendered) -> None:
    _, stats, timings = rendered
    assert stats["days"] == DAYS and stats["regions"] == 4
    assert stats["injected_found"] == stats["injected"] == 5
    assert stats["svg_bytes"] > 10_000
    assert set(timings) >= {"monty_prepare", "js_build", "total"}
    assert timings["total"] >= timings["monty_prepare"] + timings["js_build"] - 1e-6


def test_neither_half_can_reach_outside(pipe: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    with pipe.monty.checkout() as session:
        with pytest.raises(MontyRuntimeError):
            session.feed_run("open('/etc/passwd').read()")
    for attempt in ("fetch('https://example.com')", "process.env", "require('fs')"):
        with pytest.raises(JavaScriptError):
            pipe.rt.eval(attempt)
    assert pipe.rt.sandbox in ("seatbelt", "landlock+seccomp")
