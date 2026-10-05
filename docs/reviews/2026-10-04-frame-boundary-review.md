# Frame queue security review (#65)

A compromised worker can send tiny frames while the async parent is idle. The old byte-only
watermark did not account for deque entries or Python bytes objects; empty frames accounted
for zero bytes, allowing unbounded parent allocation. This is a defense-in-depth host DoS,
not a demonstrated JavaScript escape.

The async reader now pauses at 1,024 frames, or the existing byte high watermark. It resumes
only below 512 frames AND the byte low watermark, or when the queue is empty and the consumer
needs input to finish a partial frame. Either threshold can overshoot by one bounded transport
delivery: CPython's pipe transport reads at most 256 KiB, or 65,536 empty frames (roughly 0.5 MiB
of deque entries). Other loop implementations may have different delivery bounds.
At 1,024 empty frames the deque overhead is small relative to the 32 MiB
payload watermark. Oversized-frame validation remains unchanged.

Touched functions for independent security review: `_aio._FrameReader.data_received` and
`_aio._FrameReader.pop`. The synchronous reader is unchanged from #62: its caller pulls one
frame at a time, and an idle caller leaves flood data in the bounded kernel pipe.

This PR is stacked on #62 to avoid overlapping sync-reader changes. Payload-copy optimization
is tracked separately in #66; its speed claims must be measured against #62 and are not part
of this security fix.

Validation before rebasing: 992 Linux ARM64 tests passed on a fresh release CPython 3.12 wheel,
Debian trixie, Linux 6.15.10, with Landlock+seccomp+emptyroot, including the shared CI test fixes
from #59. The frame/status/attestation selection passed 24 tests in each degraded profile
(no Landlock, no seccomp, neither). These are not claims that degraded profiles are secure.
Native x86-64 validation and independent security review remain required before merging.

After rebasing on #62: all 18 frame-boundary and wire-frame tests passed on macOS and Linux
ARM64 (release wheel with the current Python overlay). Four real fake-worker cases cover sync
and async idle floods: parent RSS remains bounded, async transport pauses, and a worker that
exceeds its memory budget while flooding is terminated by idle supervision. The Linux wheel's
native extension predates #62, so this does not replace a complete integration build.
