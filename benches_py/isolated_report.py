"""Reproducible numbers for `IsolatedRuntime`: start-up, boundary cost, import time, memory.

Not collected by pytest (the name does not start with `test_`) because these are wall-clock
measurements that include process start-up and deliberate idle gaps, which pytest-benchmark's
calibrated loops are not built for. Run it against a release build::

    uv run maturin develop --uv --release
    python benches_py/isolated_report.py

Everything printed is a median of repeated runs on this machine; compare runs on the same machine.
"""

from __future__ import annotations

import statistics as st
import subprocess
import sys
import time

from pydeno import IsolatedRuntime, Runtime, RuntimeConfig, _wire


def median_ms(fn, runs: int = 11, gap: float = 0.0) -> float:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
        if gap:
            time.sleep(gap)
    return st.median(times)


def create_and_eval(**kwargs: object) -> None:
    with IsolatedRuntime(RuntimeConfig(), **kwargs) as rt:  # type: ignore[arg-type]
        rt.eval("1 + 1")


def rss_mb(pid: int) -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True
    )
    return int(out.stdout.strip() or 0) / 1024


def child_ms(code: str) -> float:
    start = time.perf_counter()
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
    return (time.perf_counter() - start) * 1000


def main() -> None:
    # Note: `.eval(...)` below is pydeno's JavaScript eval of fixed benchmark snippets, not
    # Python's builtin `eval`; nothing here evaluates caller-supplied code.
    rows: list[tuple[str, str]] = []

    # --- start-up -------------------------------------------------------------------------
    create_and_eval()  # warm: the first runtime also starts the first spare worker
    time.sleep(0.5)
    rows.append(
        (
            "create + eval, no spare worker (prewarm=False)",
            f"{median_ms(lambda: create_and_eval(prewarm=False), gap=0.2):.1f} ms",
        )
    )
    for gap in (0.1, 0.4):
        rows.append(
            (
                f"create + eval, spare worker, {gap} s idle gap",
                f"{median_ms(create_and_eval, gap=gap):.1f} ms",
            )
        )
    with Runtime(RuntimeConfig()) as plain:
        rows.append(
            (
                "in-process Runtime: eval('1 + 1')",
                f"{median_ms(lambda: plain.eval('1 + 1'), runs=2000) * 1000:.1f} us",
            )
        )
    with IsolatedRuntime(RuntimeConfig()) as warm:
        warm.eval("1")
        rows.append(
            (
                "IsolatedRuntime, warm: eval('1 + 1')",
                f"{median_ms(lambda: warm.eval('1 + 1'), runs=500) * 1000:.0f} us",
            )
        )
        rows.append(
            (
                "IsolatedRuntime worker RSS after one eval",
                f"{rss_mb(warm._proc.pid):.0f} MB",
            )
        )  # noqa: SLF001

    # --- the native wire codec ---------------------------------------------------------------
    objs = [{"i": i, "s": f"v{i}", "f": i / 3} for i in range(50_000)]
    message = {"t": "result", "id": 1, "v": _wire.Enc(objs)}
    frame = _wire.dumps(message)
    rows.append(
        (
            f"encode + write a {len(frame) / 1e6:.1f} MB frame (50k objects)",
            f"{median_ms(lambda: _wire.dumps(message)):.1f} ms",
        )
    )
    rows.append(
        (
            "parse + decode that frame",
            f"{median_ms(lambda: _wire.loads_decoded(frame)):.1f} ms",
        )
    )

    # --- results across the boundary, end to end ------------------------------------------------
    for label, js in (
        (
            "50k small objects",
            "Array.from({length: 50000}, (_, i) => ({i, s: 'v' + i, f: i / 3}))",
        ),
        ("1.7 MB string", "'x'.repeat(1700000)"),
        ("1 MB Uint8Array", "new Uint8Array(1000000).fill(7)"),
    ):
        with Runtime(RuntimeConfig()) as plain:
            base = median_ms(lambda: plain.eval(js), runs=9)
        with IsolatedRuntime(RuntimeConfig()) as iso:
            sandboxed = median_ms(lambda: iso.eval(js), runs=9)
        rows.append(
            (
                f"return {label}: in-process / isolated",
                f"{base:.1f} ms / {sandboxed:.1f} ms",
            )
        )

    # --- import time ------------------------------------------------------------------------
    rows.append(
        (
            "python -I -c pass (baseline)",
            f"{median_ms(lambda: child_ms('pass'), runs=7):.0f} ms",
        )
    )
    rows.append(
        (
            "python -I -c 'import pydeno'",
            f"{median_ms(lambda: child_ms('import pydeno'), runs=7):.0f} ms",
        )
    )

    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        print(f"{name:<{width}}  {value}")


if __name__ == "__main__":
    main()
