"""Adapter that makes `pydeno`'s async entry points look like coroutines.

The async entry points start work eagerly on the runtime thread and return a
bare `asyncio.Future`, which is awaitable but rejected by `asyncio.create_task`
("a coroutine was expected"). Wrapping it changes only the handle's type, not
when the work starts; cancelling the task still propagates to the future.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

__all__ = ["as_coroutine"]


async def as_coroutine(awaitable: Awaitable[Any]) -> Any:
    """Await `awaitable` from inside a coroutine, so `create_task` accepts it."""
    return await awaitable
