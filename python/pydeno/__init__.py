"""High-level Python bindings for the pydeno runtime."""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import sys
import threading
from collections.abc import Callable

from ._pydeno import (
    InspectorConfig,
    InspectorEndpoints,
    JavaScriptError,
    JsFunction,
    JsStream,
    JsUndefined,
    Runtime,
    RuntimeConfig,
    RuntimeForceKilled,
    RuntimeStats,
    RuntimeTerminated,
    RuntimeTimeout,
    SnapshotBuilder,
    TerminationHandle,
    SUGGESTED_FORCE_KILL_GRACE,
    undefined,
)

# `typing.TYPE_CHECKING` without importing `typing`: the worker imports this module and does not
# otherwise need `typing` (about 2 ms of its start-up on 3.14; older asyncio imports it anyway).
TYPE_CHECKING = False

if TYPE_CHECKING:  # the real imports are lazy, see `__getattr__`
    from typing import Any, TypeVar, overload

    from ._aio_front import AsyncPydeno, AsyncPydenoSession, AsyncPydenoSnapshot
    from ._front import (
        Pydeno,
        PydenoComplete,
        PydenoCrashedError,
        PydenoError,
        PydenoLimits,
        PydenoRuntimeError,
        PydenoSession,
        PydenoSnapshot,
        PydenoSyntaxError,
        PydenoTimeoutError,
        ToolThreadLimitError,
    )
    from ._agent import (
        AgentSandbox,
        Done,
        Failed,
        JournalError,
        ReplayDivergence,
        ToolCall,
        ToolNotDiscoveredError,
        describe_tools,
        typescript_stubs,
    )
    from ._errors import ErrorInfo, classify_error
    from ._aio import AsyncIsolatedRuntime
    from ._aio_agent import AsyncAgentSandbox
    from ._isolated import IsolatedRuntime, WorkerCrashed
    from ._result import ExecutionResult, ResultTooLarge
    from ._schema import SchemaTool
    from ._polyfills import WEB_POLYFILLS
    from ._preflight import (
        POLICY_MESSAGES,
        Finding,
        PreflightResult,
        SourcePolicy,
        check_source,
    )
    from ._gate import (
        Gate,
        GateContext,
        GateDenied,
        GateUnavailable,
        StaticGate,
        Verdict,
        all_of,
        any_of,
        async_gate_check,
        gate_check,
        gate_threads,
        set_gate_threads,
        static_gate,
    )
    from ._pool import (
        InMemoryJournalStore,
        JournalStore,
        JournalTooLarge,
        PoolFull,
        SessionBusy,
        SessionPool,
        StaleJournal,
    )
    from ._snapshot_auth import (
        SnapshotAuthenticationError,
        sign_snapshot,
        verify_snapshot,
    )
    from ._sandbox_pool import AsyncSandboxPool, SandboxPool
    from ._status import Layer, SandboxStatus, sandbox_status
    from ._tools import ToolBridge, ToolBudgetError, ToolError, ToolNotFoundError
    from .tools.http_fetch import (
        AsyncHttpFetch,
        HttpFetch,
        HttpFetchBlocked,
        HttpFetchError,
        HttpFetchFailed,
        HttpFetchTimeout,
        http_fetch,
    )

