"""`examples/monty_three_orbits.py`: Monty integrates the orbits, three.js builds the scene.

An example that is not tested is a screenshot. These tests hold it to its claims: the glTF is a
valid glTF with the right contents, Monty computes exactly what CPython would for the same
program, the integrator conserves energy (and the bound is tight enough to notice a bad one), the
whole pipeline is deterministic, the geometry answers match an independent computation, and
neither half can reach outside.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import math
import pathlib
import struct
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what these pipelines ask for, so they need a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "examples"
    / "monty_three_orbits.py"
)
STEPS = 1500
# Relative energy error the leapfrog may reach over STEPS steps at the example's time step. A
# deliberately coarse step (DT_COARSE) must blow through it, or the bound proves nothing.
DRIFT_BOUND = 1e-3
DT_COARSE = 0.03


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_three_orbits", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> Any:
    return _load_example()


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.OrbitsPipeline()
    pipeline.load_three()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> tuple[bytes, dict[str, Any], dict[str, float]]:
    return pipe.render(STEPS)


def _gltf_json(glb: bytes) -> dict[str, Any]:
    magic, version, length = struct.unpack("<4sII", glb[:12])
    assert magic == b"glTF" and version == 2 and length == len(glb)
    chunk_len, chunk_type = struct.unpack("<I4s", glb[12:20])
    assert chunk_type == b"JSON"
    return json.loads(glb[20 : 20 + chunk_len])


def test_the_export_is_a_valid_gltf_with_the_scene_in_it(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]],
) -> None:
    glb, stats, _ = rendered
    doc = _gltf_json(glb)
    counts = [a["count"] for a in doc["accessors"]]
    bodies, samples = stats["bodies"], stats["samples"]
    assert bodies == 6
    assert len(doc["meshes"]) == 2 * bodies  # a trail and a sphere per body
    assert (
        counts.count(samples * 6) >= bodies
    )  # tube vertices: one ring of 6 per sample
    attributes = [
        a for m in doc["meshes"] for p in m["primitives"] for a in p["attributes"]
    ]
    assert attributes.count("COLOR_0") == bodies  # the speed colouring made it out


def test_the_trails_are_real_paths(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]], pipe: Any
) -> None:
    _, stats, _ = rendered
    paths = pipe._orbits["paths"]  # noqa: SLF001 - what Monty handed over
    assert len(paths) == 6 and all(len(p) == stats["samples"] for p in paths)
    for path, length in zip(paths, stats["pathLength"], strict=True):
        expected = sum(math.dist(a, b) for a, b in itertools.pairwise(path))
        assert length == pytest.approx(expected, rel=1e-9)
    # the figure-eight bodies travel at about unit speed: roughly speed times elapsed time (3.75)
    assert all(length > 3 for length in stats["pathLength"][:3])
    assert 0 < stats["speed"][0] < stats["speed"][1]


def test_monty_computes_exactly_what_cpython_computes(example: Any, pipe: Any) -> None:
    """The data half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {"STEPS": STEPS, "DT": example.DT}
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "steps"', '\nresult = {\n    "steps"'
    )
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, run as the reference
    expected = namespace["result"]
    actual = pipe.prepare(STEPS)
    assert actual == expected


def test_energy_drift_stays_under_the_bound_and_the_bound_means_something(
    example: Any, pipe: Any
) -> None:
    good = pipe.prepare(STEPS)
    assert good["drift"] < DRIFT_BOUND
    assert good["energy0"] < 0  # a bound system
    # same physics, same number of steps, a time step twelve times too big: the check must fail
    assert pipe.prepare(STEPS, DT_COARSE)["drift"] > DRIFT_BOUND


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    first, _, _ = pipe.render(STEPS)
    second, _, _ = pipe.render(STEPS)
    assert first == second


def test_closest_approaches_match_an_independent_computation(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]], pipe: Any
) -> None:
    _, stats, _ = rendered
    paths = pipe._orbits["paths"]  # noqa: SLF001
    reported = {(c["a"], c["b"]): c for c in stats["closest"]}
    assert len(reported) == 6 * 5 // 2
    for a, b in itertools.combinations(range(6), 2):
        distances = [math.dist(p, q) for p, q in zip(paths[a], paths[b], strict=True)]
        assert reported[(a, b)]["distance"] == pytest.approx(min(distances), rel=1e-9)
        assert reported[(a, b)]["sample"] == distances.index(min(distances))


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
