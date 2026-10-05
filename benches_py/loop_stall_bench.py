"""Event-loop responsiveness while isolated workers start, run, close, die and are killed in bursts.

A 1 ms heartbeat task measures how late the loop wakes it. Every beat also records the loop
thread's own CPU time and, on Linux, its run-queue wait (``/proc/self/task/<tid>/schedstat``), so a
late beat can be attributed:

* **busy**: the loop thread was running (Python code on the loop: a slow callback);
* **starved**: it was runnable but had no CPU (the machine or the container quota is saturated);
* **blocked**: neither: waiting for the GIL, or in a blocking call made on the loop.

In a VM whose host is busy, time the hypervisor takes away from a vCPU shows up as "busy" or
"blocked" too (a guest without steal accounting cannot tell), so compare against the ``quiet`` and
``control`` rows of the same run rather than reading the columns in isolation.

A sampler thread records the loop thread's stack whenever the heartbeat is more than
``--stack-ms`` late, and ``--debug`` turns on asyncio debug mode with
``slow_callback_duration=0.02``, which logs every callback that held the loop for 20 ms or more.

    python benches_py/loop_stall_bench.py --sizes 16,32 --scenarios create,kill,pool
    python benches_py/loop_stall_bench.py --json out.json --debug

Scenarios (each runs N runtimes at once, after a quiet control phase):

* ``control``: no pydeno; N plain processes that each burn 0.1 s of CPU (the starvation floor)
* ``create``: create, bind a host function, eval with a host call 10 times, close
* ``kill``: create, run ``while (true) {}`` past a 0.2 s timeout (the worker is killed)
* ``crash``: create, SIGKILL the worker from outside, the next eval sees the crash
* ``pool``: an ``AsyncSandboxPool(size=4)`` checked out N times at once (mostly cold starts)
* ``front``: an ``AsyncPydeno(min_processes=2)``, N sessions at once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from collections import Counter
from typing import Any

import pydeno
from pydeno import AsyncIsolatedRuntime, AsyncPydeno, AsyncSandboxPool, RuntimeConfig

_LINUX = sys.platform.startswith("linux")


def _throttled() -> tuple[int, float]:
    """(periods throttled, seconds throttled) of this container's CPU quota (cgroup v2)."""
    try:
        with open("/sys/fs/cgroup/cpu.stat") as fh:
            stat = dict(line.split() for line in fh)
        return int(stat.get("nr_throttled", 0)), int(
            stat.get("throttled_usec", 0)
        ) / 1e6
    except (OSError, ValueError):
        return 0, 0.0


def _schedstat(tid: int) -> tuple[float, float]:
    """(cpu seconds, run-queue wait seconds) of one thread; zeros where unavailable."""
    if not _LINUX:
        return time.thread_time(), 0.0
    try:
        with open(f"/proc/self/task/{tid}/schedstat", "rb") as fh:
            run, wait, _ = fh.read().split()
        return int(run) / 1e9, int(wait) / 1e9
    except (OSError, ValueError):
        return time.thread_time(), 0.0


class Heartbeat:
    def __init__(self, stack_ms: float) -> None:
        self.beats: list[tuple[float, float, float, float]] = []  # t, late, cpu, wait
        self.stop = False
        self.stack_ms = stack_ms / 1000
        self.stacks: Counter[str] = Counter()
        self.last = time.monotonic()
        self.tid = 0
        self.ident = 0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.tid = threading.get_native_id()
        self.ident = threading.get_ident()
        cpu0, wait0 = _schedstat(self.tid)
        while not self.stop:
            due = loop.time() + 0.001
            await asyncio.sleep(0.001)
            now = loop.time()
            self.last = time.monotonic()
            cpu, wait = _schedstat(self.tid)
            self.beats.append((now, now - due, cpu - cpu0, wait - wait0))
            cpu0, wait0 = cpu, wait

    def sampler(self) -> None:
        """Another thread: snapshot the loop thread's stack while the heartbeat is late."""
        while not self.stop:
            time.sleep(0.005)
            if time.monotonic() - self.last > self.stack_ms and self.ident:
                frame = sys._current_frames().get(self.ident)  # noqa: SLF001
                if frame is not None:
                    tail = traceback.extract_stack(frame)[-6:]
                    key = " <- ".join(
                        f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
                        for f in reversed(tail)
                    )
                    self.stacks[key] += 1

    def window(self, t0: float, t1: float) -> dict[str, Any]:
        rows = [b for b in self.beats if t0 <= b[0] <= t1]
        if not rows:
            return {"beats": 0}
        late = sorted(r[1] for r in rows)

        def pct(q: float) -> float:
            return round(late[min(len(late) - 1, int(len(late) * q))] * 1000, 2)

        worst = max(rows, key=lambda r: r[1])
        busiest = max(rows, key=lambda r: r[2])
        over = [r for r in rows if r[1] > 0.02]
        # Attribution of the time lost in beats later than 20 ms.
        busy = sum(min(r[2], r[1] + 0.001) for r in over)
        starved = sum(min(r[3], r[1] + 0.001) for r in over)
        total = sum(r[1] + 0.001 for r in over)
        return {
            "beats": len(rows),
            "p50_ms": pct(0.5),
            "p99_ms": pct(0.99),
            "p999_ms": pct(0.999),
            "max_ms": round(worst[1] * 1000, 2),
            "max_busy_ms": round(worst[2] * 1000, 2),
            "max_starved_ms": round(worst[3] * 1000, 2),
            # The longest the loop thread itself ran between two beats: work done on the loop.
            "busiest_beat_cpu_ms": round(busiest[2] * 1000, 2),
            "over_20ms": len(over),
            "over_100ms": sum(r[1] > 0.1 for r in rows),
            "lost_busy_pct": round(100 * busy / total, 1) if total else 0.0,
            "lost_starved_pct": round(100 * starved / total, 1) if total else 0.0,
            "seconds": round(t1 - t0, 3),
        }


