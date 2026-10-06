"""Compare frame extraction with a trusted local git revision or exported source directory.

Run with a release build:
    python benches_py/frame_reader_bench.py --baseline BASE_COMMIT --aa
    python benches_py/frame_reader_bench.py --baseline BASE_COMMIT
    python benches_py/frame_reader_bench.py --baseline BASE_COMMIT --end-to-end --aa
    python benches_py/frame_reader_bench.py --baseline BASE_COMMIT --end-to-end

The default measures prebuffered extraction and buffer disposal. --end-to-end includes IPC,
guest evaluation and decoding. --aa compares the current reader with itself to expose noise.
Baseline sources are executed: only use revisions/directories you trust.
"""

import argparse
import asyncio
import gc
import json
import statistics
import struct
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path

from pydeno import _aio, _wire


def baseline_module(revision, directory, name):
    source = (
        (Path(directory) / f"{name}.py").read_text()
        if directory
        else subprocess.check_output(
            ["git", "show", f"{revision}:python/pydeno/{name}.py"], text=True
        )
    )
    module = types.ModuleType(f"pydeno._benchmark{name}")
    sys.modules[module.__name__] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def extract(reader, kind):
    if kind == "sync":
        return reader.read()
    reader.data_received(b"")
    return reader.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline")
    baseline.add_argument("--baseline-directory")
    parser.add_argument(
        "--aa", action="store_true", help="compare the current reader to itself"
    )
    parser.add_argument("--end-to-end", action="store_true")
    args = parser.parse_args()
    old_wire = baseline_module(args.baseline, args.baseline_directory, "_wire")
    old_aio = baseline_module(args.baseline, args.baseline_directory, "_aio")
    if args.aa:
        old_wire, old_aio = _wire, _aio
    if args.end_to_end:
        asyncio.run(end_to_end(old_wire, old_aio))
        return
    for kind, factories in (
        ("sync", [lambda: old_wire.FrameReader(-1), lambda: _wire.FrameReader(-1)]),
        ("async", [old_aio._FrameReader, _aio._FrameReader]),
    ):
        for size in (64, 65536, 1048576, 8388608):
            payload = b"x" * size
            frame = struct.pack("<I", size) + payload
            samples = [[], []]
            count = min(1000, max(8, (16 * 1024 * 1024) // size))
            for round_id in range(9):
                for index in (0, 1) if round_id % 2 == 0 else (1, 0):
                    readers = [factories[index]() for _ in range(count)]
                    for reader in readers:
                        reader._buf.extend(frame)
                    gc.collect()
                    start = time.perf_counter_ns()
                    for reader in readers:
                        result = extract(reader, kind)
                    samples[index].append(
                        (time.perf_counter_ns() - start) / count / 1000
                    )
                    assert result == payload
            peaks = []
            for factory in factories:
                reader = factory()
                reader._buf.extend(frame)
                tracemalloc.start()
                result = extract(reader, kind)
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
                assert result == payload
            print(
                json.dumps(
                    {
                        "reader": kind,
                        "payload_bytes": size,
                        "baseline_us": round(statistics.median(samples[0]), 2),
                        "updated_us": round(statistics.median(samples[1]), 2),
                        "baseline_peak_bytes": peaks[0],
                        "updated_peak_bytes": peaks[1],
                    }
                )
            )


async def end_to_end(old_wire, old_aio):
    """Paired warm IPC/evaluation/decoding runs, changing only the frame-reader class."""
    from pydeno import AsyncIsolatedRuntime, IsolatedRuntime

    current_sync, current_async = _wire.FrameReader, _aio._FrameReader
    variants = {
        "sync": (old_wire.FrameReader, current_sync),
        "async": (old_aio._FrameReader, current_async),
    }
    try:
        for kind in ("sync", "async"):
            for size in (64, 65536, 1048576, 8388608):
                source = f"'x'.repeat({size})"
                count = 16 if size < 1048576 else 4
                samples = [[], []]
                for round_id in range(9):
                    for index in (0, 1) if round_id % 2 == 0 else (1, 0):
                        if kind == "sync":
                            _wire.FrameReader = variants[kind][index]
                            with IsolatedRuntime(
                                sandbox="require", prewarm=False
                            ) as rt:
                                rt.eval(source)
                                start = time.perf_counter_ns()
                                for _ in range(count):
                                    result = rt.eval(source)
                                elapsed = time.perf_counter_ns() - start
                        else:
                            _aio._FrameReader = variants[kind][index]
                            async with AsyncIsolatedRuntime(
                                sandbox="require", prewarm=False
                            ) as rt:
                                await rt.eval(source)
                                start = time.perf_counter_ns()
                                for _ in range(count):
                                    result = await rt.eval(source)
                                elapsed = time.perf_counter_ns() - start
                        assert len(result) == size and result == "x" * size
                        samples[index].append(elapsed / count / 1000)
                print(
                    json.dumps(
                        {
                            "reader": kind,
                            "payload_bytes": size,
                            "baseline_us": round(statistics.median(samples[0]), 2),
                            "updated_us": round(statistics.median(samples[1]), 2),
                            "baseline_rounds_us": samples[0],
                            "updated_rounds_us": samples[1],
                        }
                    ),
                    flush=True,
                )
    finally:
        _wire.FrameReader, _aio._FrameReader = current_sync, current_async


if __name__ == "__main__":
    main()
