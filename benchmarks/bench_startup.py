#!/usr/bin/env python3
"""Startup benchmark: pydeno runtime vs subprocess overhead"""

import subprocess
import time
from pydeno import Runtime


def bench_pydeno(iterations: int = 100) -> float:
    """Measure pydeno runtime creation overhead"""
    start = time.perf_counter()
    for _ in range(iterations):
        with Runtime() as rt:
            rt.eval("2 + 2")
    return time.perf_counter() - start


def bench_subprocess(iterations: int = 100) -> float:
    """Measure subprocess overhead"""
    start = time.perf_counter()
    for _ in range(iterations):
        subprocess.run(
            ["node", "-e", "2 + 2"],
            capture_output=True,
            check=True,
        )
    return time.perf_counter() - start


def main() -> None:
    iterations = 100
    print(f"Comparing {iterations} JavaScript evaluations:")
    print("  • pydeno: create Runtime → eval → destroy")
    print("  • Node.js: spawn process → eval → terminate")
    print("-" * 60)

    # Warmup
    bench_pydeno(5)
    bench_subprocess(5)

    pydeno_time = bench_pydeno(iterations)
    subprocess_time = bench_subprocess(iterations)

    pydeno_per = (pydeno_time / iterations) * 1000
    subprocess_per = (subprocess_time / iterations) * 1000

    print(f"pydeno Runtime:      {pydeno_time:.3f}s total, {pydeno_per:.2f}ms per cycle")
    print(
        f"Node.js subprocess: {subprocess_time:.3f}s total, {subprocess_per:.2f}ms per cycle"
    )
    print("-" * 60)
    print(f"pydeno is {subprocess_time / pydeno_time:.2f}x faster")


if __name__ == "__main__":
    main()
