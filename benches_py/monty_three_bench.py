"""How fast are Monty and pydeno together? Benchmarks `examples/monty_three_terrain.py`.

Prints a markdown table: for each grid size, the median time of each stage of one agent turn
(Monty prepares the heightmap, three.js builds the scene, the scene is exported to glTF), plus
how long the same data program takes in plain, unsandboxed CPython as a reference.

    python benches_py/monty_three_bench.py            # 33, 65, 129, 193
    python benches_py/monty_three_bench.py 257 --reps 3

Numbers depend on the machine; the shape does not. Run it where you care about the answer.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import platform
import statistics
import sys
import time
from typing import Any

EXAMPLE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "examples"
    / "monty_three_terrain.py"
)


def load_example() -> Any:
    spec = importlib.util.spec_from_file_location("monty_three_terrain", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cpython_reference(example: Any, n: int) -> float:
    program = example.MODEL_PYTHON.replace(
        '\n{\n    "n": N', '\nresult = {\n    "n": N'
    )
    start = time.perf_counter()
    exec(program, {"N": n})  # noqa: S102 - the example's own fixed program
    return time.perf_counter() - start


def median_ms(values: list[float]) -> float:
    return statistics.median(values) * 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sizes", nargs="*", type=int, default=[33, 65, 129, 193])
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument(
        "--jit",
        action="store_true",
        help="run V8 with its JIT on (faster, bigger attack surface); the default is jitless",
    )
    args = parser.parse_args()

    example = load_example()

    cold_start = time.perf_counter()
    pipe = example.TerrainPipeline(jitless=not args.jit)
    pipe.load_three()
    cold = time.perf_counter() - cold_start

    print(
        f"{platform.platform()}, Python {platform.python_version()}, sandbox: {pipe.rt.sandbox}, "
        f"V8 {'JIT on' if args.jit else 'jitless (default)'}"
    )
    print(
        f"cold start (worker + OS sandbox + load three.js): {cold * 1000:.0f} ms "
        f"(three.js load alone {pipe.load_seconds * 1000:.0f} ms)\n"
    )
    print(
        "| grid | triangles | trees | Monty prepare | three.js build | glTF export "
        "| **total** | CPython, unsandboxed | GLB |"
    )
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    try:
        pipe.render(args.sizes[0])  # warm-up, discarded
        for n in args.sizes:
            runs = [pipe.render(n) for _ in range(args.reps)]
            stats = runs[0][1]
            parts = {k: [r[2][k] for r in runs] for k in runs[0][2]}
            reference = [cpython_reference(example, n) for _ in range(args.reps)]
            print(
                f"| {n}x{n} | {stats['triangles']:,} | {stats['trees']} "
                f"| {median_ms(parts['monty_prepare']):.0f} ms "
                f"| {median_ms(parts['three_build']):.0f} ms "
                f"| {median_ms(parts['glb_export']):.0f} ms "
                f"| **{median_ms(parts['total']):.0f} ms** "
                f"| {median_ms(reference):.0f} ms "
                f"| {len(runs[0][0]) / 1024:.0f} KiB |"
            )
    finally:
        pipe.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit("pydeno's IsolatedRuntime is POSIX only")
    main()
