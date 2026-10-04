"""Startup and call latency of pydeno, Monty and denobox, measured the same way.

    python benches_py/alternatives_bench.py pydeno       # needs pydeno
    python benches_py/alternatives_bench.py pydeno-pool  # pydeno.SandboxPool checkouts
    python benches_py/alternatives_bench.py pydeno-front # pydeno.Pydeno (the front door)
    python benches_py/alternatives_bench.py monty        # needs pydantic-monty
    python benches_py/alternatives_bench.py denobox      # needs denobox (and `deno` on PATH)

Three numbers per tool, each the median (p95 in brackets) in milliseconds:

* new sandbox: create one and evaluate `1 + 1` once (cold)
* warm call:   one more `1 + 1` on a live sandbox
* 10 commands: create a fresh sandbox and run ten small, state-keeping commands

Every tool runs a tiny expression in its own language; this measures the machinery, not the engine.
"""

from __future__ import annotations

import platform
import statistics
import sys
import time


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarise(name: str, new: list[float], warm: list[float], ten: list[float]) -> None:
    def cell(v: list[float]) -> str:
        return f"{statistics.median(v):.2f} ({pct(v, 0.95):.2f})"

    print(f"| {name} | {cell(new)} | {cell(warm)} | {cell(ten)} |")


def timed(fn) -> float:  # type: ignore[no-untyped-def]
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


def bench_pydeno(n: int, calls: int) -> None:
    from pydeno import IsolatedRuntime

    def make() -> IsolatedRuntime:
        return IsolatedRuntime(sandbox="require")

    new, warm, ten = [], [], []
    for _ in range(n):
        t = time.perf_counter()
        rt = make()
        rt.eval("1 + 1")
        new.append((time.perf_counter() - t) * 1000)
        rt.close()
    rt = make()
    for _ in range(calls):
        warm.append(timed(lambda: rt.eval("1 + 1")))
    rt.close()
    for _ in range(n):
        t = time.perf_counter()
        with make() as rt:
            rt.eval("var x = 0")
            for _ in range(10):
                rt.eval("x = x + 1; x")
        ten.append((time.perf_counter() - t) * 1000)
    summarise("pydeno IsolatedRuntime (OS sandbox required)", new, warm, ten)


def bench_pydeno_pool(n: int, calls: int) -> None:
    """`SandboxPool`: single-use workers started ahead of time. The pool is given time to refill
    between iterations (not timed), so these are checkouts from a warm pool; a back-to-back burst
    that outruns the refill is reported separately, because that is when it degrades to cold
    starts."""
    from pydeno import SandboxPool

    new, checkout, warm, ten = [], [], [], []
    with SandboxPool(sandbox="require", size=4) as pool:
        for _ in range(n):
            pool.wait_ready(30)
            t = time.perf_counter()
            rt = pool.checkout()
            checkout.append((time.perf_counter() - t) * 1000)
            rt.eval("1 + 1")
            new.append((time.perf_counter() - t) * 1000)
            rt.close()
        pool.wait_ready(30)
        with pool.checkout() as rt:
            for _ in range(calls):
                warm.append(timed(lambda: rt.eval("1 + 1")))
        for _ in range(n):
            pool.wait_ready(30)
            t = time.perf_counter()
            with pool.checkout() as rt:
                rt.eval("var x = 0")
                for _ in range(10):
                    rt.eval("x = x + 1; x")
            ten.append((time.perf_counter() - t) * 1000)
        summarise("pydeno SandboxPool (warm checkout + 1 + 1)", new, warm, ten)
        print(
            f"| pydeno SandboxPool checkout alone | {statistics.median(checkout):.3f} "
            f"({pct(checkout, 0.95):.3f}) | | |"
        )
        # A burst of 3 x size sessions, back to back, each running `1 + 1`.
        pool.wait_ready(30)
        burst = []
        before = pool.stats()["cold_starts"]
        for _ in range(12):
            t = time.perf_counter()
            with pool.checkout() as rt:
                rt.eval("1 + 1")
            burst.append((time.perf_counter() - t) * 1000)
        cold = pool.stats()["cold_starts"] - before
        print(
            f"| pydeno SandboxPool burst of 12 (size 4; {cold} cold) | "
            f"{statistics.median(burst):.2f} ({pct(burst, 0.95):.2f}) | | |"
        )


