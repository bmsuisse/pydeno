"""`async_bench.py`'s scenarios, run against `AsyncIsolatedRuntime`.

Same measurements, same table, so the two can be read side by side:

* **loop stall**: the longest time the event loop could not run a 1 ms heartbeat, over creation,
  evaluation and close;
* **threads**: Python threads in the parent per runtime;
* **throughput and latency** of `await rt.eval(...)` across all runtimes at once, with and without a
  host-function round trip in each evaluation;
* **parent memory** at the end.

The scenario rows map onto `async_bench.py`'s:

* "created concurrently" is its "created off-loop" (there: `asyncio.to_thread`; here: `gather` of
  `AsyncIsolatedRuntime.create`, which never blocks the loop);
* "created one by one" is its "created ON the loop" (there: the blocking constructor on the loop
  thread; here: `await create()` in a loop);
* "host call per eval" is the same, with a plain (synchronous) `echo` host function.

    python benches_py/async_core_bench.py                 # 1, 8, 32, 64 runtimes
    python benches_py/async_core_bench.py 1 16 --evals 200
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import threading
import time

from pydeno import RuntimeConfig, _sandbox
from pydeno._aio import AsyncIsolatedRuntime


class Heartbeat:
    """How late the event loop is for a 1 ms timer: the worst lag is the longest stall.
    (The same probe as `async_bench.py`'s, kept here so the two files do not depend on each other.)"""

    def __init__(self) -> None:
        self.lags: list[float] = []
        self.times: list[float] = []
        self._stop = False
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop:
            due = loop.time() + 0.001
            await asyncio.sleep(0.001)
            now = loop.time()
            self.lags.append(max(0.0, now - due))
            self.times.append(now)

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> tuple[float, float]:
        self._stop = True
        assert self._task is not None
        await self._task
        lags = sorted(self.lags) or [0.0]
        return lags[-1] * 1000, lags[int(len(lags) * 0.99)] * 1000

    def worst_between(self, start: float, end: float) -> float:
        """Worst lag of the beats that landed in [start, end) (loop time), in ms."""
        inside = [lag for t, lag in zip(self.times, self.lags) if start <= t < end]
        return max(inside or [0.0]) * 1000


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))] * 1000


async def scenario(
    n: int, evals: int, with_host_call: bool, one_by_one: bool
) -> dict[str, float]:
    heartbeat = Heartbeat()
    heartbeat.start()
    baseline_threads = threading.active_count()

    def make() -> asyncio.Future[AsyncIsolatedRuntime]:
        return AsyncIsolatedRuntime.create(  # type: ignore[return-value]
            RuntimeConfig(timeout=30.0), request_timeout=60
        )

    loop = asyncio.get_running_loop()
    marks = [loop.time()]
    # ---- create n runtimes -------------------------------------------------------------------------
    t0 = time.perf_counter()
    if one_by_one:
        runtimes = [await make() for _ in range(n)]
    else:
        runtimes = list(await asyncio.gather(*[make() for _ in range(n)]))
    create_s = time.perf_counter() - t0
    await asyncio.gather(*[rt.bind_function("echo", lambda v: v) for rt in runtimes])
    marks.append(loop.time())

    # ---- evaluate concurrently ----------------------------------------------------------------------
    code = "echo(1) + 1" if with_host_call else "1 + 1"
    latencies: list[float] = []

    async def drive(rt: AsyncIsolatedRuntime) -> None:
        for _ in range(evals):
            start = time.perf_counter()
            await rt.eval(code)
            latencies.append(time.perf_counter() - start)

    t1 = time.perf_counter()
    await asyncio.gather(*[drive(rt) for rt in runtimes])
    eval_s = time.perf_counter() - t1
    marks.append(loop.time())
    threads_loaded = threading.active_count() - baseline_threads
    rss_mib = (_sandbox.rss_bytes(os.getpid()) or 0) / 2**20

    # ---- close ----------------------------------------------------------------------------------------
    t2 = time.perf_counter()
    await asyncio.gather(*[rt.close() for rt in runtimes])
    close_s = time.perf_counter() - t2
    worst, p99 = await heartbeat.stop()
    marks.append(loop.time() + 1)
    phases = [heartbeat.worst_between(a, b) for a, b in zip(marks, marks[1:])]
    return {
        "create_s": create_s,
        "ops_per_s": n * evals / eval_s,
        "p50": pct(latencies, 0.5),
        "p99": pct(latencies, 0.99),
        "threads_per_rt": threads_loaded / n,
        "rss_mib": rss_mib,
        "close_s": close_s,
        "loop_stall_ms": worst,
        "loop_p99_ms": p99,
        "stall_create": phases[0],
        "stall_eval": phases[1],
        "stall_close": phases[2],
    }


def row(label: str, n: int, r: dict[str, float]) -> str:
    return (
        f"| {label} | {n} | {r['create_s'] * 1000 / n:.0f} ms | {r['ops_per_s']:,.0f} "
        f"| {r['p50']:.2f} / {r['p99']:.2f} ms | {r['threads_per_rt']:.2f} "
        f"| {r['loop_stall_ms']:.1f} ms | {r['stall_create']:.0f} / {r['stall_eval']:.0f} / {r['stall_close']:.0f} ms "
        f"| {r['loop_p99_ms']:.1f} ms | {r['close_s'] * 1000:.0f} ms "
        f"| {r['rss_mib']:.0f} MiB |"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sizes", nargs="*", type=int, default=[1, 8, 32, 64])
    parser.add_argument(
        "--evals", type=int, default=100, help="evaluations per runtime"
    )
    args = parser.parse_args()
    print(
        f"{platform.platform()}, Python {platform.python_version()}, {os.cpu_count()} CPUs\n"
    )
    print(
        "| scenario | runtimes | create / runtime | evals/s (all) | latency p50 / p99 "
        "| threads / runtime | worst loop stall | stall create / eval / close | loop lag p99 "
        "| close (all) | parent RSS |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for n in args.sizes:
        for label, host_call, one_by_one in (
            ("1 + 1, created concurrently", False, False),
            ("1 + 1, created one by one", False, True),
            ("host call per eval", True, False),
        ):
            result = await scenario(n, args.evals, host_call, one_by_one)
            print(row(label, n, result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