async def _host_echo_rt(n: int) -> None:
    rts = await asyncio.gather(
        *(AsyncIsolatedRuntime.create(RuntimeConfig(timeout=30.0)) for _ in range(n))
    )
    await asyncio.gather(*(rt.bind_function("echo", lambda v: v) for rt in rts))

    async def drive(rt: AsyncIsolatedRuntime) -> None:
        for i in range(10):
            assert await rt.eval(f"echo({i}) + 1") == i + 1

    await asyncio.gather(*(drive(rt) for rt in rts))
    await asyncio.gather(*(rt.close() for rt in rts))


async def _kill(n: int) -> None:
    async def one() -> None:
        rt = await AsyncIsolatedRuntime.create(RuntimeConfig(timeout=0.2))
        try:
            await rt.eval("while (true) {}")
        except pydeno.RuntimeTimeout:
            pass
        finally:
            await rt.close()

    await asyncio.gather(*(one() for _ in range(n)))


async def _crash(n: int) -> None:
    async def one() -> None:
        rt = await AsyncIsolatedRuntime.create(RuntimeConfig(timeout=5.0))
        try:
            assert await rt.eval("1 + 1") == 2
            os.kill(rt._proc.pid, signal.SIGKILL)  # noqa: SLF001
            try:
                await rt.eval("1")
            except pydeno.WorkerCrashed:
                pass
        finally:
            await rt.close()

    await asyncio.gather(*(one() for _ in range(n)))


async def _pool(n: int) -> None:
    async with AsyncSandboxPool(RuntimeConfig(timeout=5.0), size=4) as pool:

        async def one() -> None:
            async with pool.checkout() as rt:
                assert await rt.eval("2 * 21") == 42

        await asyncio.gather(*(one() for _ in range(n)))


async def _front(n: int) -> None:
    async with AsyncPydeno(min_processes=2, sandbox="auto") as pool:

        async def one() -> None:
            async with pool.checkout() as session:
                await session.feed_run("const x = 2 * 21; x")

        await asyncio.gather(*(one() for _ in range(n)))


async def _control(n: int) -> None:
    """No pydeno: N plain child processes that each burn ~0.1 s of CPU, started at once. The
    lateness this causes is the floor for any burst of N CPU-bound process start-ups."""
    burn = "import time\nt = time.process_time()\nwhile time.process_time() - t < 0.1: pass"
    procs = await asyncio.gather(
        *(
            asyncio.create_subprocess_exec(sys.executable, "-S", "-c", burn)
            for _ in range(n)
        )
    )
    await asyncio.gather(*(p.wait() for p in procs))


SCENARIOS = {
    "control": _control,
    "create": _host_echo_rt,
    "kill": _kill,
    "crash": _crash,
    "pool": _pool,
    "front": _front,
}


class _SlowCallbacks(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.seen: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "took" in msg:
            head, _, took = msg.rpartition(" took ")
            self.seen[head[:160]] += 1


async def main(args: argparse.Namespace) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    slow = _SlowCallbacks()
    if args.debug:
        loop.set_debug(True)
        loop.slow_callback_duration = 0.02
        logging.getLogger("asyncio").addHandler(slow)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
    hb = Heartbeat(args.stack_ms)
    beat = asyncio.ensure_future(hb.run())
    sampler = threading.Thread(target=hb.sampler, daemon=True)
    sampler.start()
    out: dict[str, Any] = {
        "python": sys.version.split()[0],
        "pydeno": getattr(pydeno, "__version__", "?"),
        "pydeno_file": pydeno.__file__,
        "cpus": os.cpu_count(),
        "affinity": len(os.sched_getaffinity(0)) if _LINUX else None,
        "debug": args.debug,
        "runs": [],
    }
    t0 = loop.time()
    await asyncio.sleep(args.quiet)
    out["runs"].append({"scenario": "quiet", "n": 0, **hb.window(t0, loop.time())})
    # One warm-up start so imports and the first spare are not billed to the first scenario.
    rt = await AsyncIsolatedRuntime.create()
    await rt.close()
    for name in args.scenarios.split(","):
        for n in (int(x) for x in args.sizes.split(",")):
            for rep in range(args.repeat):
                await asyncio.sleep(0.3)
                t0 = loop.time()
                th0 = _throttled()
                await SCENARIOS[name](n)
                th1 = _throttled()
                row = {
                    "scenario": name,
                    "n": n,
                    "rep": rep,
                    **hb.window(t0, loop.time()),
                    "quota_throttled": th1[0] - th0[0],
                    "quota_throttled_s": round(th1[1] - th0[1], 3),
                }
                out["runs"].append(row)
                print(json.dumps(row), flush=True)
    hb.stop = True
    await beat
    out["stacks"] = hb.stacks.most_common(15)
    out["slow_callbacks"] = slow.seen.most_common(15)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sizes", default="16,32,64")
    p.add_argument("--scenarios", default="control,create,kill,crash,pool,front")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument(
        "--quiet", type=float, default=2.0, help="seconds of quiet control first"
    )
    p.add_argument(
        "--debug", action="store_true", help="asyncio debug, slow callbacks >= 20 ms"
    )
    p.add_argument("--stack-ms", type=float, default=30.0)
    p.add_argument("--json", help="write the full report here")
    a = p.parse_args()
    report = asyncio.run(main(a))
    print("stacks:", json.dumps(report["stacks"], indent=1))
    print("slow callbacks:", json.dumps(report["slow_callbacks"], indent=1))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(report, fh, indent=1)