# Everything below is imported on first use. `import pydeno` is then just the native module, which
# keeps start-up small for plain `Runtime` users and for the isolation worker (which has no use for
# the parent-side machinery: subprocess, tempfile, asyncio, ...).
_LAZY = {
    "Pydeno": "_front",
    "PydenoSession": "_front",
    "PydenoSnapshot": "_front",
    "PydenoComplete": "_front",
    "PydenoLimits": "_front",
    "PydenoError": "_errors",
    "PydenoRuntimeError": "_front",
    "PydenoSyntaxError": "_front",
    "PydenoCrashedError": "_front",
    "PydenoTimeoutError": "_front",
    "ToolThreadLimitError": "_front",
    "AsyncPydeno": "_aio_front",
    "AsyncPydenoSession": "_aio_front",
    "AsyncPydenoSnapshot": "_aio_front",
    "AgentSandbox": "_agent",
    "ToolCall": "_agent",
    "Done": "_agent",
    "Failed": "_agent",
    "ReplayDivergence": "_agent",
    "JournalError": "_agent",
    "describe_tools": "_agent",
    "typescript_stubs": "_agent",
    "ToolNotDiscoveredError": "_agent",
    "ExecutionResult": "_result",
    "ResultTooLarge": "_result",
    "SchemaTool": "_schema",
    "AsyncIsolatedRuntime": "_aio",
    "AsyncAgentSandbox": "_aio_agent",
    "SessionPool": "_pool",
    "JournalStore": "_pool",
    "InMemoryJournalStore": "_pool",
    "SessionBusy": "_pool",
    "PoolFull": "_pool",
    "StaleJournal": "_pool",
    "JournalTooLarge": "_pool",
    "IsolatedRuntime": "_isolated",
    "WorkerCrashed": "_isolated",
    "SandboxPool": "_sandbox_pool",
    "AsyncSandboxPool": "_sandbox_pool",
    "WEB_POLYFILLS": "_polyfills",
    "SnapshotAuthenticationError": "_snapshot_auth",
    "sign_snapshot": "_snapshot_auth",
    "verify_snapshot": "_snapshot_auth",
    "ToolBridge": "_tools",
    "ToolBudgetError": "_tools",
    "ToolError": "_tools",
    "ToolNotFoundError": "_tools",
    "ErrorInfo": "_errors",
    "classify_error": "_errors",
    "Finding": "_preflight",
    "PreflightResult": "_preflight",
    "check_source": "_preflight",
    "SourcePolicy": "_preflight",
    "POLICY_MESSAGES": "_preflight",
    "Gate": "_gate",
    "GateContext": "_gate",
    "GateDenied": "_gate",
    "GateUnavailable": "_gate",
    "StaticGate": "_gate",
    "Verdict": "_gate",
    "all_of": "_gate",
    "any_of": "_gate",
    "async_gate_check": "_gate",
    "gate_check": "_gate",
    "gate_threads": "_gate",
    "set_gate_threads": "_gate",
    "static_gate": "_gate",
    "Layer": "_status",
    "SandboxStatus": "_status",
    "sandbox_status": "_status",
    "http_fetch": "tools.http_fetch",
    "HttpFetch": "tools.http_fetch",
    "AsyncHttpFetch": "tools.http_fetch",
    "HttpFetchError": "tools.http_fetch",
    "HttpFetchBlocked": "tools.http_fetch",
    "HttpFetchTimeout": "tools.http_fetch",
    "HttpFetchFailed": "tools.http_fetch",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'pydeno' has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:
    F = TypeVar("F", bound=Callable[..., Any])
else:

    def overload(func: object) -> object:  # only type checkers read the overloads
        return func


@overload
def _runtime_bind(self: Runtime, func: F, /, *, name: str | None = ...) -> F: ...


@overload
def _runtime_bind(
    self: Runtime, func: None = ..., /, *, name: str | None = ...
) -> Callable[[F], F]: ...


def _runtime_bind(
    self: Runtime, func: object | None = None, /, *, name: str | None = None
) -> Callable[[F], F] | F:
    """Bind a Python callable to the runtime via a decorator-friendly API.

    The helper accepts both synchronous and asynchronous callables, forwarding
    registration to :meth:`Runtime.bind_function` while returning the original
    callable for continued direct usage.
    """

    def _register(target: F) -> F:
        binding_name = name if name is not None else getattr(target, "__name__", None)
        if not binding_name:
            raise ValueError("A function name is required when binding to JavaScript")
        self.bind_function(binding_name, target)
        return target

    if func is None:
        return _register
    if not callable(func):
        raise TypeError("runtime.bind expects a callable or to be used as a decorator")
    return _register(func)  # type: ignore[arg-type]


class _RuntimeSlot:
    __slots__ = ("runtime", "owner", "closed")

    def __init__(self, runtime: Runtime | IsolatedRuntime, owner: object) -> None:
        self.runtime = runtime
        self.owner = owner
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.runtime.is_closed():
            self.runtime.close()


def _current_runtime_owner() -> object:
    # A running task can only exist if asyncio has been imported, so a plain script never pays
    # for importing it here.
    asyncio = sys.modules.get("asyncio")
    if asyncio is not None:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is not None:
            return task
    return threading.current_thread()


def _schedule_owner_cleanup(slot: _RuntimeSlot) -> None:
    asyncio = sys.modules.get("asyncio")
    owner = slot.owner
    if asyncio is not None and isinstance(owner, asyncio.Task):
        owner.add_done_callback(lambda _: slot.close())


setattr(Runtime, "bind", _runtime_bind)


# Standard library only, so importing it costs next to nothing (the isolation worker needs it too).
from ._wasm import AsyncWasmModule, WasmModule  # noqa: E402
from ._wasm import runtime_load_wasm as _runtime_load_wasm  # noqa: E402

setattr(Runtime, "load_wasm", _runtime_load_wasm)


_default_runtime_var: contextvars.ContextVar[_RuntimeSlot | None] = (
    contextvars.ContextVar("pydeno_default_runtime", default=None)
)


_default_factory: Callable[[], Any] = Runtime


def configure_default_runtime(
    config: RuntimeConfig | None = None,
    *,
    isolated: bool = False,
    **isolated_options: Any,
) -> None:
    """Choose what `pydeno.eval()` and the other module-level functions run on.

    By default each task or thread gets an in-process `Runtime`, which is fast but can be
    crashed or hung by hostile JavaScript. For code you do not trust, make the *easy* path the
    safe one:

        pydeno.configure_default_runtime(isolated=True, sandbox="require")
        pydeno.eval("1 + 1")        # now runs in a sandboxed worker process

    With ``isolated=True`` every default runtime is an :class:`IsolatedRuntime` and
    ``isolated_options`` are its keyword arguments (``sandbox``, ``max_memory``, ``clock``, ...).
    With ``isolated=False`` (the default) it is a plain `Runtime`, optionally built from `config`.

    Only runtimes created after this call are affected; one that already exists keeps running
    until it is closed (``close_default_runtime()``).
    """
    global _default_factory  # noqa: PLW0603
    if isolated:
        if config is not None:
            isolated_options["config"] = config
        from ._isolated import IsolatedRuntime

        _default_factory = lambda: IsolatedRuntime(**isolated_options)  # noqa: E731
    else:
        if isolated_options:
            raise TypeError(
                "options such as "
                f"{sorted(isolated_options)} apply to isolated=True; pass them with it"
            )
        _default_factory = (lambda: Runtime(config)) if config is not None else Runtime


def get_default_runtime() -> Runtime | IsolatedRuntime:
    """Get or create a runtime isolated to the current context.

    In an asyncio app, this is per-task (e.g., per-request).
    In a sync app, this is per-thread.

    The runtime is a plain `Runtime` with default configuration unless
    :func:`configure_default_runtime` chose otherwise. For custom configuration
    (heap limits, bootstrap code, etc.), use the Runtime class directly.

    Returns:
        The context-local runtime instance.
    """
    slot = _default_runtime_var.get()
    owner = _current_runtime_owner()
    if slot is None or slot.runtime.is_closed() or slot.owner is not owner:
        slot = _RuntimeSlot(runtime=_default_factory(), owner=owner)
        _default_runtime_var.set(slot)
        _schedule_owner_cleanup(slot)
    return slot.runtime


def close_default_runtime() -> None:
    """Close the current context's runtime, if one exists."""
    slot = _default_runtime_var.get()
    if slot is None:
        return
    owner = _current_runtime_owner()
    if slot.owner is not owner:
        raise RuntimeError("Default runtime can only be closed from its owner context.")
    slot.close()
    _default_runtime_var.set(None)


@atexit.register
def _close_default_runtime_on_exit() -> None:
    with contextlib.suppress(RuntimeError):
        close_default_runtime()


def eval(code: str) -> Any:
    """Evaluate JavaScript code synchronously using the default context-local runtime.

    This is a convenience function for simple use cases. Each asyncio task or thread
    gets its own isolated runtime automatically.

    For custom configuration or fine-grained control, use the Runtime class directly.

    Args:
        code: JavaScript code to evaluate.

    Returns:
        The result of the JavaScript evaluation, converted to Python types.

    Raises:
        JavaScriptError: If the JavaScript code throws an exception.

    Example:
        >>> import pydeno
        >>> pydeno.eval("2 + 2")
        4
        >>> pydeno.eval("Math.sqrt(16)")
        4.0
    """
    return get_default_runtime().eval(code)


async def eval_async(code: str, **kwargs: Any) -> Any:
    """Evaluate JavaScript code asynchronously using the default context-local runtime.

    This is a convenience function for simple async use cases. Each asyncio task or
    thread gets its own isolated runtime automatically.

    For custom configuration or fine-grained control, use the Runtime class directly.

    Args:
        code: JavaScript code to evaluate.
        **kwargs: Additional arguments passed to Runtime.eval_async (e.g., timeout).

    Returns:
        The result of the JavaScript evaluation, converted to Python types.

    Raises:
        JavaScriptError: If the JavaScript code throws an exception.
        RuntimeTimeout: If timeout is specified and exceeded.

    Example:
        >>> import asyncio
        >>> import pydeno
        >>> asyncio.run(pydeno.eval_async("Promise.resolve(42)"))
        42
    """
    return await get_default_runtime().eval_async(code, **kwargs)


def bind_function(name: str, handler: Callable[..., Any]) -> int:
    """Bind a Python function to the default context-local runtime.

    The function will be available as a global in JavaScript. Both sync and async
    Python functions are supported.

    Args:
        name: The name to bind in JavaScript globalThis.
        handler: The Python callable to bind (sync or async).

    Returns:
        The op's capability token, for :meth:`Runtime.revoke_op`.

    Example:
        >>> import pydeno
        >>> pydeno.bind_function("add", lambda a, b: a + b)
        >>> pydeno.eval("add(2, 3)")
        5
    """
    return get_default_runtime().bind_function(name, handler)


def bind_object(name: str, obj: dict) -> dict[str, int]:
    """Bind a Python dict as a JavaScript object in the default context-local runtime.

    Args:
        name: The name to bind in JavaScript globalThis.
        obj: The Python dict to expose as a JavaScript object.

    Returns:
        The capability token of each callable key, for
        :meth:`Runtime.revoke_op`.

    Example:
        >>> import pydeno
        >>> pydeno.bind_object("config", {"version": "1.0", "debug": True})
        >>> pydeno.eval("config.version")
        '1.0'
    """
    return get_default_runtime().bind_object(name, obj)


__all__ = [
    "Pydeno",
    "AsyncPydeno",
    "PydenoSession",
    "AsyncPydenoSession",
    "PydenoSnapshot",
    "AsyncPydenoSnapshot",
    "PydenoComplete",
    "PydenoLimits",
    "PydenoError",
    "PydenoRuntimeError",
    "PydenoSyntaxError",
    "PydenoCrashedError",
    "PydenoTimeoutError",
    "ToolThreadLimitError",
    "classify_error",
    "ErrorInfo",
    "check_source",
    "PreflightResult",
    "Finding",
    "SourcePolicy",
    "POLICY_MESSAGES",
    "Gate",
    "GateContext",
    "GateDenied",
    "GateUnavailable",
    "StaticGate",
    "Verdict",
    "all_of",
    "any_of",
    "async_gate_check",
    "gate_check",
    "gate_threads",
    "set_gate_threads",
    "static_gate",
    "sandbox_status",
    "SandboxStatus",
    "Layer",
    "AsyncIsolatedRuntime",
    "AsyncAgentSandbox",
    "SessionPool",
    "JournalStore",
    "InMemoryJournalStore",
    "SessionBusy",
    "PoolFull",
    "StaleJournal",
    "JournalTooLarge",
    "eval",
    "eval_async",
    "get_default_runtime",
    "configure_default_runtime",
    "close_default_runtime",
    "bind_function",
    "bind_object",
    "Runtime",
    "IsolatedRuntime",
    "SandboxPool",
    "AsyncSandboxPool",
    "AgentSandbox",
    "ToolCall",
    "Done",
    "Failed",
    "ReplayDivergence",
    "JournalError",
    "describe_tools",
    "typescript_stubs",
    "ExecutionResult",
    "ResultTooLarge",
    "SchemaTool",
    "ToolNotDiscoveredError",
    "WEB_POLYFILLS",
    "WorkerCrashed",
    "WasmModule",
    "AsyncWasmModule",
    "SnapshotAuthenticationError",
    "sign_snapshot",
    "verify_snapshot",
    "RuntimeConfig",
    "InspectorConfig",
    "InspectorEndpoints",
    "SnapshotBuilder",
    "JsFunction",
    "JsUndefined",
    "RuntimeStats",
    "JavaScriptError",
    "RuntimeTerminated",
    "RuntimeForceKilled",
    "RuntimeTimeout",
    "SUGGESTED_FORCE_KILL_GRACE",
    "undefined",
    "JsStream",
    "TerminationHandle",
    "ToolBridge",
    "ToolError",
    "ToolBudgetError",
    "ToolNotFoundError",
    "http_fetch",
    "HttpFetch",
    "AsyncHttpFetch",
    "HttpFetchError",
    "HttpFetchBlocked",
    "HttpFetchTimeout",
    "HttpFetchFailed",
]
