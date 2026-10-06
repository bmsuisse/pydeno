# Keep hard output and transport budgets separate from model previews

OpenCode's [process collector](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/process.ts) retains only a configured byte prefix and records truncation. This is a useful design prompt for pydeno: a short human/model preview must never stand in for the resource budget that governs a complete value. This reference inspires coverage, not a claim of a new vulnerability in either project.

## Current boundaries

- `OutputCapture` retains each console stream under its own UTF-8 payload cap; the documented truncation marker is additional. The default front-door printer has a separate shared stdout/stderr budget. An explicit host print callback is intentionally uncapped.
- `bounded_result` measures compact JSON bytes, including string escapes and base64 expansion. A brief console or model summary does not allow a larger final result through this cap.
- Worker transport separately enforces a 16 MiB frame cap, depth/node budgets, and async receive backpressure. A tool's complete reply still crosses this boundary even when its summary or the final result is small. Worker RSS and V8/serialization limits remain separate controls; a preview is not a worker-memory ceiling.
- `HttpFetch._read` retains at most `max_response_bytes`, reads at most one additional body byte to detect truncation, and does not trust a declared `Content-Length`. Decoding follows the bounded read. A cap on display characters is not this byte budget.

## Confirmed gap and focused change

The pydantic-ai model/error preview used `list(value.items())[:5]`. It displayed five entries but traversed and temporarily copied the whole dictionary first. A bounded regression with twenty real entries observed twenty visits. Replacing that eager list with `itertools.islice` preserves the displayed text while visiting only the five shown entries. This is a bounded-work fix; no sandbox escape, new hard-memory guarantee, or measured speedup is claimed.

## Acceptance criteria

- Exact UTF-8, JSON-escape and base64 boundaries pass at the byte cap and fail one byte below it.
- A short console preview cannot turn an oversized final result into success.
- Console calls after truncation do not format or traverse later values; chunk boundaries cannot reset the retained-output budget, and stderr keeps its own budget.
- Unknown-length, falsely declared long, and chunked HTTP responses stop at the body cap plus one-byte lookahead without draining the tail. An exact-cap complete body is not falsely reported as truncated.
- Model dictionary previews traverse only the entries they display and preserve the existing preview text.
- A complete tool reply exceeding the wire budget is rejected even when its console preview and final-result budget would fit. Validate this on a fresh matching native artifact; never enlarge the frame/memory caps to make a preview work.
- Retain existing worker RSS, serialization, and receive-buffer limit checks. Do not add a generic subprocess runner or change public output defaults for this follow-up.

## Evidence and remaining verification

Focused tests live in `tests/test_agent_output_boundaries.py`. The preview regression failed before the two-line fix; result/log/HTTP checks exercise actual Python collectors and the standard HTTP response parser over a bounded in-memory transport. Pure-unit execution uses a temporary import shim that rejects the unavailable native source scanner (including reporting its import-time empty-table probe unsupported). This proves no scanner or OS sandbox behavior.

The local cached macOS extension is not a fresh build of this branch, and lacks `_scan_source`. The native wire regression is therefore left for matching-artifact validation, not reported as passed. Linux Podman was attempted once and denied by the current sandbox's network policy (`operation not permitted` connecting to its loopback control socket). No GitHub issue was filed from this restricted session; this file is the ready-to-file issue body.
