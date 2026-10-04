"""FrameReader / FrameWriter framing: how frames split and coalesce across reads.

The reader takes a frame straight from the chunk when one read returns exactly one whole frame,
and buffers otherwise; both paths must give the same frames, and the size cap must hold on both.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from pydeno import _wire


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "little") + payload


@pytest.fixture
def pipe():  # type: ignore[no-untyped-def]
    r, w = os.pipe()
    yield r, w
    for fd in (r, w):
        try:
            os.close(fd)
        except OSError:
            pass


def test_one_whole_frame_per_read(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    reader = _wire.FrameReader(r)
    for payload in (b"x", b"", b"hello" * 100):
        os.write(w, _frame(payload))
        assert reader.read(time.monotonic() + 1) == payload


def test_two_frames_in_one_read(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    os.write(w, _frame(b"first") + _frame(b"second"))
    reader = _wire.FrameReader(r)
    assert reader.read() == b"first"
    assert reader.read() == b"second"


def test_a_frame_and_part_of_the_next(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    second = _frame(b"second-frame")
    os.write(w, _frame(b"first") + second[:6])
    reader = _wire.FrameReader(r)
    assert reader.read() == b"first"
    os.write(w, second[6:])
    assert reader.read(time.monotonic() + 1) == b"second-frame"


def test_a_frame_split_byte_by_byte(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    data = _frame(b"split across many reads")

    def drip() -> None:
        for i in range(len(data)):
            os.write(w, data[i : i + 1])
            time.sleep(0.001)

    t = threading.Thread(target=drip)
    t.start()
    try:
        assert (
            _wire.FrameReader(r).read(time.monotonic() + 5)
            == b"split across many reads"
        )
    finally:
        t.join()


def test_the_cap_holds_when_the_header_arrives_with_payload(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    os.write(w, _frame(b"y" * 100))
    with pytest.raises(_wire.WireError, match="exceeds"):
        _wire.FrameReader(r, max_frame=99).read()


def test_a_frame_at_the_cap_is_accepted(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    os.write(w, _frame(b"y" * 100))
    assert _wire.FrameReader(r, max_frame=100).read() == b"y" * 100


def test_eof_between_frames_and_mid_header(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    os.write(w, _frame(b"last"))
    os.close(w)
    reader = _wire.FrameReader(r)
    assert reader.read() == b"last"
    assert reader.read() is None

    r2, w2 = os.pipe()
    try:
        os.write(w2, b"\x05\x00")
        os.close(w2)
        with pytest.raises(_wire.WireError, match="mid-header"):
            _wire.FrameReader(r2).read()
    finally:
        os.close(r2)


def test_writer_and_reader_round_trip(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    writer = _wire.FrameWriter(w)
    reader = _wire.FrameReader(r)
    for i in range(50):
        writer.send({"t": "x", "i": i, "v": _wire.Enc(i)})
        assert _wire.loads(reader.read(time.monotonic() + 1)) == {
            "t": "x",
            "i": i,
            "v": i,
        }


def test_a_large_frame_goes_out_in_several_writes(pipe) -> None:  # type: ignore[no-untyped-def]
    r, w = pipe
    os.set_blocking(w, False)
    writer = _wire.FrameWriter(w, stall_timeout=5.0)
    big = "z" * (1 << 20)  # larger than a pipe buffer: the first write is partial
    got: list[bytes | None] = []
    t = threading.Thread(
        target=lambda: got.append(_wire.FrameReader(r).read(time.monotonic() + 5))
    )
    t.start()
    writer.send({"t": "x", "s": big})
    t.join()
    assert got[0] is not None and _wire.loads(got[0])["s"] == big
