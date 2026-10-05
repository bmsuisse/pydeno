"""Reply bursts stay bounded, a reader that keeps up is not killed, and one that stops or only
drips is killed within one stall window."""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from pydeno import AsyncIsolatedRuntime
from pydeno import _aio, _wire


async def _pipe():
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    transport, protocol = await asyncio.get_running_loop().connect_write_pipe(
        _aio._Writer, os.fdopen(write_fd, "wb", buffering=0)
    )
    runtime = SimpleNamespace(
        _wtransport=transport,
        _wproto=protocol,
        _stall=0.2,
        _killed=False,
        _send_lock=asyncio.Lock(),
    )
    runtime._write = lambda frame: AsyncIsolatedRuntime._write(runtime, frame)
    runtime._drain = lambda: AsyncIsolatedRuntime._drain(runtime)
    return runtime, read_fd


async def test_slow_progressing_reader_has_bounded_backlog_and_no_false_stall():
    # The reader takes 8 KiB per tick (no more than the smallest pipe gives), so each frame
    # drains well within one stall window while the whole burst takes longer than one.
    runtime, fd = await _pipe()
    runtime._stall = 0.5
    frame = b"x" * (128 * 1024)
    count = 32
    peak = 0
    read = 0

    async def consume():
        nonlocal read, peak
        while read < count * len(frame):
            peak = max(peak, runtime._wtransport.get_write_buffer_size())
            try:
                read += len(os.read(fd, 8 * 1024))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.002)

    consumer = asyncio.create_task(consume())
    started = time.monotonic()
    try:
        outcomes = await asyncio.gather(
            *(AsyncIsolatedRuntime._send_frame(runtime, frame) for _ in range(count)),
            return_exceptions=True,
        )
        assert time.monotonic() - started > runtime._stall
        await asyncio.wait_for(consumer, 10)
        assert peak <= len(frame) + 64 * 1024
        assert outcomes == [None] * count
        assert read == count * len(frame)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        runtime._wtransport.close()
        os.close(fd)


async def test_reader_without_progress_still_stalls():
    runtime, fd = await _pipe()
    try:
        # 1 MiB: more than the pipe and the 64 KiB high watermark take, on any kernel.
        runtime._write(b"x" * (1024 * 1024))
        assert runtime._wproto.paused
        with pytest.raises(_wire.StalledWrite):
            await asyncio.wait_for(runtime._drain(), 1)
    finally:
        runtime._wtransport.close()
        os.close(fd)


async def test_dripping_reader_is_killed_within_one_stall_window():
    # A reader that takes one page per window, just under the stall limit, must not keep a
    # large reply (and with it the host call) alive past one window.
    runtime, fd = await _pipe()
    stop = False

    async def drip():
        while not stop:
            await asyncio.sleep(runtime._stall * 0.9)
            try:
                os.read(fd, 4 * 1024)
            except BlockingIOError:
                pass

    dripper = asyncio.create_task(drip())
    started = time.monotonic()
    try:
        with pytest.raises(_wire.StalledWrite):
            await asyncio.wait_for(
                AsyncIsolatedRuntime._send_frame(runtime, b"x" * (1024 * 1024)), 2
            )
        # one stall window is 0.2 s; the margin is for a loaded runner's event loop
        assert time.monotonic() - started < runtime._stall * 4
    finally:
        stop = True
        dripper.cancel()
        await asyncio.gather(dripper, return_exceptions=True)
        runtime._wtransport.close()
        os.close(fd)


async def test_host_call_remains_inflight_until_reply_is_sent():
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler():
        return 42

    async def send_reply(*args, **kwargs):
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(_async_inflight=1, _serial=123, _send_reply=send_reply)
    task = asyncio.create_task(
        AsyncIsolatedRuntime._async_call(runtime, handler, [], 1, None)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert runtime._async_inflight == 1
    finally:
        release.set()
        await task
    assert runtime._async_inflight == 0


async def test_waiting_replies_are_not_all_encoded_before_backpressure():
    entered, release = asyncio.Event(), asyncio.Event()
    encoded = 0

    async def encode(reply, big):
        nonlocal encoded
        encoded += 1
        return b"reply"

    async def drain():
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(
        _closed=False,
        _send_lock=asyncio.Lock(),
        _encode=encode,
        _write=lambda frame: None,
        _drain=drain,
    )
    runtime._send_frame = lambda frame: AsyncIsolatedRuntime._send_frame(runtime, frame)
    tasks = [
        asyncio.create_task(AsyncIsolatedRuntime._send_reply(runtime, {"cid": i}, None))
        for i in range(4)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert encoded == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)
