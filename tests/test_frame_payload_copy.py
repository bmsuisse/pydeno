"""Frame extraction in the sync (`_wire.FrameReader`) and async (`_aio._FrameReader`) readers.

Large payloads are copied out of the reassembly buffer through a memoryview, not through a
bytearray slice, so extraction holds one payload-sized copy instead of two. These tests pin that,
and pin that the reassembly itself is unchanged: frames split at every byte offset, frames larger
than one read, back-to-back frames, partial headers, empty frames, the 16 MiB cap, hostile length
headers, and that a returned payload is owned `bytes` that later buffer reuse cannot change.

No pipes: the sync reader is fed through its `_chunk` hook, so this runs on every platform.
"""

from __future__ import annotations

import struct
import tracemalloc

import pytest

from pydeno import _aio, _wire

CAP = _wire.MAX_FRAME_BYTES
THRESHOLD = 64 * 1024  # `_wire._COPY_VIEW_THRESHOLD`


def _frame(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


class _SyncFeed:
    """A sync reader whose reads return the given chunks, then EOF."""

    def __init__(self, chunks: list[bytes], max_frame: int = CAP) -> None:
        self.reader = _wire.FrameReader(-1, max_frame=max_frame)
        self._chunks = [c for c in chunks if c]  # an empty read is EOF
        self.reader._chunk = lambda deadline: (  # type: ignore[method-assign]
            self._chunks.pop(0) if self._chunks else b""
        )

    def all(self) -> list[bytes]:
        frames = []
        while (payload := self.reader.read()) is not None:
            frames.append(payload)
        return frames


class _Transport:
    paused = False

    def pause_reading(self) -> None:
        self.paused = True

    def resume_reading(self) -> None:
        self.paused = False


def _async_reader(max_frame: int = CAP) -> _aio._FrameReader:
    reader = _aio._FrameReader(max_frame)
    reader.connection_made(_Transport())  # type: ignore[arg-type]
    return reader


def _async_all(chunks: list[bytes], max_frame: int = CAP) -> list[bytes]:
    reader = _async_reader(max_frame)
    frames = []
    for chunk in chunks:
        reader.data_received(chunk)
        while reader.frames:
            frames.append(reader.pop())
    assert reader.error is None
    assert not reader.partial()
    return frames


def _read_all(kind: str, chunks: list[bytes]) -> list[bytes]:
    return _SyncFeed(chunks).all() if kind == "sync" else _async_all(chunks)


KINDS = ["sync", "async"]


@pytest.fixture
def small_threshold(monkeypatch: pytest.MonkeyPatch) -> int:
    """Take the memoryview path from 8 bytes up, so byte-offset sweeps stay cheap."""
    monkeypatch.setattr(_wire, "_COPY_VIEW_THRESHOLD", 8, raising=False)
    return 8


@pytest.mark.parametrize("kind", KINDS)
def test_extracting_large_frame_needs_only_one_payload_copy(kind: str) -> None:
    size = 2 * 1024 * 1024
    payload = b"x" * size
    reader = _wire.FrameReader(-1) if kind == "sync" else _aio._FrameReader()
    # Prebuffer the frame, excluding transport buffering from the measurement.
    reader._buf.extend(struct.pack("<I", size) + payload)
    tracemalloc.start()
    try:
        if kind == "sync":
            result = reader.read()
        else:
            reader.data_received(b"")
            result = reader.pop()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert isinstance(result, bytes)
    assert result == payload
    assert peak < size * 1.5, f"temporary payload slice: peak={peak}, size={size}"
    # The returned bytes must remain owned after the input buffer is reused.
    reader._buf.extend(struct.pack("<I", 3) + b"new")
    assert result == payload


def test_the_threshold_is_the_one_tested_here() -> None:
    assert _wire._COPY_VIEW_THRESHOLD == THRESHOLD


@pytest.mark.parametrize("kind", KINDS)
def test_every_split_offset_gives_the_same_frames(
    kind: str, small_threshold: int
) -> None:
    # Sizes straddle the (lowered) threshold, so both extraction paths see every split.
    payloads = [b"", b"a", b"b" * 7, b"c" * 8, b"d" * 9, b"e" * 40, b""]
    stream = b"".join(_frame(p) for p in payloads)
    for cut in range(len(stream) + 1):
        assert _read_all(kind, [stream[:cut], stream[cut:]]) == payloads, cut
    for a in range(len(stream) + 1):
        for b in range(a, len(stream) + 1, 5):
            assert _read_all(kind, [stream[:a], stream[a:b], stream[b:]]) == payloads


@pytest.mark.parametrize("kind", KINDS)
def test_byte_by_byte(kind: str, small_threshold: int) -> None:
    payloads = [b"x" * 20, b"", b"yz", b"w" * 100]
    stream = b"".join(_frame(p) for p in payloads)
    assert _read_all(kind, [stream[i : i + 1] for i in range(len(stream))]) == payloads


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "size",
    [
        THRESHOLD - 1,
        THRESHOLD,
        THRESHOLD + 1,
        1 << 20,
    ],
)
def test_frames_larger_than_one_read_at_the_real_threshold(
    kind: str, size: int
) -> None:
    payloads = [bytes([i % 251]) * size for i in range(3)]
    stream = b"".join(_frame(p) for p in payloads)
    for step in (1 << 16, 4093, (1 << 18) + 7):
        got = _read_all(
            kind, [stream[i : i + step] for i in range(0, len(stream), step)]
        )
        assert got == payloads
    # All three back to back in a single read.
    assert _read_all(kind, [stream]) == payloads