def bench_pydeno_front(n: int, calls: int) -> None:
    """`Pydeno` (the front door) on its defaults: a `SandboxPool` underneath, every session an
    `AgentSandbox` on a checked-out worker. Measured exactly like `pydeno-pool` and Monty, plus the
    session's own costs, so the front door's overhead over a raw pool checkout is visible."""
    from pydeno import Pydeno

    new, enter, warm, ten, leave = [], [], [], [], []
    with Pydeno() as pool:
        for _ in range(n):
            pool._pool.wait_ready(30)  # noqa: SLF001 - a warm pool, as for pydeno-pool
            t = time.perf_counter()
            session = pool.checkout()
            session.__enter__()
            enter.append((time.perf_counter() - t) * 1000)
            session.feed_run("1 + 1")
            new.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            session.__exit__(None, None, None)
            leave.append((time.perf_counter() - t) * 1000)
        pool._pool.wait_ready(30)  # noqa: SLF001
        with pool.checkout() as s:
            for _ in range(calls):
                warm.append(timed(lambda: s.feed_run("1 + 1")))
        for _ in range(n):
            pool._pool.wait_ready(30)  # noqa: SLF001
            t = time.perf_counter()
            with pool.checkout() as s:
                s.feed_run("var x = 0")
                for _ in range(10):
                    s.feed_run("x = x + 1\nx")
            ten.append((time.perf_counter() - t) * 1000)
    summarise("pydeno Pydeno (warm checkout + feed_run('1 + 1'))", new, warm, ten)
    for label, values in (("checkout (enter)", enter), ("session exit", leave)):
        print(
            f"| pydeno Pydeno {label} alone | {statistics.median(values):.3f} "
            f"({pct(values, 0.95):.3f}) | | |"
        )


def bench_monty(n: int, calls: int) -> None:
    from pydantic_monty import Monty

    new, warm, ten = [], [], []
    with Monty() as pool:
        for _ in range(n):
            t = time.perf_counter()
            with pool.checkout() as s:
                s.feed_run("1 + 1")
            new.append((time.perf_counter() - t) * 1000)
        with pool.checkout() as s:
            for _ in range(calls):
                warm.append(timed(lambda: s.feed_run("1 + 1")))
        for _ in range(n):
            t = time.perf_counter()
            with pool.checkout() as s:
                s.feed_run("x = 0")
                for _ in range(10):
                    s.feed_run("x = x + 1\nx")
            ten.append((time.perf_counter() - t) * 1000)
    summarise("Monty (pool checkout)", new, warm, ten)


def bench_denobox(n: int, calls: int) -> None:
    from denobox import Denobox

    new, warm, ten = [], [], []
    for _ in range(n):
        t = time.perf_counter()
        with Denobox() as b:
            b.eval("1 + 1")
        new.append((time.perf_counter() - t) * 1000)
    with Denobox() as b:
        for _ in range(calls):
            warm.append(timed(lambda: b.eval("1 + 1")))
    for _ in range(n):
        t = time.perf_counter()
        with Denobox() as b:
            b.eval("var x = 0")
            for _ in range(10):
                b.eval("x = x + 1; x")
        ten.append((time.perf_counter() - t) * 1000)
    summarise("denobox (Deno subprocess)", new, warm, ten)


def main() -> None:
    which = sys.argv[1]
    n, calls = 15, 300
    print(
        f"{platform.platform()}, Python {platform.python_version()}, {which}",
        file=sys.stderr,
    )
    {
        "pydeno": bench_pydeno,
        "pydeno-pool": bench_pydeno_pool,
        "pydeno-front": bench_pydeno_front,
        "monty": bench_monty,
        "denobox": bench_denobox,
    }[which](n, calls)


if __name__ == "__main__":
    main()
