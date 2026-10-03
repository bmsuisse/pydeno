"""`examples/monty_turf_geo.py`: Monty cleans the GPS tracks, Turf.js does the geospatial analysis.

An example that is not tested is a screenshot. These tests hold it to its claims: the map is
well-formed SVG, Monty computes exactly what CPython would for the same program, Turf's distances
agree with Monty's own haversine, the coverage area behaves like a union, every stop's nearest depot
matches an independent brute-force computation, the vendored bundle is the one that was reviewed,
and neither half can reach outside.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import pathlib
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "monty_turf_geo.py"
BUNDLE = ROOT / "vendor" / "libs" / "turf-7.4.0.bundle.js"
BUNDLE_SHA256 = "ab93309f52566b6cd998200485d4825be1c434c5c8f81a3e18dde0a92dd63940"
VEHICLES = 6
EARTH_R = 6371008.8  # the radius both Monty's program and Turf use
REL = (
    1e-6  # Turf and Monty use the same radius and formula; they differ only in rounding
)


def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """An independent implementation (atan2 form), so agreement is not shared code."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_turf_geo", EXAMPLE)
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
    pipeline.load_turf()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> tuple[str, dict[str, Any], dict[str, float]]:
    return pipe.render(VEHICLES)


@pytest.fixture(scope="module")
def fleet(pipe: Any, rendered: Any) -> dict[str, Any]:
    return pipe._fleet  # noqa: SLF001 - what Monty handed over for `rendered`


def test_the_bundle_is_the_one_that_was_reviewed() -> None:
    assert hashlib.sha256(BUNDLE.read_bytes()).hexdigest() == BUNDLE_SHA256


def test_the_map_is_well_formed_svg_with_everything_drawn(
    rendered: tuple[str, dict[str, Any], dict[str, float]], example: Any
) -> None:
    svg, stats, timings = rendered
    root = ET.fromstring(svg)  # raises if it is not XML
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert len(root.findall("s:polyline", ns)) == VEHICLES  # one track each
    assert len(root.findall("s:circle", ns)) == stats["stops"]
    assert len(root.findall("s:rect", ns)) == 1 + 4  # background + four depots
    assert stats["zones"] == 4
    assert set(timings) == {"monty_prepare", "js_build", "total"}
    assert example.SIZES and example.PIPELINE is example.GeoPipeline


def test_monty_computes_exactly_what_cpython_computes(
    example: Any, pipe: Any, fleet: dict[str, Any]
) -> None:
    """The data half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {
        "V": VEHICLES,
        "SEED": 7,
        "BUFFER_M": example.BUFFER_METERS,
    }
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "depots"', '\nresult = {\n    "depots"'
    )
    assert program != example.MODEL_PYTHON
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, run as the reference
    assert fleet == namespace["result"]  # every float, every list, exactly


def test_the_data_was_really_dirty_and_really_cleaned(fleet: dict[str, Any]) -> None:
    assert sum(v["dropped"] for v in fleet["vehicles"]) > 0  # spikes were dropped
    for v in fleet["vehicles"]:
        assert v["max_speed_ms"] <= 35.0  # nothing impossible survives
        assert len(v["track"]) > 50
        assert v["raw_points"] > len(v["track"]) // 2


def test_turf_lengths_agree_with_montys_own_haversine(
    rendered: tuple[str, dict[str, Any], dict[str, float]], fleet: dict[str, Any]
) -> None:
    _, stats, _ = rendered
    for turf, monty in zip(stats["vehicles"], fleet["vehicles"], strict=True):
        assert turf["id"] == monty["id"]
        assert turf["lengthKm"] * 1000 == pytest.approx(monty["distance_m"], rel=REL)
        # and against an independent implementation, to a looser stated tolerance (0.01 %)
        t = monty["track"]
        mine = sum(_haversine(*t[i - 1], *t[i]) for i in range(1, len(t)))
        assert turf["lengthKm"] * 1000 == pytest.approx(mine, rel=1e-4)


def test_the_coverage_area_is_a_union_of_the_buffers(
    rendered: tuple[str, dict[str, Any], dict[str, float]],
) -> None:
    _, stats, _ = rendered
    assert len(stats["bufferAreasM2"]) == VEHICLES
    assert stats["largestBufferM2"] <= stats["coverageAreaM2"] <= stats["sumBufferM2"]
    # tracks from different vehicles overlap (shared depots), so it is strictly less than the sum
    assert stats["coverageAreaM2"] < stats["sumBufferM2"]
    # a 150 m buffer of a track of L km covers about 0.3 * L km2
    longest = max(v["lengthKm"] for v in stats["vehicles"])
    assert stats["largestBufferM2"] / 1e6 == pytest.approx(0.3 * longest, rel=0.2)
    assert stats["coverageKm2"] == pytest.approx(stats["coverageAreaM2"] / 1e6)
    assert (
        0 < stats["convexKm2"] and 0 < stats["concaveKm2"] <= stats["convexKm2"] + 1e-9
    )


def test_every_stops_nearest_depot_matches_brute_force(
    rendered: tuple[str, dict[str, Any], dict[str, float]], fleet: dict[str, Any]
) -> None:
    _, stats, _ = rendered
    depots = fleet["depots"]
    stops = [(v["id"], s) for v in fleet["vehicles"] for s in v["stops"]]
    assert len(stats["nearest"]) == len(stops) == stats["stops"]
    for got, (vid, (lon, lat)) in zip(stats["nearest"], stops, strict=True):
        distances = [_haversine(lon, lat, d[0], d[1]) for d in depots]
        best = min(range(len(depots)), key=distances.__getitem__)
        assert got["vehicle"] == vid
        assert got["depot"] == best
        assert got["meters"] == pytest.approx(distances[best], rel=1e-4)


def test_closest_approach_is_bracketed_by_an_independent_bound(
    rendered: tuple[str, dict[str, Any], dict[str, float]], fleet: dict[str, Any]
) -> None:
    """The distance between two polylines is at most the closest vertex pair, and (the vertices
    being at most `L` apart along each track) at least that minus `L/2` for the line it is
    measured to. That brackets Turf's answer without re-implementing it."""
    _, stats, _ = rendered
    vehicles = fleet["vehicles"]
    assert len(stats["closestApproach"]) == VEHICLES * (VEHICLES - 1) // 2
    by_id = {v["id"]: v["track"] for v in vehicles}
    for pair in stats["closestApproach"]:
        ta, tb = by_id[pair["a"]], by_id[pair["b"]]
        upper = min(_haversine(*p, *q) for p in ta for q in tb)
        longest = max(
            _haversine(*t[i - 1], *t[i]) for t in (ta, tb) for i in range(1, len(t))
        )
        assert pair["meters"] <= upper * (1 + 1e-4) + 1e-6
        assert pair["meters"] >= upper - longest / 2 * 1.01 - 1e-6


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    first, _, _ = pipe.render(4)
    second, _, _ = pipe.render(4)
    assert first == second


def test_neither_half_can_reach_outside(pipe: Any) -> None:
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
