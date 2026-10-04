"""One number for the autoresearch speed loops (see docs/contributing/autoresearch.md).

    python scripts/autoresearch/metric_speed.py warm       # microseconds per warm eval
    python scripts/autoresearch/metric_speed.py cold       # milliseconds, new sandbox + first call
    python scripts/autoresearch/metric_speed.py checkout   # milliseconds, pool checkout + first call
    python scripts/autoresearch/metric_speed.py feeds10    # milliseconds, checkout + 10 evals + close

Prints a single line `METRIC: <value>` (lower is better) on stdout; everything else goes to stderr.
Medians over interleaved rounds so a loaded machine moves every variant the same way; the number is
only comparable within one session on one machine, not across machines.
"""

from __future__ import annotations

import statistics
import sys
import time

ROUNDS = {"warm": 3000, "cold": 25, "checkout": 40, "feeds10": 25}


def timed(fn) -> float:  # type: ignore[no-untyped-def]
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def warm() -> float:
    from pydeno import IsolatedRuntime

    with IsolatedRuntime(sandbox="require") as rt:
        for _ in range(300):
            rt.eval("1 + 1")
        samples = [timed(lambda: rt.eval("1 + 1")) for _ in range(ROUNDS["warm"])]
    return statistics.median(samples) * 1e6


def cold() -> float:
    from pydeno import IsolatedRuntime

    def one() -> None:
        rt = IsolatedRuntime(sandbox="require")
        rt.eval("1 + 1")
        rt.close()

    one()  # imports and caches
    return statistics.median(timed(one) for _ in range(ROUNDS["cold"])) * 1e3


def checkout() -> float:
    from pydeno import SandboxPool

    samples = []
    with SandboxPool(size=2, sandbox="require") as pool:
        for _ in range(5):
            with pool.checkout() as rt:
                rt.eval("1 + 1")
        for _ in range(ROUNDS["checkout"]):
            time.sleep(
                0.15
            )  # let the pool refill: this measures a ready worker, not a cold start

            def one() -> None:
                with pool.checkout() as rt:
                    rt.eval("1 + 1")

            samples.append(timed(one))
    return statistics.median(samples) * 1e3


def feeds10() -> float:
    from pydeno import SandboxPool

    samples = []
    with SandboxPool(size=2, sandbox="require") as pool:
        for _ in range(ROUNDS["feeds10"]):
            time.sleep(0.15)

            def one() -> None:
                with pool.checkout() as rt:
                    rt.eval("var x = 0")
                    for _ in range(10):
                        rt.eval("x = x + 1; x")

            samples.append(timed(one))
    return statistics.median(samples) * 1e3


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "warm"
    repeat = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    measure = {"warm": warm, "cold": cold, "checkout": checkout, "feeds10": feeds10}[
        which
    ]
    # Load only ever makes a run slower, never faster, so the minimum of several independent
    # medians is the least noisy estimate of the code's own cost.
    runs = [measure() for _ in range(repeat)]
    print(f"{which}: runs={[round(r, 3) for r in runs]}", file=sys.stderr)
    print(f"METRIC: {min(runs):.3f}")


if __name__ == "__main__":
    main()
