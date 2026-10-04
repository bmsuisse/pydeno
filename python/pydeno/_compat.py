"""Small shims so the asyncio code runs on every supported CPython (3.10 and newer)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

if sys.version_info >= (3, 11):
    timeout = asyncio.timeout
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
