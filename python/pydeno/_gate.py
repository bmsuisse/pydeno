"""Gates: a host-side check of the exact source a sandbox is about to run.

A gate is any callable ``gate(source: str, context: GateContext)`` that returns a `Verdict` (or an
awaitable of one). Pass it as ``gate=`` to `Pydeno`, `AgentSandbox`, `IsolatedRuntime` or their
async forms, or call it yourself with `gate_check` / `async_gate_check`. A gate is defence in
depth in front of the sandbox, never the boundary: see ``docs/guides/gate.md``.

The rules, which every hook and both helpers share:

* The source is first made an exact `str` (`str.__str__`, so a subclass's own ``__str__`` or
  ``__eq__`` never runs), and that one string is what the gate sees and what runs afterwards.
* A source over `MAX_GATE_SOURCE_BYTES` (UTF-8) is denied before the gate is called.
* Fail closed. `Verdict(allow=False)` or a raised `GateDenied` is a denial (`GateDenied`). A gate
  that raises anything else, returns anything but an exact `Verdict`, or answers after
  ``gate_timeout`` could not decide (`GateUnavailable`): the code does not run either way.
* Cancellation is not a verdict: `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and
  every other `BaseException` propagate unchanged.

This module is pure Python with no I/O, and it imports everything it needs at import time, so
`static_gate` can run anywhere, a sandboxed worker included.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Union

from . import _compat
from ._errors import PydenoError
from ._limits import limit_seconds
from ._preflight import SourcePolicy, check_source

__all__ = [
    "DEFAULT_GATE_TIMEOUT",
    "MAX_GATE_SOURCE_BYTES",
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
    "static_gate",
]

#: Seconds a gate may take before it counts as unavailable (``gate_timeout=``'s default).
DEFAULT_GATE_TIMEOUT = 10.0
#: Sources larger than this (UTF-8 bytes) are denied before any gate sees them. It is the
#: isolation wire's frame cap: a larger source could not reach a worker anyway.
MAX_GATE_SOURCE_BYTES = 16 * 1024 * 1024
#: The label of a denial for size, made before the gate is called.
TOO_LARGE_LABEL = "source-too-large"


@dataclass(frozen=True)
class Verdict:
    """A gate's answer. `allow` must be a real `bool`; `labels` a tuple of strings. A denial's
    first label is its top label (`GateDenied.top_label`)."""

    allow: bool
    reason: str
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allow, bool):
            raise TypeError("Verdict.allow must be a bool")
        if not isinstance(self.reason, str):
            raise TypeError("Verdict.reason must be a str")
        if not isinstance(self.labels, tuple) or not all(
            isinstance(label, str) for label in self.labels
        ):
            raise TypeError("Verdict.labels must be a tuple of str")


@dataclass(frozen=True)
class GateContext:
    """What a gate is told besides the source. Frozen; built by the hook (or `for_source`).

    `mode` is the entry point's method (``"feed_run"``, ``"eval"``, ``"add_static_module"``,
    ...), `entry_point` the qualified name (``"PydenoSession.feed_run"``), `tools` the host
    function names the code can call, `source_length` the source's size in UTF-8 bytes and
    `source_sha256` its SHA-256 (hex), handy as a cache key. `specifier` names the module for
    module sources, else None."""

    mode: str
    entry_point: str
    tools: tuple[str, ...]
    source_length: int
    source_sha256: str
    specifier: str | None = None
    language: str = "javascript"

    @classmethod
    def for_source(
        cls,
        source: str,
        *,
        mode: str = "check",
        entry_point: str = "gate_check",
        tools: Sequence[str] = (),
        specifier: str | None = None,
    ) -> GateContext:
        """The context for `source` (an exact `str` is made of it first)."""
        data = _utf8(_exact(source))
        return cls(
            mode=mode,
            entry_point=entry_point,
            tools=tuple(tools),
            source_length=len(data),
            source_sha256=hashlib.sha256(data).hexdigest(),
            specifier=specifier,
        )


#: A gate: ``(source, context) -> Verdict``, or a coroutine function returning one.
Gate = Callable[[str, GateContext], Union[Verdict, Awaitable[Verdict]]]


class GateDenied(PydenoError):
    """A gate refused the code; nothing of it ran. `classify_error` kind ``gate_denied``, not
    retryable (the same code is refused again). `reason` and `labels` are the gate's;
    `top_label` is the first label (None without labels)."""

    def __init__(self, reason: str, labels: Sequence[str] = ()) -> None:
        if not isinstance(reason, str):
            raise TypeError("GateDenied reason must be a str")
        labels = tuple(labels)
        if not all(isinstance(label, str) for label in labels):
            raise TypeError("GateDenied labels must be str")
        super().__init__(
            f"the gate denied the code: {reason}"
            if reason
            else "the gate denied the code"
        )
        self.reason = reason
        self.labels = labels

    @property
    def top_label(self) -> str | None:
        return self.labels[0] if self.labels else None

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.reason, self.labels))


class GateUnavailable(PydenoError):
    """A gate could not decide: it raised, timed out, or answered with something that is not a
    `Verdict`. The code did not run. `classify_error` kind ``gate_unavailable``, retryable (a
    later attempt may find the gate working). A gate may raise it itself to say "try again
    shortly". The underlying exception, if any, is the ``__cause__``; it is never part of the
    message, which may be shown to a model."""

    def __init__(self, reason: str) -> None:
        if not isinstance(reason, str):
            raise TypeError("GateUnavailable reason must be a str")
        super().__init__(f"the gate is unavailable: {reason}")
        self.reason = reason

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.reason,))


# ---------------------------------------------------------------------------
# deciding
# ---------------------------------------------------------------------------


def _exact(source: Any) -> str:
    """`source` as an exact `str`, without running any method a subclass defines."""
    if type(source) is str:
        return source
    if isinstance(source, str):
        return str.__str__(source)  # the underlying data, copied into a plain str
    raise TypeError("code must be a string")


def _utf8(source: str) -> bytes:
    # A lone surrogate cannot be UTF-8; counted and hashed as it stands, never refused here.
    return source.encode("utf-8", "surrogatepass")


def _too_large(size: str) -> GateDenied:
    return GateDenied(
        f"the code is {size} bytes, over the limit of {MAX_GATE_SOURCE_BYTES} bytes; "
        "send a shorter program",
        (TOO_LARGE_LABEL,),
    )


def _prepare(
    source: Any,
    *,
    mode: str,
    entry_point: str,
    tools: Sequence[str],
    specifier: str | None,
) -> tuple[str, GateContext]:
    """The exact source and its context; refuses an oversized source before hashing it."""
    source = _exact(source)
    if len(source) > MAX_GATE_SOURCE_BYTES:  # never fewer bytes than characters
        raise _too_large(f"more than {MAX_GATE_SOURCE_BYTES}")
    data = _utf8(source)
    if len(data) > MAX_GATE_SOURCE_BYTES:
        raise _too_large(str(len(data)))
    context = GateContext(
        mode=mode,
        entry_point=entry_point,
        tools=tuple(tools),
        source_length=len(data),
        source_sha256=hashlib.sha256(data).hexdigest(),
        specifier=specifier,
    )
    return source, context


def _accept(result: Any) -> Verdict:
    """An allowing `Verdict`, or `GateDenied` / `GateUnavailable`."""
    if type(result) is not Verdict:
        raise GateUnavailable(
            f"the gate returned {type(result).__name__}, not a Verdict"
        )
    allow, reason, labels = result.allow, result.reason, result.labels
    if (
        not isinstance(reason, str)
        or not isinstance(labels, tuple)
        or not all(isinstance(label, str) for label in labels)
    ):
        raise GateUnavailable("the gate returned a malformed Verdict")
    if allow is True:
        return result
    if allow is False:
        raise GateDenied(reason, labels)
    raise GateUnavailable("the gate returned a malformed Verdict")


def _discard(awaitable: Any) -> None:
    close = getattr(awaitable, "close", None)  # a coroutine: no "never awaited" warning
    if close is not None:
        close()
        return
    cancel = getattr(awaitable, "cancel", None)  # a future or task
    if cancel is not None:
        cancel()


def _late(timeout: float) -> GateUnavailable:
    return GateUnavailable(f"the gate took longer than gate_timeout={timeout:g}s")


def _raised(exc: BaseException) -> GateUnavailable:
    return GateUnavailable(f"the gate raised {type(exc).__name__}")


def _decide(
    gate: Any, source: str, context: GateContext, timeout: float | None
) -> Verdict:
    """Call a gate synchronously. It cannot be interrupted; a verdict after the deadline is
    discarded (unavailable)."""
    started = time.monotonic()
    try:
        result = gate(source, context)
    except (GateDenied, GateUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed; BaseException propagates
        raise _raised(exc) from exc
    if inspect.isawaitable(result):
        _discard(result)
        raise GateUnavailable(
            "the gate returned an awaitable, which a synchronous entry point cannot await; "
            "use the async API or a sync gate"
        )
    if timeout is not None and time.monotonic() - started > timeout:
        raise _late(timeout)
    return _accept(result)


async def _adecide(
    gate: Any, source: str, context: GateContext, timeout: float | None
) -> Verdict:
    """Call a gate from a coroutine: an async gate is awaited and cancelled at the deadline (a
    sync one is called inline and cannot be). A verdict after the deadline is discarded, also
    from a gate that swallowed its cancellation."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    try:
        async with _compat.timeout(timeout):
            result = gate(source, context)
            if inspect.isawaitable(result):
                result = await result
    except (GateDenied, GateUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed; BaseException propagates
        if deadline is not None and loop.time() >= deadline:
            raise _late(timeout) from exc  # type: ignore[arg-type]
        raise _raised(exc) from exc
    if deadline is not None and loop.time() > deadline:
        raise _late(timeout)  # type: ignore[arg-type]
    return _accept(result)


def _is_async_gate(gate: Any) -> bool:
    """True for a coroutine function (or a partial of one, or an object whose ``__call__`` is
    one, such as `all_of` over an async gate)."""
    while isinstance(gate, functools.partial):
        gate = gate.func
    if inspect.iscoroutinefunction(gate):
        return True
    return inspect.iscoroutinefunction(getattr(type(gate), "__call__", None))


def _check_context(context: Any, source: str) -> GateContext:
    if not isinstance(context, GateContext):
        raise TypeError("context must be a GateContext")
    data = _utf8(source)
    if (
        context.source_length != len(data)
        or context.source_sha256 != hashlib.sha256(data).hexdigest()
    ):
        raise ValueError("context was made for another source")
    return context


def gate_check(
    gate: Gate,
    source: str,
    context: GateContext | None = None,
    *,
    timeout: float | None = DEFAULT_GATE_TIMEOUT,
) -> Verdict:
    """Run `gate` on `source` the way every hook does, synchronously: returns the allowing
    `Verdict`, or raises `GateDenied` / `GateUnavailable`. For a host that gates in its own
    process before handing the code on. An async gate is unavailable here (use
    `async_gate_check`). `context` defaults to one for mode ``"check"``; one you pass must have
    been made for this source (`GateContext.for_source`)."""
    timeout = limit_seconds("timeout", timeout)
    if not callable(gate):
        raise TypeError("gate must be callable")
    exact, made = _prepare(
        source, mode="check", entry_point="gate_check", tools=(), specifier=None
    )
    return _decide(
        gate,
        exact,
        made if context is None else _check_context(context, exact),
        timeout,
    )


async def async_gate_check(
    gate: Gate,
    source: str,
    context: GateContext | None = None,
    *,
    timeout: float | None = DEFAULT_GATE_TIMEOUT,
) -> Verdict:
    """`gate_check` for a coroutine: awaits an async gate, cancelling it at `timeout`."""
    timeout = limit_seconds("timeout", timeout)
    if not callable(gate):
        raise TypeError("gate must be callable")
    exact, made = _prepare(
        source, mode="check", entry_point="gate_check", tools=(), specifier=None
    )
    return await _adecide(
        gate,
        exact,
        made if context is None else _check_context(context, exact),
        timeout,
    )


class _Hook:
    """A gate as an entry point holds it: the timeout, and whether it may be async."""

    __slots__ = ("gate", "timeout", "who")

    def __init__(self, gate: Any, timeout: float | None, who: str) -> None:
        self.gate = gate
        self.timeout = timeout
        self.who = who

    def check(
        self,
        source: Any,
        mode: str,
        tools: Sequence[str] = (),
        specifier: str | None = None,
    ) -> str:
        """The exact source to run, once the gate allowed it."""
        exact, context = _prepare(
            source,
            mode=mode,
            entry_point=f"{self.who}.{mode}",
            tools=tools,
            specifier=specifier,
        )
        _decide(self.gate, exact, context, self.timeout)
        return exact

    async def acheck(
        self,
        source: Any,
        mode: str,
        tools: Sequence[str] = (),
        specifier: str | None = None,
    ) -> str:
        exact, context = _prepare(
            source,
            mode=mode,
            entry_point=f"{self.who}.{mode}",
            tools=tools,
            specifier=specifier,
        )
        await _adecide(self.gate, exact, context, self.timeout)
        return exact


def _gated_loader(
    hook: _Hook,
    loader: Callable[[str], Any],
    tools: Callable[[], Sequence[str]],
    record: Callable[[BaseException], None],
) -> Callable[[str], Any]:
    """`loader` (a module loader: specifier -> source) with every source it returns gated
    before the worker compiles it (mode ``"module_loader"``). A refusal is passed to `record`,
    so the command that triggered the import can raise it, and raised into the worker, where
    the import fails with an error named after it (its message, written by the host, is kept).
    With an async loader or an async gate the wrapper is async (a sync loader then runs in an
    executor, never on the event loop)."""

    def refused(exc: BaseException) -> BaseException:
        record(exc)
        exc._pydeno_public = True  # type: ignore[attr-defined]
        return exc

    def specifier_of(specifier: Any) -> str | None:
        return specifier if isinstance(specifier, str) else None

    def need_text(source: Any) -> None:
        if not isinstance(source, str):
            raise TypeError(
                "a gated module loader must return the module's source as a str"
            )

    loader_is_async = inspect.iscoroutinefunction(loader)
    if loader_is_async or _is_async_gate(hook.gate):

        async def gated_async(specifier: str) -> str:
            if loader_is_async:
                source = await loader(specifier)
            else:  # a sync loader may block: not on the event loop
                source = await asyncio.get_running_loop().run_in_executor(
                    None, loader, specifier
                )
            need_text(source)
            try:
                return await hook.acheck(
                    source, "module_loader", tools(), specifier_of(specifier)
                )
            except (GateDenied, GateUnavailable) as exc:
                raise refused(exc) from None

        return gated_async

    def gated(specifier: str) -> str:
        source = loader(specifier)
        need_text(source)
        try:
            return hook.check(source, "module_loader", tools(), specifier_of(specifier))
        except (GateDenied, GateUnavailable) as exc:
            raise refused(exc) from None

    return gated


def _hook(gate: Any, gate_timeout: Any, *, who: str, sync_only: bool) -> _Hook | None:
    """Validate ``gate=`` / ``gate_timeout=`` for an entry point; None without a gate."""
    timeout = limit_seconds("gate_timeout", gate_timeout)
    if gate is None:
        return None
    if not callable(gate):
        raise TypeError("gate must be callable: (source, context) -> Verdict")
    if sync_only and _is_async_gate(gate):
        raise TypeError(
            f"{who} calls its gate synchronously, and this gate is async; pass a sync gate, or "
            f"use the async class"
        )
    return _Hook(gate, timeout, who)


# ---------------------------------------------------------------------------
# ready-made gates
# ---------------------------------------------------------------------------

#: Most findings listed in a `static_gate` denial's reason (the rest are counted).
_MAX_REASON_FINDINGS = 20


class StaticGate:
    """`static_gate(policy)`: denies code with any error finding of `check_source(source,
    policy=policy)`. Pure, deterministic, no I/O: safe in any process, a sandboxed worker
    included. The reason lists findings as ``line:column [rule] message`` (stable templates, see
    `pydeno.POLICY_MESSAGES`); the labels are the rules, in order of first appearance."""

    __slots__ = ("policy",)

    def __init__(self, policy: SourcePolicy) -> None:
        if not isinstance(policy, SourcePolicy):
            raise TypeError("static_gate takes a SourcePolicy")
        self.policy = policy

    def __call__(self, source: str, context: GateContext | None = None) -> Verdict:
        result = check_source(source, policy=self.policy)
        errors = [f for f in result.findings if f.severity == "error"]
        if not errors:
            return Verdict(True, "")
        lines = [
            f"{f.line}:{f.column} [{f.rule}] {f.message}"
            for f in errors[:_MAX_REASON_FINDINGS]
        ]
        if len(errors) > _MAX_REASON_FINDINGS:
            lines.append(f"... and {len(errors) - _MAX_REASON_FINDINGS} more findings")
        return Verdict(
            False, "\n".join(lines), tuple(dict.fromkeys(f.rule for f in errors))
        )

    def __repr__(self) -> str:
        return f"static_gate({self.policy!r})"


def static_gate(policy: SourcePolicy) -> StaticGate:
    """A sync `Gate` that applies a `SourcePolicy` (see `StaticGate`). Heuristic, like
    `check_source`: a first, cheap layer, never the boundary."""
    return StaticGate(policy)


def _merge(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(label for group in groups for label in group))


class _Fold:
    """The combinators' bookkeeping, shared by the sync and async forms."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.allowed: list[Verdict] = []
        self.denied: list[GateDenied] = []
        self.unavailable: GateUnavailable | None = None

    def add(self, outcome: Verdict | GateDenied | GateUnavailable) -> Verdict | None:
        """The combined verdict once it is decided, else None (keep going)."""
        if isinstance(outcome, GateUnavailable):
            if self.kind == "all_of":
                raise outcome
            self.unavailable = self.unavailable or outcome
            return None
        if isinstance(outcome, GateDenied):
            if self.kind == "all_of":
                # First denial wins; the labels of the gates that allowed before it follow its own.
                return Verdict(
                    False,
                    outcome.reason,
                    _merge(outcome.labels, *(v.labels for v in self.allowed)),
                )
            self.denied.append(outcome)
            return None
        if self.kind == "any_of":
            return outcome  # first allow wins
        self.allowed.append(outcome)
        return None

    def end(self) -> Verdict:
        if self.kind == "all_of":
            return Verdict(
                True,
                "; ".join(v.reason for v in self.allowed if v.reason),
                _merge(*(v.labels for v in self.allowed)),
            )
        if self.unavailable is not None:
            raise self.unavailable
        return Verdict(
            False,
            "\n".join(d.reason for d in self.denied if d.reason),
            _merge(*(d.labels for d in self.denied)),
        )


def _outcome(call: Callable[[], Verdict]) -> Verdict | GateDenied | GateUnavailable:
    try:
        return call()
    except (GateDenied, GateUnavailable) as exc:
        return exc


class _Combined:
    __slots__ = ("gates", "kind")

    def __init__(self, kind: str, gates: tuple[Any, ...]) -> None:
        self.kind = kind
        self.gates = gates

    def __repr__(self) -> str:
        return f"{self.kind}({', '.join(map(repr, self.gates))})"


class _SyncCombined(_Combined):
    __slots__ = ()

    def __call__(self, source: str, context: GateContext) -> Verdict:
        fold = _Fold(self.kind)
        for gate in self.gates:
            decided = fold.add(
                _outcome(functools.partial(_decide, gate, source, context, None))
            )
            if decided is not None:
                return decided
        return fold.end()


class _AsyncCombined(_Combined):
    __slots__ = ()

    async def __call__(self, source: str, context: GateContext) -> Verdict:
        fold = _Fold(self.kind)
        for gate in self.gates:
            try:
                outcome: Verdict | GateDenied | GateUnavailable = await _adecide(
                    gate, source, context, None
                )
            except (GateDenied, GateUnavailable) as exc:
                outcome = exc
            decided = fold.add(outcome)
            if decided is not None:
                return decided
        return fold.end()


def _combine(kind: str, gates: tuple[Any, ...]) -> _Combined:
    if not gates:
        raise TypeError(f"{kind}() needs at least one gate")
    if not all(callable(gate) for gate in gates):
        raise TypeError(f"{kind}() takes gates (callables)")
    cls = _AsyncCombined if any(_is_async_gate(g) for g in gates) else _SyncCombined
    return cls(kind, gates)


def all_of(*gates: Gate) -> Gate:
    """Allow only if every gate allows. Gates run in order and the first denial wins: later gates
    are not called, and the denial carries its own reason and labels, then the labels of the
    gates that allowed before it. All allowing: reasons joined, labels merged. A gate that
    cannot decide makes the whole check unavailable. Async if any gate is async."""
    return _combine("all_of", gates)


def any_of(*gates: Gate) -> Gate:
    """Allow if any gate allows: the first allowing verdict wins and later gates are not called.
    Otherwise a denial with every reason and label merged (the first denial's labels lead), or,
    when some gate could not decide, unavailable. Async if any gate is async."""
    return _combine("any_of", gates)
