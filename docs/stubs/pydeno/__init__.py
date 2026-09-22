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
