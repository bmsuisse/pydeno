"""`examples/monty_three_city.py`: Monty lays out a city, three.js measures which roofs get sun.

An example that is not tested is a screenshot. These tests hold it to its claims: the glTF is a
valid glTF with the right contents, Monty computes exactly what CPython would for the same
program, the layout never puts two buildings on top of each other, the pipeline is deterministic,
the sun analysis is right on scenes small enough to check by hand, and neither half can reach
outside.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "monty_three_city.py"
)
SIZE = 4


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_three_city", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> Any:
    return _load_example()


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.CityPipeline()
    pipeline.load_three()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> tuple[bytes, dict[str, Any], dict[str, float]]:
    return pipe.render(SIZE)


def _gltf_json(glb: bytes) -> dict[str, Any]:
    magic, version, length = struct.unpack("<4sII", glb[:12])
    assert magic == b"glTF" and version == 2 and length == len(glb)
    chunk_len, chunk_type = struct.unpack("<I4s", glb[12:20])
    assert chunk_type == b"JSON"
    return json.loads(glb[20 : 20 + chunk_len])


def _analyse(
    pipe: Any, buildings: list[list[float]], azimuth: float, elevation: float
) -> list[float]:
    """Run the example's sun analysis on a hand-built city and return per-building fractions."""
    pipe._city = {  # noqa: SLF001 - stand in for what Monty would hand over
        "extent": 100.0,
        "buildings": buildings,
    }
    return pipe.rt.eval(f"buildScene({azimuth}, {elevation}, 4)")["fractions"]


def test_the_export_is_a_valid_gltf_with_the_scene_in_it(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]],
) -> None:
    glb, stats, timings = rendered
    doc = _gltf_json(glb)
    # one mesh per building plus the ground
    assert len(doc["meshes"]) == stats["buildings"] + 1
    assert any(a["count"] == 24 for a in doc["accessors"])  # a box's vertices
    assert any(
        "COLOR_0" in p["attributes"] for m in doc["meshes"] for p in m["primitives"]
    )
    assert set(timings) == {"monty_prepare", "three_build", "glb_export", "total"}


def test_the_sun_analysis_reports_a_ranking(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]],
) -> None:
    _, stats, _ = rendered
    fractions = stats["fractions"]
    assert len(fractions) == stats["buildings"] > 0
    assert all(0.0 <= f <= 1.0 for f in fractions)
    assert 0.0 < stats["citySunlitPercent"] < 100.0
    assert len(stats["sunniest"]) == len(stats["shadiest"]) == 5
    assert [e["sunlit"] for e in stats["sunniest"]] == sorted(
        (e["sunlit"] for e in stats["sunniest"]), reverse=True
    )
    assert stats["sunniest"][0]["sunlit"] == max(fractions)
    assert stats["shadiest"][0]["sunlit"] == min(fractions)
    # the overall percentage is the mean of the per-building fractions (equal sample counts)
    assert stats["citySunlitPercent"] == pytest.approx(
        100 * sum(fractions) / len(fractions)
    )


def test_monty_computes_exactly_what_cpython_computes(example: Any, pipe: Any) -> None:
    """The layout half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {"N": SIZE, "SEED": example.SEED}
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "blocks": N', '\nresult = {\n    "blocks": N'
    )
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, run as the reference
    assert pipe.prepare(SIZE) == namespace["result"]


@pytest.mark.parametrize("size", [3, 4, 6])
def test_no_two_buildings_overlap(pipe: Any, size: int) -> None:
    city = pipe.prepare(size)
    boxes = city["buildings"]
    assert len(boxes) > 0
    half = city["extent"] / 2
    for x, z, w, d, h in boxes:
        assert w > 0 and d > 0 and h > 0
        assert -half <= x and x + w <= half  # inside the city
        assert -half <= z and z + d <= half
    for a in range(len(boxes)):
        ax, az, aw, ad, _ = boxes[a]
        for b in range(a + 1, len(boxes)):
            bx, bz, bw, bd, _ = boxes[b]
            apart = ax + aw <= bx or bx + bw <= ax or az + ad <= bz or bz + bd <= az
            assert apart, f"buildings {a} and {b} overlap"


def test_the_city_has_a_mix_of_zones(pipe: Any) -> None:
    city = pipe.prepare(8)
    assert city["low_rise"] > 0 and city["towers"] > 0 and city["parks"] > 0
    assert city["tallest"] >= 24


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    first, stats_a, _ = pipe.render(SIZE)
    second, stats_b, _ = pipe.render(SIZE)
    assert first == second
    assert stats_a == stats_b


def test_a_tall_tower_shades_its_neighbour_on_the_shadow_side_only(pipe: Any) -> None:
    # tower in the middle; a short building 15 units either side along x
    tower = [0.0, 0.0, 10.0, 10.0, 50.0]
    west = [-25.0, 0.0, 10.0, 10.0, 5.0]
    east = [15.0, 0.0, 10.0, 10.0, 5.0]
    # sun in the east (azimuth 90), low: the shadow falls westward
    _, west_fraction, east_fraction = _analyse(pipe, [tower, west, east], 90.0, 20.0)
    assert east_fraction == 1.0  # sun side: nothing between it and the sun
    assert west_fraction < 0.5  # shadow side: darkened by the tower
    # flip the sun to the west and the roles swap
    _, west_fraction, east_fraction = _analyse(pipe, [tower, west, east], 270.0, 20.0)
    assert west_fraction == 1.0
    assert east_fraction < 0.5


def test_the_tower_itself_is_lit_when_nothing_taller_is_near(pipe: Any) -> None:
    tower = [0.0, 0.0, 10.0, 10.0, 50.0]
    short = [-25.0, 0.0, 10.0, 10.0, 5.0]
    assert _analyse(pipe, [tower, short], 90.0, 20.0)[0] == 1.0


def test_sun_straight_overhead_lights_every_roof(pipe: Any) -> None:
    buildings = [
        [0.0, 0.0, 10.0, 10.0, 50.0],
        [12.0, 0.0, 10.0, 10.0, 5.0],
        [-14.0, 3.0, 8.0, 8.0, 20.0],
    ]
    assert _analyse(pipe, buildings, 0.0, 90.0) == [1.0, 1.0, 1.0]


def test_sun_below_the_horizon_lights_nothing(pipe: Any) -> None:
    buildings = [
        [0.0, 0.0, 10.0, 10.0, 50.0],
        [12.0, 0.0, 10.0, 10.0, 5.0],
    ]
    assert _analyse(pipe, buildings, 90.0, -10.0) == [0.0, 0.0]


def test_neither_half_can_reach_outside(example: Any, pipe: Any) -> None:
    from pydantic_monty import MontyRuntimeError

    from pydeno import JavaScriptError

    with pipe.monty.checkout() as session:
        with pytest.raises(MontyRuntimeError):
            session.feed_run("open('/etc/passwd').read()")
    for attempt in ("fetch('https://example.com')", "process.env", "require('fs')"):
        with pytest.raises(JavaScriptError):
            pipe.rt.eval(attempt)
    assert pipe.rt.sandbox in ("seatbelt", "landlock+seccomp")
