"""`examples/monty_taxi_flows.py`: Monty aggregates real taxi trips, three.js draws a flow map.

An example that is not tested is a screenshot. These tests hold it to its claims: the glTF is a
valid glTF with the right contents, Monty computes exactly what CPython would for the same
program, the pipeline is deterministic, the Monty -> pydeno handoff is genuinely Arrow IPC bytes
(not JSON dressed up), and neither half can reach outside.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

# `sandbox="require"` is what this pipeline asks for, so it needs a complete sandbox.
pytestmark = [pytest.mark.needs_monty, pytest.mark.full_sandbox]

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "monty_taxi_flows.py"
)
TOP_FLOWS = 12  # small, so the test suite stays fast


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_taxi_flows", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> Any:
    return _load_example()


@pytest.fixture(scope="module")
def pipe(example: Any) -> Iterator[Any]:
    pipeline = example.TaxiFlowsPipeline()
    pipeline.load_libraries()
    yield pipeline
    pipeline.close()


@pytest.fixture(scope="module")
def rendered(
    pipe: Any,
) -> tuple[bytes, dict[str, Any], dict[str, Any], dict[str, float]]:
    return pipe.render(TOP_FLOWS)


def _gltf_json(glb: bytes) -> dict[str, Any]:
    magic, version, length = struct.unpack("<4sII", glb[:12])
    assert magic == b"glTF" and version == 2 and length == len(glb)
    chunk_len, chunk_type = struct.unpack("<I4s", glb[12:20])
    assert chunk_type == b"JSON"
    return json.loads(glb[20 : 20 + chunk_len])


def test_the_export_is_a_valid_gltf_with_the_scene_in_it(
    rendered: tuple[bytes, dict[str, Any], dict[str, Any], dict[str, float]],
) -> None:
    glb, stats, meta, timings = rendered
    doc = _gltf_json(glb)
    # ground + instanced columns + 2 tubes (core + glow halo) per drawn flow arc
    assert len(doc["meshes"]) == 2 + 2 * stats["flowsDrawn"]
    assert stats["flowsDrawn"] == TOP_FLOWS
    assert stats["zones"] == meta["zone_count"]
    assert set(timings) == {"monty_prepare", "three_build", "glb_export", "total"}


def test_real_data_was_actually_loaded_and_joined(
    rendered: tuple[bytes, dict[str, Any], dict[str, Any], dict[str, float]],
) -> None:
    _, stats, meta, _ = rendered
    assert meta["trip_count"] == 60_000
    assert meta["zone_count"] > 150  # most of the 263 zones see at least one pickup
    assert meta["flow_count"] > 1000  # thousands of distinct origin-destination pairs
    assert (
        0 <= meta["skipped"] < 500
    )  # a handful of unknown zones / bad durations, not more
    assert stats["totalPickups"] == meta["trip_count"] - meta["skipped"]


def test_flows_are_sorted_by_trip_count_descending(
    pipe: Any,
) -> None:
    analysis = pipe.prepare()
    counts = [row[6] for row in analysis["flows"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] >= counts[-1]


def test_monty_computes_exactly_what_cpython_computes(example: Any, pipe: Any) -> None:
    """The aggregation half is a Python program; the sandbox must not change its answer."""
    namespace: dict[str, Any] = {
        "ZONES": pipe._zones_rows,  # noqa: SLF001 - reaching into the pipeline's own loaded data
        "TRIPS": pipe._trips_rows,  # noqa: SLF001
    }
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "zones"', '\nresult = {\n    "zones"'
    )
    exec(program, namespace)  # noqa: S102 - the example's own fixed program, run as the reference
    assert pipe.prepare() == namespace["result"]


def test_the_whole_pipeline_is_deterministic(pipe: Any) -> None:
    first_glb, first_stats, first_meta, _ = pipe.render(TOP_FLOWS)
    second_glb, second_stats, second_meta, _ = pipe.render(TOP_FLOWS)
    assert first_glb == second_glb
    assert first_stats == second_stats
    assert first_meta == second_meta


def test_the_arrow_ipc_path_is_genuinely_exercised(example: Any, pipe: Any) -> None:
    """The bytes pydeno gets must actually be Arrow IPC, not JSON wearing a trench coat."""
    analysis = pipe.prepare()
    zones_table = example.rows_to_table(analysis["zones"], example.ZONE_COLUMNS)
    flows_table = example.rows_to_table(analysis["flows"], example.FLOW_COLUMNS)
    zones_ipc = example.to_ipc(zones_table)
    flows_ipc = example.to_ipc(flows_table)

    # Arrow's streaming IPC format starts every message with the 0xFFFFFFFF continuation marker;
    # plain JSON never does. A weak but real signal that this is not the JSON fallback path.
    assert zones_ipc[:4] == b"\xff\xff\xff\xff"
    assert flows_ipc[:4] == b"\xff\xff\xff\xff"

    # The strong signal: pyarrow reads them back as real Arrow tables with the right schema.
    back = pa.ipc.open_stream(flows_ipc).read_all()
    assert back.num_rows == len(analysis["flows"]) > 1000
    assert back.column_names == list(example.FLOW_COLUMNS)

    # This dataset's flow table sits right around the crossover row count where
    # examples/arrow_ipc_dataframes.py measured JSON starting to lose to Arrow -- large enough
    # that the choice is a real one, not cosmetic.
    assert len(json.dumps(analysis["flows"]).encode()) > len(flows_ipc) / 2


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