@pytest.mark.parametrize("kind", KINDS)
def test_payloads_are_owned_bytes_unaffected_by_buffer_reuse(
    kind: str, small_threshold: int
) -> None:
    first, second = b"A" * 64, b"B" * 64
    tail = _frame(b"C" * 64)
    stream = _frame(first) + _frame(second) + tail[:10]
    if kind == "sync":
        feed = _SyncFeed([stream, tail[10:]])
        got = [feed.reader.read(), feed.reader.read()]
        # The buffer still holds a partial frame; growing and overwriting it must neither raise
        # (no view of it outlives extraction) nor change what was handed out.
        buf = feed.reader._buf
        buf += b"\xff" * 4096
        buf[:] = b"\x00" * len(buf)
        del buf[:]
        buf += tail[:10]
        assert feed.reader.read() == b"C" * 64
    else:
        reader = _async_reader()
        reader.data_received(stream)
        got = [reader.pop(), reader.pop()]
        reader._buf += b"\xff" * 4096
        reader._buf[:] = b"\x00" * len(reader._buf)
        reader._buf.clear()
        reader.data_received(tail)
        assert reader.pop() == b"C" * 64
    assert got == [first, second]
    assert all(type(p) is bytes for p in got)


@pytest.mark.parametrize("kind", KINDS)
def test_zero_length_frames(kind: str) -> None:
    stream = _frame(b"") * 3 + _frame(b"x") + _frame(b"")
    assert _read_all(kind, [stream]) == [b"", b"", b"", b"x", b""]


def test_partial_header_then_eof() -> None:
    for n in (1, 2, 3):
        with pytest.raises(_wire.WireError, match="mid-header"):
            _SyncFeed([b"\x05\x00\x00\x00"[:n]]).all()
    with pytest.raises(_wire.WireError, match="mid-frame"):
        _SyncFeed([_frame(b"abcdef")[:7]]).all()
    reader = _async_reader()
    reader.data_received(b"\x05\x00")
    assert not reader.frames and reader.partial()
    reader.data_received(b"\x00\x00hel")
    assert not reader.frames and reader.partial()
    reader.data_received(b"lo")
    assert reader.pop() == b"hello" and not reader.partial()


@pytest.mark.parametrize("kind", KINDS)
def test_a_frame_of_exactly_the_cap_is_accepted(kind: str) -> None:
    payload = b"m" * CAP
    stream = _frame(payload)
    chunks = [stream[i : i + (1 << 18)] for i in range(0, len(stream), 1 << 18)]
    got = _read_all(kind, chunks)
    assert len(got) == 1 and got[0] == payload


@pytest.mark.parametrize("length", [CAP + 1, 0x7FFFFFFF, 0xFFFFFFFF])
def test_oversized_and_hostile_length_headers_are_refused_before_buffering(
    length: int,
) -> None:
    header = struct.pack("<I", length)
    with pytest.raises(_wire.WireError, match="exceeds"):
        _SyncFeed([header + b"x" * 16]).all()
    # Split header: refused as soon as its fourth byte is in.
    feed = _SyncFeed([header[:3], header[3:]])
    with pytest.raises(_wire.WireError, match="exceeds"):
        feed.all()
    reader = _async_reader()
    reader.data_received(header[:3])
    assert reader.error is None
    reader.data_received(header[3:] + b"x" * 16)
    assert reader.error is not None and "exceeds" in reader.error
    assert not reader._buf and reader._transport.paused  # type: ignore[union-attr]
    reader.data_received(_frame(b"later"))
    assert not reader.frames  # nothing after a refusal is believed
