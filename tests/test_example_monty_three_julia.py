"""`examples/monty_three_julia.py`: Monty counts escapes, three.js builds the relief.

The data half is checked against plain CPython running the same program, and against a property
of the mathematics itself (a Julia set is symmetric under z -> -z), so a wrong answer cannot hide
behind "it matches itself".
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct
from collections.abc import Iterator
from typing import Any

import pytest

# `sandbox="require"` is what the pipeline asks for, so it needs a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "monty_three_julia.py"
)
N = 33  # odd, so the grid is symmetric about the origin


@pytest.fixture(scope="module")
def example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_three_julia", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.JuliaPipeline()
    pipeline.load_three()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(pipe: Any) -> tuple[bytes, dict[str, Any], dict[str, float]]:
    return pipe.render(N)


def _python_reference(example: Any, n: int) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "N": n,
        "CR": example.C[0],
        "CI": example.C[1],
        "MAX_ITER": example.MAX_ITER,
    }
    program = example.MODEL_PYTHON.replace('\n{"n": N', '\nresult = {"n": N')
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, as the reference
    return namespace["result"]


def test_monty_counts_exactly_what_cpython_counts(example: Any, pipe: Any) -> None:
    assert pipe.prepare(N) == _python_reference(example, N)


def test_the_counts_have_the_symmetry_of_a_julia_set(pipe: Any) -> None:
    # f(z) = z*z + c is even, so the escape time of -z equals that of z; on a grid that is
    # symmetric about the origin the counts must be point-symmetric, exactly.
    counts = pipe.prepare(N)["counts"]
    for k, value in enumerate(counts):
        assert value == counts[N * N - 1 - k], k


def test_the_set_is_neither_empty_nor_the_whole_plane(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]], example: Any
) -> None:
    _, stats, _ = rendered
    assert 0.02 < stats["insideFraction"] < 0.6
    assert stats["triangles"] == 2 * (N - 1) ** 2
    assert 0 < stats["peak"] <= 18


def test_the_export_is_a_valid_gltf(
    rendered: tuple[bytes, dict[str, Any], dict[str, float]],
) -> None:
    glb, _, _ = rendered
    magic, version, length = struct.unpack("<4sII", glb[:12])
    assert magic == b"glTF" and version == 2 and length == len(glb)
    chunk_len, chunk_type = struct.unpack("<I4s", glb[12:20])
    assert chunk_type == b"JSON"
    doc = json.loads(glb[20 : 20 + chunk_len])
    counts = [a["count"] for a in doc["accessors"]]
    assert N * N in counts  # positions, normals and colours, one per grid point
    assert any(a["type"] == "VEC3" and a["count"] == N * N for a in doc["accessors"])


def test_the_pipeline_is_deterministic(pipe: Any) -> None:
    first, _, _ = pipe.render(N)
    second, _, _ = pipe.render(N)
    assert first == second


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
