"""Reply bursts stay bounded while a slow reader makes measurable pipe progress."""

import asyncio
import os
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
    runtime, fd = await _pipe()
    frame = b"x" * (128 * 1024)
    count = 4
    peak = 0
    read = 0

    async def consume():
        nonlocal read, peak
        while read < count * len(frame):
            peak = max(peak, runtime._wtransport.get_write_buffer_size())
            try:
                read += len(os.read(fd, 4 * 1024))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.01)

    consumer = asyncio.create_task(consume())
    try:
        outcomes = await asyncio.gather(
            *(AsyncIsolatedRuntime._send_frame(runtime, frame) for _ in range(count)),
            return_exceptions=True,
        )
        await asyncio.wait_for(consumer, 5)
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
        with pytest.raises(_wire.StalledWrite):
            await asyncio.wait_for(
                AsyncIsolatedRuntime._send_frame(runtime, b"x" * (128 * 1024)), 1
            )
    finally:
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
