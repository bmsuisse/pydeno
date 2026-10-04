#!/usr/bin/env python3
"""Run every Monty + pydeno example and keep what each one produces.

    python scripts/run_examples.py --out out/            # all nine
    python scripts/run_examples.py --out out/ --only terrain,julia

Writes the artefacts (.glb, .svg, .html, .pptx) and `results.md` (a timing table) into `--out`, and
checks each artefact is what it claims to be (a real glTF header, well-formed SVG, an HTML page with no
scripts, a valid zip). Exits non-zero if an example fails or its output is not valid, so it is also the
release smoke test: it needs `pip install pydeno pydantic-monty` and the vendored bundles in `vendor/libs`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import platform
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"

# (key, example file, human label, [(output file, render argument)])
CASES: list[tuple[str, str, str, list[tuple[str, object]]]] = [
    ("terrain", "monty_three_terrain.py", "3D terrain", [("terrain.glb", 129)]),
    ("orbits", "monty_three_orbits.py", "Orbits", [("orbits.glb", 3000)]),
    ("julia", "monty_three_julia.py", "Julia relief", [("julia.glb", 129)]),
    ("city", "monty_three_city.py", "City sun analysis", [("city.glb", 6)]),
    ("network", "monty_d3_network.py", "Dependency network", [("network.svg", 200)]),
    ("dashboard", "monty_echarts_dashboard.py", "Dashboard", [("dashboard.html", 365)]),
    ("geo", "monty_turf_geo.py", "Geospatial", [("geo.svg", 8)]),
    (
        "sql",
        "monty_sql_charts.py",
        "SQL to chart",
        [
            ("sql-revenue.svg", "revenue_by_month"),
            ("sql-customers.svg", "top_customers_by_region"),
            ("sql-cohorts.svg", "cohort_retention"),
        ],
    ),
    ("deck", "monty_spreadsheet_deck.py", "Spreadsheet to deck", [("deck.pptx", 1000)]),
]


def load(path: pathlib.Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def validate(name: str, data: bytes | str) -> str:
    """Raise if `data` is not a valid example of its kind; return a short description."""
    raw = data.encode() if isinstance(data, str) else data
    if name.endswith(".glb"):
        magic, version, length = struct.unpack("<4sII", raw[:12])
        assert magic == b"glTF" and version == 2 and length == len(raw), (
            "not a valid glb"
        )
        chunk_len, kind = struct.unpack("<I4s", raw[12:20])
        doc = json.loads(raw[20 : 20 + chunk_len])
        assert kind == b"JSON" and doc["meshes"], "glb without meshes"
        return f"glTF 2, {len(doc['meshes'])} meshes"
    if name.endswith(".svg"):
        ET.fromstring(raw)  # noqa: S314 - our own output
        return "well-formed SVG"
    if name.endswith(".html"):
        text = raw.decode()
        assert "<script" not in text.lower(), "dashboard contains a script"
        assert "<svg" in text, "dashboard has no chart"
        return "HTML with inline SVG, no scripts"
    if name.endswith(".pptx"):
        import io

        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            assert z.testzip() is None, "corrupt zip"
            slides = [n for n in z.namelist() if n.startswith("ppt/slides/slide")]
        return f"valid zip, {len(slides)} slides"
    raise AssertionError(f"unknown artefact type: {name}")


def run_case(
    key: str, filename: str, outputs: list[tuple[str, object]], out: pathlib.Path
):  # type: ignore[no-untyped-def]
    module = load(EXAMPLES / filename)
    pipe = module.PIPELINE(jitless=True)
    rows = []
    try:
        for attr in dir(pipe):
            if attr.startswith("load_") and callable(getattr(pipe, attr)):
                getattr(pipe, attr)()
                break
        for artefact, arg in outputs:
            start = time.perf_counter()
            result = pipe.render(arg)
            elapsed = time.perf_counter() - start
            data = result[0]
            if isinstance(data, (bytearray, memoryview)):
                data = bytes(data)
            note = validate(artefact, data)
            path = out / artefact
            path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data)
            rows.append((artefact, elapsed, path.stat().st_size, note))
        sandbox = getattr(getattr(pipe, "rt", None), "sandbox", "?")
    finally:
        pipe.close()
    return rows, sandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="out", type=pathlib.Path)
    parser.add_argument(
        "--only", default="", help="comma-separated keys, e.g. terrain,julia"
    )
    args = parser.parse_args()
    wanted = {k for k in args.only.split(",") if k}
    args.out.mkdir(parents=True, exist_ok=True)

    table = []
    failures = []
    sandbox = "?"
    for key, filename, label, outputs in CASES:
        if wanted and key not in wanted:
            continue
        try:
            rows, sandbox = run_case(key, filename, outputs, args.out)
        except Exception as exc:  # noqa: BLE001
            failures.append((label, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {label}: {type(exc).__name__}: {exc}", flush=True)
            continue
        for artefact, elapsed, size, note in rows:
            table.append((label, artefact, elapsed, size, note))
            print(
                f"ok    {label:22s} {artefact:20s} {elapsed * 1000:7.0f} ms  {size / 1024:7.0f} KiB  {note}",
                flush=True,
            )

    import pydeno  # noqa: PLC0415

    version = getattr(pydeno, "__version__", None)
    if version is None:
        from importlib.metadata import version as dist_version  # noqa: PLC0415

        version = dist_version("pydeno")
    lines = [
        f"# pydeno {version}: Monty + pydeno examples",
        "",
        f"{platform.platform()}, Python {platform.python_version()}, sandbox `{sandbox}`, V8 jitless (secure default)",
        "",
        "| Example | Artefact | Time | Size | Checked |",
        "|---|---|---:|---:|---|",
    ]
    for label, artefact, elapsed, size, note in table:
        lines.append(
            f"| {label} | `{artefact}` | {elapsed * 1000:.0f} ms | {size / 1024:.0f} KiB | {note} |"
        )
    if failures:
        lines += ["", "## Failures", ""] + [
            f"- **{label}**: {why}" for label, why in failures
        ]
    (args.out / "results.md").write_text("\n".join(lines) + "\n")
    print(f"\nwrote {len(table)} artefacts and results.md to {args.out}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
