"""Adapter that makes `pydeno`'s async entry points look like coroutines.

Every async entry point (`Runtime.eval_async`, `Runtime.eval_module_async`,
`JsFunction.call_async`, and `JsFunction.__call__` when the JS function returns
a promise) starts its work on the runtime thread immediately and settles an
`asyncio.Future` from that thread. A bare `Future` is awaitable, so `await
f.call_async()` always worked -- but it is not a *coroutine*, and
`asyncio.create_task` rejects it:

    TypeError: a coroutine was expected, got <Future pending ...>

`create_task` is the first thing anyone reaches for when they want two JS calls
in flight, so the answer is to hand back something it accepts. The work is
still started eagerly by the call itself -- this wrapper only changes the type
of the handle, not when the work begins -- and cancelling a task that is
awaiting one of these propagates to the underlying future's done callback
exactly as cancelling the future directly used to.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

__all__ = ["as_coroutine"]


async def as_coroutine(awaitable: Awaitable[Any]) -> Any:
    """Await `awaitable` from inside a coroutine, so `create_task` accepts it."""
    return await awaitable
