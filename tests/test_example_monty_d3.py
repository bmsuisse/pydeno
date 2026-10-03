"""`examples/monty_d3_network.py`: Monty prepares the graph, d3 lays it out and draws it.

An example that is not tested is a screenshot. These tests hold it to its claims: the SVG is
well-formed XML with every node and edge in it, Monty computes exactly what CPython would for the
same program, its PageRank is a real PageRank, the d3-force layout is deterministic, the vendored
bytes are the pinned bytes, and neither half can reach outside.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "monty_d3_network.py"
BUNDLE = ROOT / "vendor" / "libs" / "d3-force-3.0.0-delaunay-6.0.4.bundle.js"
BUNDLE_SHA256 = "67e190242161066fea190c201ea2f97ea4d3d97fb1ba9f2f577f33d5dc5b97f7"
N = 120
SVG = "{http://www.w3.org/2000/svg}"


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_d3_network", EXAMPLE)
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
    pipeline.load_libs()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> tuple[str, dict[str, Any], dict[str, float]]:
    return pipe.render(N)


def _reference_program(example: Any) -> str:
    return example.MODEL_PYTHON.replace('\n{\n    "n": N', '\nresult = {\n    "n": N')


def test_the_vendored_bundle_is_the_pinned_bytes() -> None:
    assert hashlib.sha256(BUNDLE.read_bytes()).hexdigest() == BUNDLE_SHA256


def test_the_svg_is_well_formed_with_every_node_and_edge(
    rendered: tuple[str, dict[str, Any], dict[str, float]], pipe: Any
) -> None:
    svg, stats, timings = rendered
    root = ET.fromstring(svg)  # raises on anything that is not well-formed XML
    assert root.tag == f"{SVG}svg"
    groups = {g.get("id"): g for g in root.iter(f"{SVG}g") if g.get("id")}
    assert len(list(groups["nodes"].iter(f"{SVG}circle"))) == N
    edges = list(groups["edges"].iter(f"{SVG}path"))
    assert len(edges) == len(pipe._network["edges"]) > N  # noqa: SLF001
    assert len(list(groups["voronoi"].iter(f"{SVG}path"))) == N
    assert stats["treemapLeaves"] == N
    assert len(list(groups["treemap"].iter(f"{SVG}rect"))) == 1 + 4 + N
    assert set(timings) == {"monty_prepare", "js_build", "total"}


def test_layout_stats_are_sane(
    rendered: tuple[str, dict[str, Any], dict[str, float]], pipe: Any
) -> None:
    _, stats, _ = rendered
    assert 1 < stats["iterations"] <= 500
    assert stats["springEnergy"] > 0
    assert 0 < stats["nearestMin"] <= stats["nearestMean"] <= stats["nearestMax"]
    assert stats["treemapTotal"] == sum(x["size"] for x in pipe._network["nodes"])  # noqa: SLF001
    assert all(0 <= x <= 1000 and 0 <= y <= 720 for x, y in stats["positions"])


def test_monty_computes_exactly_what_cpython_computes(example: Any, pipe: Any) -> None:
    """The data half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {"N": N}
    exec(_reference_program(example), namespace)  # noqa: S102 - the example's own fixed program
    assert pipe.prepare(N) == namespace["result"]


def test_pagerank_sums_to_one_and_matches_an_independent_reference(pipe: Any) -> None:
    net = pipe.prepare(N)
    assert net["pagerank_sum"] == pytest.approx(1.0, abs=1e-9)
    nodes, edges = net["nodes"], net["edges"]
    assert len(nodes) == N

    # An independent formulation: a dense column-stochastic matrix, plain Python, to convergence.
    out: dict[int, list[int]] = {i: [] for i in range(N)}
    for s, d in edges:
        out[s].append(d)
    matrix = [[0.0] * N for _ in range(N)]
    for s, targets in out.items():
        for d in targets:
            matrix[d][s] += 1.0 / len(targets)
        if not targets:
            for d in range(N):
                matrix[d][s] = 1.0 / N
    rank = [1.0 / N] * N
    for _ in range(500):
        rank = [
            0.15 / N + 0.85 * sum(matrix[i][j] * rank[j] for j in range(N))
            for i in range(N)
        ]
    for node in nodes:
        assert node["pagerank"] == pytest.approx(rank[node["id"]], abs=1e-9)
    # hubs win: the best-ranked package is a heavily depended-on one
    best = max(nodes, key=lambda x: x["pagerank"])
    assert best["indegree"] == net["max_indegree"] or best["indegree"] >= 10
    assert sum(t["packages"] for t in net["tier_stats"]) == N
    assert sum(x["indegree"] for x in nodes) == len(edges)


def test_the_d3_force_layout_is_deterministic(pipe: Any, example: Any) -> None:
    first_svg, first, _ = pipe.render(N)
    second_svg, second, _ = pipe.render(N)
    assert first["positions"] == second["positions"]
    assert first["iterations"] == second["iterations"]
    assert first_svg == second_svg
    # and across a brand-new worker process
    fresh = example.PIPELINE()
    try:
        fresh.load_libs()
        third_svg, third, _ = fresh.render(N)
    finally:
        fresh.close()
    assert third["positions"] == first["positions"]
    assert third_svg == first_svg


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
