"""Small shims so the asyncio code runs on every supported CPython (3.10 and newer)."""

from __future__ import annotations

# PEP 810 (Python 3.15): these stdlib modules are loaded on first use, not at import. A plain
# list, so it is inert on 3.10-3.14. Never list what the isolation worker imports before it
# applies its sandbox (`_worker`, `_sandbox`, `_wire`, `_wasm`, `_awaitable`): a lazy import
# there would run after the sandbox closed the filesystem.
__lazy_modules__ = ["asyncio"]

import asyncio
import sys
from typing import Any

if sys.version_info >= (3, 11):

    def timeout(delay: float | None) -> Any:
        # A function, not `timeout = asyncio.timeout`: that would load asyncio at import time.
        return asyncio.timeout(delay)

else:

    class _Timeout:
        """`asyncio.timeout` for Python 3.10: cancel the awaiting task at the deadline and raise the
        builtin `TimeoutError`, like the 3.11 version. `None` means no deadline."""

        def __init__(self, delay: float | None) -> None:
            self._delay = delay
            self._task: asyncio.Task[Any] | None = None
            self._handle: asyncio.TimerHandle | None = None
            self._expired = False

        async def __aenter__(self) -> _Timeout:
            if self._delay is not None:
                self._task = asyncio.current_task()
                self._handle = asyncio.get_running_loop().call_later(
                    self._delay, self._expire
                )
            return self

        def _expire(self) -> None:
            self._expired = True
            if self._task is not None:
                self._task.cancel()

        async def __aexit__(
            self, exc_type: type | None, exc: BaseException | None, tb: Any
        ) -> bool:
            if self._handle is not None:
                self._handle.cancel()
            if self._expired and exc_type is asyncio.CancelledError:
                raise TimeoutError from exc
            return False

    def timeout(delay: float | None) -> _Timeout:
        return _Timeout(delay)
