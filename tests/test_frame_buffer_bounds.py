"""Bound parent allocations even when a compromised worker sends tiny frames."""

import asyncio
import os
import struct
import sys
import time

import pytest

from pydeno import AsyncIsolatedRuntime, IsolatedRuntime, _aio, _sandbox


class Transport:
    paused = False

    def pause_reading(self):
        self.paused = True

    def resume_reading(self):
        self.paused = False


@pytest.mark.parametrize("payload", [b"", b"0", b'{"t":"result","id":1,"v":0}'])
def test_tiny_frame_flood_pauses_and_resumes_without_losing_frames(payload):
    reader = _aio._FrameReader()
    transport = Transport()
    reader.connection_made(transport)
    chunk = (struct.pack("<I", len(payload)) + payload) * 256
    sent = 0
    for _ in range(64):
        reader.data_received(chunk)
        sent += 256
        if transport.paused:
            break
    assert transport.paused, (
        "frame objects must be bounded independently of payload bytes"
    )
    received = []
    while reader.frames:
        received.append(reader.pop())
    assert received == [payload] * sent
    assert not transport.paused
    assert not reader.partial()
    reader.data_received(struct.pack("<I", 2) + b"ok")
    assert reader.pop() == b"ok"


def _reading(transport):
    """`transport.is_reading()`, or None where the transport cannot say (Python 3.10 pipes)."""
    try:
        return transport.is_reading()
    except NotImplementedError:
        return None


async def test_idle_pipe_reader_stops_buffering_an_empty_frame_flood():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    pipe = os.fdopen(read_fd, "rb", buffering=0)
    reader = _aio._FrameReader()
    transport = None
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: reader, pipe
        )
        chunk = struct.pack("<I", 0) * 4096
        stable = 0
        previous = -1
        for _ in range(256):
            try:
                os.write(write_fd, chunk)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.001)
            if _reading(transport) is False:
                break
            # Python 3.10's pipe transport cannot say whether it is reading: no growth is the signal.
            stable = stable + 1 if len(reader.frames) == previous else 0
            previous = len(reader.frames)
            if _reading(transport) is None and stable >= 8:
                break
        assert _reading(transport) in (False, None), (
            "idle worker output must trigger backpressure"
        )
        queued = len(reader.frames)
        assert queued > 0
        for _ in range(10):
            try:
                os.write(write_fd, chunk)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.001)
        assert len(reader.frames) == queued
        while reader.frames:
            assert reader.pop() == b""
        assert _reading(transport) in (True, None)
    finally:
        if transport is not None:
            transport.close()
        pipe.close()
        os.close(write_fd)


def test_resume_requires_both_frame_and_byte_counts_below_the_low_watermarks(
    monkeypatch,
):
    monkeypatch.setattr(_aio, "_READ_HIGH_WATER", 100)
    monkeypatch.setattr(_aio, "_READ_LOW_WATER", 50)
    monkeypatch.setattr(_aio, "_READ_HIGH_FRAMES", 4)
    monkeypatch.setattr(_aio, "_READ_LOW_FRAMES", 2)
    reader = _aio._FrameReader()
    transport = Transport()
    reader.connection_made(transport)
    reader.data_received(struct.pack("<I", 0) * 4 + struct.pack("<I", 60) + b"x" * 60)
    assert transport.paused
    for _ in range(4):
        assert reader.pop() == b""
        assert (
            transport.paused
        )  # below the frame low watermark, still above the byte one
    assert reader.pop() == b"x" * 60
    assert not transport.paused

    reader.data_received(struct.pack("<I", 0) * 4)
    assert transport.paused
    for _ in range(2):
        reader.pop()
        assert transport.paused  # zero bytes, still at or above the frame low watermark
    reader.pop()
    assert not transport.paused


def _flood_worker(tmp_path, allocate):
    script = tmp_path / "flood.py"
    script.write_text(
        "import os, struct, sys\n"
        "size = struct.unpack('<I', sys.stdin.buffer.read(4))[0]\n"
        "sys.stdin.buffer.read(size)\n"
        'ready = b\'{"t":"ready","version":1,"sandbox":"off"}\'\n'
        "os.write(1, struct.pack('<I', len(ready)) + ready)\n"
        "sys.stdin.buffer.read(1)\n"
        f"allocation = bytearray({128 * 1024 * 1024 if allocate else 0})\n"
        "chunk = struct.pack('<I', 0) * 4096\n"
        "while True:\n"
        "    os.write(1, chunk)\n"
    )
    wrapper = tmp_path / "flood-worker"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    wrapper.chmod(0o700)
    return str(wrapper)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("allocate", [False, True])
async def test_idle_runtime_flood_is_bounded_and_supervised(
    tmp_path, asynchronous, allocate
):
    options = dict(
        python=_flood_worker(tmp_path, allocate),
        sandbox="off",
        prewarm=False,
        max_memory=64 * 1024 * 1024,
    )
    runtime = (
        await AsyncIsolatedRuntime.create(**options)
        if asynchronous
        else IsolatedRuntime(**options)
    )
    try:
        before = _sandbox.rss_bytes(os.getpid())
        assert before is not None
        os.write(runtime._proc.stdin.fileno(), b"G")
        if allocate:
            deadline = time.monotonic() + 5
            while runtime._proc.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert runtime._proc.poll() is not None, (
                "idle watchdog did not kill the worker"
            )
            assert runtime.is_closed()
        else:
            await asyncio.sleep(0.2)
            assert runtime._proc.poll() is None
            if asynchronous:
                assert runtime._rproto._paused
                count = len(runtime._rproto.frames)
                await asyncio.sleep(0.1)
                assert len(runtime._rproto.frames) == count
            else:
                # No reader thread drains idle stdout: the kernel pipe bounds the backlog.
                assert len(runtime._reader._buf) <= 65536
            after = _sandbox.rss_bytes(os.getpid())
            assert after is not None and after - before < 8 * 1024 * 1024
    finally:
        if asynchronous:
            await runtime.close()
        else:
            runtime.close()


def test_empty_queue_resumes_to_finish_a_partial_frame(monkeypatch):
    monkeypatch.setattr(_aio, "_READ_HIGH_FRAMES", 4)
    monkeypatch.setattr(_aio, "_READ_LOW_FRAMES", 2)
    monkeypatch.setattr(_aio, "_READ_LOW_WATER", 50)
    reader = _aio._FrameReader()
    transport = Transport()
    reader.connection_made(transport)
    reader.data_received(struct.pack("<I", 0) * 4 + struct.pack("<I", 100) + b"x" * 50)
    assert transport.paused
    for _ in range(4):
        assert reader.pop() == b""
    assert not reader.frames
    assert not transport.paused, (
        "an empty queue needs more input to complete its partial frame"
    )
    reader.data_received(b"x" * 50)
    assert reader.pop() == b"x" * 100
