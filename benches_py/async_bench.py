"""How well does IsolatedRuntime behave inside an asyncio service?

Measures what an event-loop-driven server cares about, as the number of concurrent runtimes grows:

* **loop stall**: the longest time the event loop was unable to run (a 1 ms heartbeat) while runtimes were
  created, driven and closed. Anything that blocks the loop shows up here.
* **threads**: Python threads the parent *adds* per runtime (a thread-per-runtime design does not scale to
  thousands of sessions). The benchmark's own helper pool is created and warmed before the baseline, so it
  is not counted.
* **throughput and latency** of `await rt.eval_async(...)` across all runtimes at once, with and without a
  host-function round trip in each evaluation.
* **parent memory** added by the scenario, and the cost of closing the runtimes.

    python benches_py/async_bench.py                 # 1, 8, 32 runtimes
    python benches_py/async_bench.py 1 16 64 --evals 100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pydeno import IsolatedRuntime, RuntimeConfig, _sandbox


class Heartbeat:
    """Records how late the event loop is for a 1 ms timer: the worst lag is the longest stall."""

    def __init__(self) -> None:
        self.lags: list[float] = []
        self._stop = False
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop:
            due = loop.time() + 0.001
            await asyncio.sleep(0.001)
            self.lags.append(max(0.0, loop.time() - due))

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> float:
        self._stop = True
        assert self._task is not None
        await self._task
        return max(self.lags or [0.0]) * 1000


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))] * 1000


def rss_mib() -> float:
    return (_sandbox.rss_bytes(os.getpid()) or 0) / 2**20


async def scenario(
    n: int,
    evals: int,
    with_host_call: bool,
    create_blocking: bool,
    helper: ThreadPoolExecutor,
) -> dict[str, float]:
    heartbeat = Heartbeat()
    baseline_threads = threading.active_count()
    baseline_rss = rss_mib()
    heartbeat.start()
    runtimes: list[IsolatedRuntime] = []
    loop = asyncio.get_running_loop()
    latencies: list[float] = []
    try:
        # ---- create n runtimes -------------------------------------------------------------------
        t0 = time.perf_counter()
        if create_blocking:  # what a naive service does: construct on the loop thread
            for _ in range(n):
                runtimes.append(
                    IsolatedRuntime(RuntimeConfig(timeout=30.0), request_timeout=60)
                )
        else:  # what it has to do today to keep the loop free
            made = await asyncio.gather(
                *[
                    loop.run_in_executor(
                        helper,
                        lambda: IsolatedRuntime(
                            RuntimeConfig(timeout=30.0), request_timeout=60
                        ),
                    )
                    for _ in range(n)
                ]
            )
            runtimes.extend(made)
        create_s = time.perf_counter() - t0
        for rt in runtimes:
            rt.bind_function("echo", lambda v: v)

        # ---- evaluate concurrently ------------------------------------------------------------------
        code = "echo(1) + 1" if with_host_call else "1 + 1"

        async def drive(rt: IsolatedRuntime) -> None:
            for _ in range(evals):
                start = time.perf_counter()
                await rt.eval_async(code)
                latencies.append(time.perf_counter() - start)

        t1 = time.perf_counter()
        await asyncio.gather(*[drive(rt) for rt in runtimes])
        eval_s = max(time.perf_counter() - t1, 1e-9)
        threads_added = threading.active_count() - baseline_threads
        rss_added = rss_mib() - baseline_rss
    finally:
        # ---- close (blocks the loop today: the stall is part of what is measured) ------------------
        t2 = time.perf_counter()
        for rt in runtimes:
            rt.close()
        close_s = time.perf_counter() - t2
    stall = await heartbeat.stop()
    return {
        "create_ms": create_s * 1000 / n,
        "ops_per_s": n * evals / eval_s,
        "p50": pct(latencies, 0.5),
        "p99": pct(latencies, 0.99),
        "threads_per_rt": threads_added / n,
        "rss_added_mib": rss_added,
        "close_ms": close_s * 1000 / n,
        "loop_stall_ms": stall,
    }


def row(label: str, n: int, r: dict[str, float]) -> str:
    return (
        f"| {label} | {n} | {r['create_ms']:.0f} ms | {r['close_ms']:.0f} ms | {r['ops_per_s']:,.0f} "
        f"| {r['p50']:.2f} / {r['p99']:.2f} ms | {r['threads_per_rt']:.1f} | {r['loop_stall_ms']:.0f} ms "
        f"| {r['rss_added_mib']:+.0f} MiB |"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sizes", nargs="*", type=int, default=[1, 8, 32])
    parser.add_argument(
        "--evals", type=int, default=100, help="evaluations per runtime"
    )
    args = parser.parse_args()
    if args.evals < 1 or any(n < 1 for n in args.sizes):
        parser.error("sizes and --evals must be at least 1")

    # The helper pool exists, fully started, before any baseline is taken, so its threads are not
    # counted against the runtimes.
    helper = ThreadPoolExecutor(max_workers=8)
    await asyncio.gather(
        *[
            asyncio.get_running_loop().run_in_executor(helper, time.sleep, 0.05)
            for _ in range(8)
        ]
    )

    print(
        f"{platform.platform()}, Python {platform.python_version()}, {os.cpu_count()} CPUs\n"
    )
    print(
        "| scenario | runtimes | create each | close each | evals/s (all) | latency p50 / p99 "
        "| threads added / runtime | worst loop stall | parent RSS added |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for n in args.sizes:
        for label, host_call, blocking in (
            ("1 + 1, created off-loop", False, False),
            ("1 + 1, created ON the loop", False, True),
            ("host call per eval", True, False),
        ):
            result = await scenario(n, args.evals, host_call, blocking, helper)
            print(row(label, n, result), flush=True)
    helper.shutdown(wait=True)


if __name__ == "__main__":
    asyncio.run(main())
