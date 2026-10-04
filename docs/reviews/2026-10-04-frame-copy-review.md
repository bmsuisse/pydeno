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
