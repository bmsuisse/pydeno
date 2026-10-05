"""Large frame extraction must not allocate a redundant payload-sized slice."""

import struct
import tracemalloc

import pytest

from pydeno import _aio, _wire


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_extracting_large_frame_needs_only_one_payload_copy(kind):
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
