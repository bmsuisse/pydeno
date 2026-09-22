"""`ToolBridge`: expose N Python callables to sandboxed JS, safely.

A pure-Python convenience layer over :meth:`Runtime.bind_object` -- no new
Rust. It exists because every embedder that gives a sandbox a set of callable
tools ends up building the same three things by hand: a total call budget, a
fail-closed name check, and errors JS can actually branch on.

See ``docs/guides/bindings.md`` for the guide-level walkthrough.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "ToolBridge",
    "ToolError",
    "ToolBudgetError",
    "ToolNotFoundError",
]

# A JS identifier that is also safe to install as an object property and to
# reference as `namespace.name`. Deliberately stricter than JS itself allows:
# no leading digits, no dots, no unicode, no `__proto__`-style surprises.
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Property names that would shadow or corrupt the JS object/prototype machinery
# if a tool were allowed to claim them.
_RESERVED_NAMES = frozenset(
    {
        "__proto__",
        "constructor",
        "prototype",
        "hasOwnProperty",
        "toString",
        "valueOf",
    }
)


class ToolError(Exception):
    """Base class for tool failures surfaced to JavaScript.

    Raising this (or any subclass) inside a tool gives guest JS a catchable
    error whose ``name`` is the Python class name:

        try { await tools.get_weather('Zurich') }
        catch (e) { if (e.name === 'ToolError') { ... } }

    That works for *any* Python exception type, not just these -- see
    :class:`ToolBridge` for the mechanism. These three exist so the common
    cases have a shared vocabulary.
    """


class ToolBudgetError(ToolError):
    """Raised by the bridge when its total call budget is exhausted.

    This is the one error in this module the library itself raises; the other
    two are vocabulary for *your* tools to raise.
    """


class ToolNotFoundError(ToolError):
    """For a tool to raise when the thing it was asked to look up is missing.

    ``pydeno`` never raises this itself, and deliberately does not: a tool this
    bridge does not expose is simply not a property on the namespace object, so
    guest JS gets V8's own ``TypeError: tools.nope is not a function`` -- which
    is both the correct JS semantics and a better error than a host exception
    smuggled through the op boundary. Through 0.2.x this class claimed
    otherwise ("Raised when JS asks for a tool the bridge does not expose"),
    which described a behaviour that has never existed.

    What it is good for is the inner miss, which is common enough to deserve a
    shared name::

        def read(key):
            if key not in store:
                raise ToolNotFoundError(f"no such key: {key}")
            return store[key]

    JS then catches an error named ``"ToolNotFoundError"`` and can branch on
    it, the same as for any other Python exception class.
    """


class ToolBridge:
    """Bind a set of Python callables into a sandbox under one namespace.

    Args:
        tools: Mapping of JS-visible name to Python callable. Sync and async
            callables are both supported and are detected automatically; an
            async tool becomes an awaitable JS function.
        max_calls: Total calls allowed across *all* tools on this bridge
            (not a per-tool quota). ``None`` means unlimited.
        namespace: The global object tools are installed on. ``"tools"``
            gives ``tools.get_weather(...)``. Pass ``None`` to install each
            tool as a bare global instead.
        on_exhausted: What happens on the call that exceeds ``max_calls``.
            ``"raise"`` (default) raises :class:`ToolBudgetError`, which
            reaches JS as a catchable error named ``"ToolBudgetError"``.
            ``"silent"`` returns ``None`` to JS without invoking the tool.

    Example:
        >>> from pydeno import Runtime, ToolBridge
        >>> bridge = ToolBridge({"add": lambda a, b: a + b}, max_calls=50)
        >>> with Runtime() as rt:
        ...     bridge.attach(rt)
        ...     rt.eval("tools.add(2, 3)")
        5

    Errors are typed, not stringified. Any exception a tool raises reaches
    JS as a real ``Error`` whose ``.name`` is the Python exception's class
    name and whose ``.message`` is its message, so JS can branch on it:

        >>> def lookup(key):
        ...     raise ToolNotFoundError("no such key")
        >>> bridge = ToolBridge({"lookup": lookup})
        >>> with Runtime() as rt:
        ...     bridge.attach(rt)
        ...     rt.eval("try { tools.lookup('x') } catch (e) { e.name }")
        'ToolNotFoundError'

    **The budget and the name check are load-bearing, not decorative.** The
    only thing a guest can invoke is the capability token the bind step
    installed, and what that token resolves to is the budgeted shim
    (:meth:`_wrap`) -- so every call is charged, and a name this bridge
    refused was never registered in the first place. That was not true before
    v0.2.1: op ids were sequential integers and dispatch resolved any
    registered id, so `__host_op_sync__(0, ...)` reached tools the guest had
    never been given, two bridges with different trust levels on one
    ``Runtime`` were one trust level, and the namespace was naming rather than
    isolation. Op ids are now unguessable tokens drawn from a CSPRNG and
    dispatch is gated on what a completed bind actually exposed; see the
    module docs in ``src/runtime/ops.rs``.

    Use :meth:`detach` to revoke everything this bridge installed.

    **ToolBridge requires a full `Runtime`.** Binding a Python callable
    needs an op registry to attach to -- ``Deno.core.ops`` on a real
    ``deno_core::JsRuntime`` -- which only :class:`pydeno.Runtime` provides.
    """

    __slots__ = (
        "_tools",
        "_namespace",
        "_max_calls",
        "_on_exhausted",
        "_calls",
        "_tokens",
    )

    def __init__(
        self,
        tools: Mapping[str, Callable[..., Any]],
        *,
        max_calls: int | None = None,
        namespace: str | None = "tools",
        on_exhausted: str = "raise",
    ) -> None:
        if not isinstance(tools, Mapping):
            raise TypeError("tools must be a mapping of name -> callable")
        if on_exhausted not in ("raise", "silent"):
            raise ValueError(
                f"on_exhausted must be 'raise' or 'silent', got {on_exhausted!r}"
            )
        if max_calls is not None:
            if not isinstance(max_calls, int) or isinstance(max_calls, bool):
                raise TypeError("max_calls must be an int or None")
            if max_calls < 0:
                raise ValueError("max_calls must be non-negative")
        if namespace is not None:
            self._check_name(namespace, what="namespace")

        checked: dict[str, Callable[..., Any]] = {}
        for name, func in tools.items():
            self._check_name(name, what="tool name")
            if not callable(func):
                raise TypeError(f"Tool {name!r} is not callable")
            checked[name] = func

        self._tools = checked
        self._namespace = namespace
        self._max_calls = max_calls
        self._on_exhausted = on_exhausted
        self._calls = 0
        self._tokens: list[int] = []

    # ------------------------------------------------------------------ names

    @staticmethod
    def _check_name(name: object, *, what: str) -> None:
        """Fail closed on anything that isn't a plain, safe JS identifier."""
        if not isinstance(name, str):
            raise TypeError(f"{what} must be a string, got {type(name).__name__}")
        if not _SAFE_NAME.match(name):
            raise ValueError(
                f"Invalid {what} {name!r}: must match [A-Za-z_][A-Za-z0-9_]* "
                "so it is safe to install as a JavaScript property"
            )
        if name in _RESERVED_NAMES:
            raise ValueError(
                f"Invalid {what} {name!r}: shadows a JavaScript object/prototype member"
            )

    # ----------------------------------------------------------------- budget

    @property
    def calls_made(self) -> int:
        """Number of tool calls made through this bridge so far."""
        return self._calls

    @property
    def calls_remaining(self) -> int | None:
        """Calls left in the budget, or ``None`` if unlimited."""
        if self._max_calls is None:
            return None
        return max(0, self._max_calls - self._calls)

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The tool names this bridge exposes."""
        return tuple(self._tools)

    def reset_budget(self) -> None:
        """Reset the call counter back to zero."""
        self._calls = 0

    def _spend(self, name: str) -> bool:
        """Charge one call against the budget.

        Returns True if the call may proceed. Raises :class:`ToolBudgetError`
        when the budget is exhausted and ``on_exhausted="raise"``.
        """
        if self._max_calls is not None and self._calls >= self._max_calls:
            if self._on_exhausted == "raise":
                raise ToolBudgetError(
                    f"Tool call budget exhausted ({self._max_calls} calls); "
                    f"refused {name!r}"
                )
            return False
        self._calls += 1
        return True

    # ------------------------------------------------------------------ shims

    def _wrap(self, name: str, func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap one tool in a budget-checking shim.

        The shim must preserve sync-vs-async, because `bind_object` detects
        the mode with `inspect.iscoroutinefunction` on whatever it is handed
        -- wrapping an async tool in a sync shim would register it as a sync
        op and break it. So the mode is decided once, here, and each kind
        gets its own shim.
        """
        if _is_async_callable(func):

            async def async_shim(*args: Any) -> Any:
                if not self._spend(name):
                    return None
                return await func(*args)

            async_shim.__name__ = f"{name}_budgeted"
            return async_shim

        def sync_shim(*args: Any) -> Any:
            if not self._spend(name):
                return None
            return func(*args)

        sync_shim.__name__ = f"{name}_budgeted"
        return sync_shim

    # ----------------------------------------------------------------- attach

    def attach(self, runtime: Any) -> None:
        """Install this bridge's tools into `runtime`.

        Args:
            runtime: A :class:`pydeno.Runtime`.

        Raises:
            TypeError: If `runtime` is not a `Runtime`.
        """
        self._reject_non_runtime(runtime)

        wrapped = {name: self._wrap(name, func) for name, func in self._tools.items()}

        # Keep the capability tokens so `detach` can revoke them. Nothing
        # reads them out of here and hands them to JS; they are the host's.
        if self._namespace is None:
            for name, shim in wrapped.items():
                self._tokens.append(runtime.bind_function(name, shim))
        else:
            tokens = runtime.bind_object(self._namespace, wrapped)
            self._tokens.extend(tokens.values())

    def detach(self, runtime: Any) -> int:
        """Revoke every capability this bridge installed into `runtime`.

        The bound names stay on the global object -- a guest may already have
        captured the function references anyway -- but the capability behind
        each one is gone, so calling them raises. That is the part that
        matters: a name is not authority, the token is.

        Returns:
            The number of capabilities actually revoked.
        """
        self._reject_non_runtime(runtime)
        revoked = sum(1 for token in self._tokens if runtime.revoke_op(token))
        self._tokens.clear()
        return revoked

    @staticmethod
    def _reject_non_runtime(runtime: object) -> None:
        from ._pydeno import Runtime

        if not isinstance(runtime, Runtime):
            raise TypeError(
                f"ToolBridge.attach expects a pydeno.Runtime, got "
                f"{type(runtime).__name__}"
            )

    def __repr__(self) -> str:
        budget = "unlimited" if self._max_calls is None else str(self._max_calls)
        return (
            f"ToolBridge(tools={list(self._tools)!r}, namespace={self._namespace!r}, "
            f"calls={self._calls}/{budget})"
        )


def _is_async_callable(func: Callable[..., Any]) -> bool:
    """Async-detection matching `Runtime::detect_async` in the Rust bindings.

    Checks `__call__` too, so an async callable *object* is classified the
    same way the Rust side would classify it.
    """
    if inspect.iscoroutinefunction(func):
        return True
    call = getattr(func, "__call__", None)  # noqa: B004
    return call is not None and inspect.iscoroutinefunction(call)
