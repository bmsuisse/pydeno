#!/usr/bin/env python3
"""Threading benchmark that highlights the GIL-free execution model."""

import concurrent.futures
import dataclasses
import time
from typing import Callable

from pydeno import Runtime


def _fibonacci_python(n: int) -> int:
    if n <= 1:
        return n
    return _fibonacci_python(n - 1) + _fibonacci_python(n - 2)


def _fibonacci_pydeno(n: int) -> int:
    with Runtime() as runtime:
        code = f"""
        function fib(n) {{
            if (n <= 1) return n;
            return fib(n - 1) + fib(n - 2);
        }}
        fib({n});
        """
        return runtime.eval(code)


def _run_in_threads(func: Callable[[int], int], n: int, threads: int) -> float:
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(func, [n] * threads))
    return time.perf_counter() - started


@dataclasses.dataclass
class ThreadingResult:
    target: str
    threads: int
    n: int
    elapsed: float


def run_benchmark(n: int = 35, threads: int = 4) -> list[ThreadingResult]:
    python_elapsed = _run_in_threads(_fibonacci_python, n, threads)
    pydeno_elapsed = _run_in_threads(_fibonacci_pydeno, n, threads)

    return [
        ThreadingResult("Python (GIL bound)", threads, n, python_elapsed),
        ThreadingResult("pydeno (GIL released)", threads, n, pydeno_elapsed),
    ]


def main() -> None:
    print("Threading Benchmark • Recursive Fibonacci")
    print("-" * 60)
    results = run_benchmark()
    for result in results:
        print(
            f"{result.target:<24} -> {result.elapsed:6.3f}s "
            f"({result.threads} threads, fib({result.n}))"
        )

    py, js = results
    print("-" * 60)
    print(f"pydeno speedup vs CPython threads: {py.elapsed / js.elapsed:.2f}x")


if __name__ == "__main__":
    main()
