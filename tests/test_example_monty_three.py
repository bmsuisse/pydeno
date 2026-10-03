"""`examples/monty_three_terrain.py`: Monty prepares the data, three.js builds the scene.

An example that is not tested is a screenshot. These tests hold it to its claims: the glTF is a
valid glTF with the right contents, Monty computes exactly what CPython would for the same
program (so the data half is trustworthy, not merely fast), the whole pipeline is deterministic,
the raycaster answers real geometry questions, and neither half can reach outside.
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
    pathlib.Path(__file__).resolve().parent.parent
    / "examples"
    / "monty_three_terrain.py"
)
N = 33


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_three_terrain", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> Any:
    return _load_example()


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.TerrainPipeline()
    pipeline.load_three()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> tuple[bytes, dict[str, Any], dict[str, float]]:
    return pipe.render(N)


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
    assert N * N in counts  # the terrain's vertex positions
    assert "EXT_mesh_gpu_instancing" in doc["extensionsUsed"]
    assert stats["trees"] in counts  # one instance per tree
    assert stats["triangles"] == 2 * (N - 1) ** 2
    assert len(doc["meshes"]) == 2


def test_the_terrain_is_in_range_and_trees_stand_on_it(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]], pipe: Any
) -> None:
    _, stats, _ = rendered
    low, high = stats["height"]
    assert 0 <= low <= high <= 32
    assert stats["trees"] > 0
    terrain = pipe._terrain  # noqa: SLF001 - what Monty handed over
    assert len(terrain["heights"]) == N * N
    assert all(0 <= u <= 1 and 0 <= v <= 1 for u, v in terrain["trees"])
    # every tree sits where the program said trees grow: above the beach, below the snow line
    for u, v in terrain["trees"]:
        i, j = round(u * (N - 1)), round(v * (N - 1))
        assert 2.0 < terrain["heights"][j * N + i] < 15.0


def test_monty_computes_exactly_what_cpython_computes(example: Any, pipe: Any) -> None:
    """The data half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {"N": N}
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "n": N', '\nresult = {\n    "n": N'
    )
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, run as the reference
    expected = namespace["result"]
    actual = pipe.prepare(N)
    assert actual["heights"] == expected["heights"]
    assert actual["trees"] == expected["trees"]
    assert actual["mean"] == expected["mean"]


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    first, _, _ = pipe.render(N)
    second, _, _ = pipe.render(N)
    assert first == second


def test_the_raycaster_answers_real_geometry_questions(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]], pipe: Any
) -> None:
    # straight down onto the island's centre hits the terrain; a ray far above it hits nothing
    down = """(() => {
      const terrain = globalThis.__scene.children[0];
      const hit = (o, d) => new THREE.Raycaster(o, d).intersectObject(terrain).length;
      return [hit(new THREE.Vector3(0, 100, 0), new THREE.Vector3(0, -1, 0)),
              hit(new THREE.Vector3(0, 100, 0), new THREE.Vector3(0, 1, 0)),
              hit(new THREE.Vector3(500, 100, 0), new THREE.Vector3(0, -1, 0))];
    })()"""
    onto, away, beside = pipe.rt.eval(down)
    assert onto > 0
    assert away == 0
    assert beside == 0  # past the edge of the 100-unit plane


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
