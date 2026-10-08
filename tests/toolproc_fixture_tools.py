"""Importable tool functions for `tests/test_tool_process.py` (run inside the tool host)."""

from __future__ import annotations

import asyncio
import ctypes
import mmap
import os
import threading
import time
from typing import Any

COUNTER = {"n": 0}


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def aadd(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b


def count() -> int:
    COUNTER["n"] += 1
    return COUNTER["n"]


def pid() -> int:
    return os.getpid()


def env() -> dict[str, str]:
    return dict(os.environ)


def boom(message: str) -> None:
    raise ValueError(message)


def keyerror(key: str) -> None:
    raise KeyError(key)


class CustomFailure(Exception):
    pass


def custom(message: str) -> None:
    raise CustomFailure(message)


def sleep(seconds: float) -> str:
    time.sleep(seconds)
    return "slept"


async def asleep(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "slept"


def spin() -> None:
    while True:
        pass


def big(n: int) -> str:
    return "x" * n


def segfault() -> None:
    ctypes.string_at(0)


def abort() -> None:
    os.abort()


def hard_exit() -> None:
    os._exit(7)


def eat_shared(megabytes: int) -> int:
    """Resident memory the kernel's data limit does not count (shared mapping)."""
    size = megabytes << 20
    block = mmap.mmap(-1, size)
    for offset in range(0, size, 4096):
        block[offset] = 1
    time.sleep(30)
    return size


def eat_private(megabytes: int) -> int:
    chunks = []
    for _ in range(megabytes):
        chunks.append(bytearray(1 << 20))
    return len(chunks)


def unencodable() -> Any:
    return object()


def barrier_pair(tag: str) -> str:
    """Two of these in flight at once finish; one alone waits (proves concurrency)."""
    with _BARRIER_LOCK:
        _WAITING.append(tag)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with _BARRIER_LOCK:
            if len(_WAITING) >= 2:
                return "both"
        time.sleep(0.01)
    return "alone"


_BARRIER_LOCK = threading.Lock()
_WAITING: list[str] = []


async def abarrier(tag: str) -> str:
    _WAITING_A.append(tag)
    for _ in range(500):
        if len(_WAITING_A) >= 2:
            return "both"
        await asyncio.sleep(0.01)
    return "alone"


_WAITING_A: list[str] = []


def fileread(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def threads_alive() -> int:
    return threading.active_count()


def echo(value: Any) -> Any:
    return value


def chatty() -> str:
    print("garbage on stdout that must not reach the wire")
    os.write(1, b"\x00\x00\x00\x05raw bytes straight to fd 1")
    return "done"
