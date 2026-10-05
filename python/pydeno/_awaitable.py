"""Adapter that makes `pydeno`'s async entry points look like coroutines.

The async entry points start work eagerly on the runtime thread and return a
bare `asyncio.Future`, which is awaitable but rejected by `asyncio.create_task`
("a coroutine was expected"). Wrapping it changes only the handle's type, not
when the work starts; cancelling the task still propagates to the future.
"""

from __future__ import annotations

from collections.abc import Awaitable

# `typing.TYPE_CHECKING` without importing `typing`: the worker imports this module and does not
# otherwise need `typing` (about 2 ms of its start-up on 3.14; older asyncio imports it anyway).
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

__all__ = ["aclose_quietly", "as_coroutine"]


async def as_coroutine(awaitable: Awaitable[Any]) -> Any:
    """Await `awaitable` from inside a coroutine, so `create_task` accepts it."""
    return await awaitable


async def aclose_quietly(iterator: Any) -> None:
    """Close a cancelled stream source's iterator; its errors are not the caller's."""
    try:
        await iterator.aclose()
    except Exception:  # noqa: BLE001, S110
        pass
