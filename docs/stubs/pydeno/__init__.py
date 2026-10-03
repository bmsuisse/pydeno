from collections.abc import Callable, Mapping
from typing import Any

from ._pydeno import (
    InspectorConfig,
    InspectorEndpoints,
    JavaScriptError,
    JsFunction,
    JsStream,
    JsUndefined,
    Runtime,
    RuntimeConfig,
    RuntimeStats,
    RuntimeTerminated,
    SnapshotBuilder,
    undefined,
)

__all__ = [
    "bind_function",
    "bind_object",
    "close_default_runtime",
    "configure_default_runtime",
    "eval",
    "eval_async",
    "get_default_runtime",
    "InspectorConfig",
    "InspectorEndpoints",
    "JavaScriptError",
    "JsFunction",
    "JsStream",
    "JsUndefined",
    "Runtime",
    "RuntimeConfig",
    "RuntimeStats",
    "RuntimeTerminated",
    "SnapshotBuilder",
    "undefined",
]


def get_default_runtime() -> Runtime:
    """Return a runtime tied to the current asyncio task or thread."""
    ...


def close_default_runtime() -> None:
    """Close the current context-local runtime, if one exists."""
    ...


def configure_default_runtime(
    config: RuntimeConfig | None = None,
    *,
    isolated: bool = False,
    **isolated_options: Any,
) -> None:
    """Choose what ``pydeno.eval()`` and the other module-level functions run on.

    By default each task or thread gets an in-process ``Runtime``, which is fast but can be
    crashed or hung by hostile JavaScript. For code you do not trust, make the easy path the
    safe one::

        pydeno.configure_default_runtime(isolated=True, sandbox="require")
        pydeno.eval("1 + 1")  # now runs in a sandboxed worker process

    With ``isolated=True`` every default runtime is an ``IsolatedRuntime`` and
    ``isolated_options`` are its keyword arguments (``sandbox``, ``max_memory``, ``clock``, ...).
    See the isolated runtime guide.

    Only runtimes created after this call are affected; one that already exists keeps running
    until it is closed.
    """
    ...


def eval(code: str) -> Any:
    """Synchronously evaluate JavaScript using the default runtime."""
    ...


async def eval_async(code: str, **kwargs: Any) -> Any:
    """Evaluate JavaScript asynchronously using the default runtime."""
    ...


def bind_function(name: str, handler: Callable[..., Any]) -> int:
    """Expose a Python callable on ``globalThis``."""
    ...


def bind_object(name: str, obj: Mapping[str, Any]) -> dict[str, int]:
    """Expose a Python mapping as a JavaScript object."""
    ...
