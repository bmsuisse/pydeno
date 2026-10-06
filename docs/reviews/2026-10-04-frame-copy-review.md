# Buffered frame allocation (#66)

The sync and async readers previously converted a bytearray slice to bytes, creating a
payload-sized temporary slice before the owned result. For buffered payloads at least 64 KiB,
copy directly from a scoped memoryview. Both views are released before resizing the buffer.
Smaller frames keep the slice path; the synchronous single-read fast path from #62 is unchanged.

The optimization is separate from the frame-count security bound in #67. Independent review
should cover `_wire.FrameReader.read` and `_aio._FrameReader.data_received`.

## Validation

The two 2-MiB allocation regressions fail before the change and pass afterward. They also verify
that the result is owned bytes and remains valid after the buffer is reused. The allocation,
wire-frame and native-wire selection passed 320 tests on macOS; after merging integration
updates through 45d982e, the focused allocation/wire-frame selection passed 11 tests. Ruff and
diff checks pass.

Linux ARM64 measurements used a release wheel built from integration 3ea2e0a, CPython 3.12.15,
Debian trixie, Linux 6.15.10, with only the updated Python reader modules overlaid. The native
extension was identical in both variants. Prebuffered extraction excludes the initial input
buffer from traced allocation measurements:

| Payload | Reader | Baseline traced peak | Updated traced peak |
|---|---|---:|---:|
| 1 MiB | sync / async | 2,097,302 bytes | 1,049,309 bytes |
| 8 MiB | sync / async | 16,777,366 bytes | 8,389,341 bytes |

The benchmark supports paired extraction and end-to-end IPC/evaluation/decoding runs, and A/A
controls. The Linux A/A end-to-end ratios ranged from 0.659 to 1.284 despite using the same
reader on both sides. The environment was too noisy to establish a speed change, so the A/B
timings are not evidence of a speedup or of unchanged end-to-end latency. No integrated 0.8
speed claim is made. Repeat these controls on a quiet native runner before making one.

Run with a release installation and a trusted baseline revision:

```sh
python benches_py/frame_reader_bench.py --baseline BASE_COMMIT --aa
python benches_py/frame_reader_bench.py --baseline BASE_COMMIT
python benches_py/frame_reader_bench.py --baseline BASE_COMMIT --end-to-end --aa
python benches_py/frame_reader_bench.py --baseline BASE_COMMIT --end-to-end
```

Native x86-64 measurements and independent review remain pending. This optimization need not
block the 0.8 release if those gates cannot be completed in time.

## 0.9 measurements (2026-10-05)

Merged onto the 0.9 line (persistent worker loop, reply backpressure, send lock); the readers
merged without conflict and only the extraction lines differ. One release wheel (macOS arm64,
CPython 3.12) installed twice, the baseline with the 0.9 `_wire.py` and `_aio.py` restored, so
the native extension is identical. Each number comes from a child process (fresh interpreter,
fresh worker), the two variants alternating order each round, 9 rounds; medians of the
per-process values. The machine was shared and loaded, so only differences that hold for both
the minimum and the median are read as real.

Reader only (a pipe for the sync reader, 256 KiB `data_received` chunks for the async one; peak
RSS growth is the parent's high-water mark above its level before reading):

| Stream | Reader | Python-heap peak | Peak RSS growth | Reader CPU |
|---|---|---:|---:|---:|
| 3 x 16 MiB | sync | 52.1 -> 35.3 MB | 71.2 -> 54.4 MB | 27.8 -> 22.8 ms |
| 3 x 16 MiB | async | 52.7 -> 36.2 MB | 69.8 -> 53.0 MB | 17.6 -> 12.3 ms |
| 4 x 8 MiB | sync | 25.8 -> 17.4 MB | 46.0 -> 37.6 MB | 18.0 -> 14.8 ms |
| 4 x 8 MiB | async | 25.6 -> 17.5 MB | 46.6 -> 37.9 MB | 13.5 -> 10.1 ms |
| 8 x 1 MiB | sync | 3.4 -> 2.4 MB | 5.5 -> 2.6 MB | 3.6 -> 3.2 ms |
| 8 x 1 MiB | async | 3.4 -> 2.6 MB | within noise | 2.2 -> 1.4 ms |
| 10,000 x 100 B | both | unchanged | unchanged | within noise |
| mixed (small, threshold edges, 1 and 8 MiB) | both | 25.7 -> 17.3 MB | 29 -> 20 MB | 12 to 29 percent less |

End to end (`eval("'x'.repeat(N)")` on a warm `IsolatedRuntime` / `AsyncIsolatedRuntime`):
the Python-heap peak of one call falls by one payload (16 MiB: 52.1 -> 35.3 MB; 8 MiB:
25.8 -> 17.4 MB; 1 MiB: 3.3 -> 2.2 MB), host CPU per call falls 2 to 4 percent (16 MiB sync
28.1 -> 27.0 ms), wall time is within noise, and the process's peak RSS is unchanged (about
121 MB for a 16 MiB reply in both): a later phase of the call, not frame extraction, sets the
high-water mark.

Latency, unchanged within noise (median of per-process medians, base -> branch): warm `eval`
64 -> 64 us, one host call 161 -> 164 us, 50 concurrent async host calls 6.55 -> 6.49 ms,
`metric_speed.py` warm 67.0 -> 65.8 us, checkout 7.43 -> 7.06 ms, feeds10 14.9 -> 14.0 ms.
`feed_run` was within noise on a loaded machine (two runs: 390 -> 412 us and 894 -> 839 us).
