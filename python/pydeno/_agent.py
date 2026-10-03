"""`AgentSandbox`: a stateful JavaScript sandbox for AI agents, with pause/resume at tool calls.

What pydantic/monty offers an agent that writes Python, for an agent that writes JavaScript, built
only on `IsolatedRuntime`'s public API:

- **Tools** from a plain ``name -> callable`` mapping, checked like `ToolBridge` names, with one
  call budget for the whole session.
- **Prompt helpers**: `describe_tools()` (a block for the system prompt) and `typescript_stubs()`
  (a ``.d.ts`` the model can be shown, and its code checked against).
- **Session state**: one worker per session, so globals and functions survive between runs.
- **Pause/resume** (Monty's ``FunctionSnapshot``): `start()` stops at every tool call and hands it
  to the caller, who answers with `resume()`; `run()` answers them with the real tools.
- **Durability by deterministic replay**: V8 cannot serialise a half-run isolate, so a session is
  persisted as a signed journal of its inputs (code and tool results) plus a hash of every outcome.
  `AgentSandbox.load()` replays it on a fresh worker with the same frozen clock and random seed,
  substituting the recorded tool results, and raises `ReplayDivergence` if any outcome differs.

Everything the guest sends (tool names it calls, their arguments, its results) is untrusted data.
The tools run with the host's full authority; validate their arguments.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import collections.abc
import hashlib
import hmac
import inspect
import itertools
import json
import math
import os
import re
import secrets
import threading
import types
import typing
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ._isolated import IsolatedRuntime
from ._pydeno import JsUndefined, RuntimeConfig, undefined
from ._snapshot_auth import _engine_version
from ._snapshot_auth import _key as _checked_key
from ._tools import ToolBridge, ToolBudgetError

__all__ = [
    "AgentSandbox",
    "Done",
    "Failed",
    "JournalError",
    "ReplayDivergence",
    "Step",
    "ToolCall",
    "describe_tools",
    "typescript_stubs",
]

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_PAUSE = 600.0
DEFAULT_MAX_JOURNAL_BYTES = 8 * 1024 * 1024
# Calls arriving after their run ended. The settle step in `_wrap` makes these impossible for code
# that leaves the prelude's wrappers alone; a guest that tampers with them can still produce some.
# They are never answered (see `_Core.on_tool_call`), so each holds a pending future here and in
# the worker until the session closes: hence a cap.
_MAX_ABANDONED_CALLS = 64
_JOURNAL_FORMAT = 1
# Distinct from the snapshot magic, so a signed snapshot can never be loaded as a journal, nor the
# other way round, even under the same key.
_MAGIC = b"pydeno-agent2\x00"
_MAX_ASSOCIATED_DATA = 1024
_MAC_LEN = hashlib.sha256().digest_size
# Top-level declarations, recognised only at the start of a line (no parser: a convenience, not a
# guarantee). Their bindings are copied to `globalThis` when a run ends; see `_wrap`.
_DECLARATION = re.compile(
    r"^(?:(?:async[ \t]+)?function\*?[ \t]*|class[ \t]+|(?:const|let|var)[ \t]+)"
    r"([A-Za-z_$][\w$]*)",
    re.MULTILINE | re.ASCII,  # JavaScript identifiers are narrower than Unicode "\w"
)
_SETTLE = "__pydeno_agent_settle"
_PERSIST = "__pydeno_agent_persist"
_SAFE_ERROR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# JavaScript words that cannot be a parameter name in a `.d.ts`; Python allows several of them.
_JS_RESERVED = frozenset(
    "break case catch class const continue debugger default delete do else enum export extends "
    "false finally for function if import in instanceof new null return super switch this throw "
    "true try typeof var void while with yield let static implements interface package private "
    "protected public await arguments eval".split()
)
# Owned by the session because replay depends on them, or because the pause model needs them.
_OWNED_OPTIONS = frozenset({"clock", "random_seed", "request_timeout", "max_host_wait"})


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """The guest called a tool and is waiting for the answer.

    `name` is one of the session's tools; `args` are the guest's positional arguments, plain data
    it chose (untrusted: validate before acting on them). Answer with `AgentSandbox.resume`.
    """

    name: str
    args: tuple[Any, ...]
    call_id: int
    _session: int = field(default=0, repr=False, compare=False)


@dataclass(frozen=True)
class Done:
    """The run finished; `value` is its result (`pydeno.undefined` when it had none)."""

    value: Any


@dataclass(frozen=True)
class Failed:
    """The run failed. A JavaScript error leaves the session usable; a crash, a hard timeout or
    a memory kill closes it (`AgentSandbox.is_closed()`)."""

    error: BaseException


Step = ToolCall | Done | Failed


class ReplayDivergence(RuntimeError):
    """Replaying a journal produced a different outcome than the one recorded."""


class JournalError(ValueError):
    """A journal is too large, malformed, or not authentic (tampered or wrongly keyed)."""


_SESSION_IDS = itertools.count(1)


class _Missing:
    pass


_MISSING: Any = _Missing()


# ---------------------------------------------------------------------------
# values: a tagged JSON form for the journal and the outcome hashes
# ---------------------------------------------------------------------------


def _encode(value: Any, *, canonical: bool = False, depth: int = 0) -> Any:
    """Plain data -> JSON-able structure, in the vocabulary values cross the worker boundary in.

    `canonical` sorts set members, so a hash does not depend on Python's per-process string hash
    seed. Anything outside the vocabulary is a `TypeError`."""
    if depth > 200:
        raise TypeError("value is nested too deeply")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2**53 else {"$": "int", "v": str(value)}
    if isinstance(value, float):
        if math.isfinite(value) and not (value == 0 and math.copysign(1, value) < 0):
            return value
        return {"$": "f", "v": repr(value)}
    if isinstance(value, JsUndefined):
        return {"$": "u"}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$": "b", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, datetime):
        return {"$": "dt", "v": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_encode(v, canonical=canonical, depth=depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        members = [_encode(v, canonical=canonical, depth=depth + 1) for v in value]
        if canonical:
            members.sort(key=lambda m: json.dumps(m, sort_keys=True))
        return {"$": "set", "v": members}
    if isinstance(value, dict):
        return {
            "$": "d",
            "v": [
                [
                    _encode(k, canonical=canonical, depth=depth + 1),
                    _encode(v, canonical=canonical, depth=depth + 1),
                ]
                for k, v in value.items()
            ],
        }
    raise TypeError(
        f"{type(value).__name__} cannot cross into the sandbox (plain data only: None, bool, "
        "int, float, str, bytes, list, tuple, dict, set, datetime, pydeno.undefined)"
    )


def _decode(node: Any) -> Any:
    if node is None or isinstance(node, (bool, int, float, str)):
        return node
    if isinstance(node, list):
        return [_decode(v) for v in node]
    if isinstance(node, dict):
        tag = node.get("$")
        if tag == "int":
            return int(node["v"])
        if tag == "f":
            return float(node["v"])
        if tag == "u":
            return undefined
        if tag == "b":
            return base64.b64decode(node["v"], validate=True)
        if tag == "dt":
            return datetime.fromisoformat(node["v"])
        if tag == "set":
            return {_decode(v) for v in node["v"]}
        if tag == "d":
            return {_decode(k): _decode(v) for k, v in node["v"]}
    raise JournalError("malformed value in journal")


def _digest(*parts: Any) -> str:
    """Hash of an outcome. Values the encoder does not know (none should reach here: they all came
    across the wire) are hashed by type name, so an outcome is never unhashable."""

    def enc(v: Any) -> Any:
        try:
            return _encode(v, canonical=True)
        except TypeError:
            return {"$": "?", "v": type(v).__name__}

    blob = json.dumps([enc(p) for p in parts], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _outcome(step: Step) -> tuple[str, str]:
    if isinstance(step, ToolCall):
        return "call", _digest("call", step.name, list(step.args))
    if isinstance(step, Done):
        return "done", _digest("done", step.value)
    return "failed", _digest("failed", type(step.error).__name__, str(step.error))


def _error_class(name: str) -> type[Exception]:
    """An exception class with a given name. The guest learns a host error's class name and its
    message, nothing else, so a plain class of the same name reproduces exactly what it saw, live
    and on replay. (Not the builtin of that name: `str(KeyError(m))` is `repr(m)`, not `m`.)"""
    if not _SAFE_ERROR_NAME.fullmatch(name):
        name = "Exception"
    return type(name, (Exception,), {})


# ---------------------------------------------------------------------------
# prompt helpers
# ---------------------------------------------------------------------------


def _union(parts: list[str]) -> str:
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    if "unknown" in seen:
        return "unknown"
    # `null` last reads the way people write it: `string | null`.
    if "null" in seen:
        seen = [p for p in seen if p != "null"] + ["null"]
    return " | ".join(seen)


def _array(element: str) -> str:
    return f"({element})[]" if " | " in element else f"{element}[]"


_SEQUENCES = (
    list,
    collections.abc.Sequence,
    collections.abc.MutableSequence,
    collections.abc.Iterable,
    collections.abc.Collection,
)
_MAPPINGS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)
_SETS = (set, frozenset, collections.abc.Set, collections.abc.MutableSet)


def ts_type(annotation: Any) -> str:
    """TypeScript for one Python annotation. What cannot be expressed is `unknown`."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return "unknown"
    if annotation is None or annotation is type(None):
        return "null"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    if annotation is str:
        return "string"
    if annotation in (bytes, bytearray, memoryview):
        return "Uint8Array"
    if annotation is datetime:
        return "Date"
    if annotation is JsUndefined:
        return "undefined"
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return ts_type(args[0])
    if origin in (typing.Union, types.UnionType):
        return _union([ts_type(a) for a in args])
    if origin is typing.Literal:
        literals = []
        for value in args:
            if value is None:
                literals.append("null")
            elif isinstance(value, (bool, int, float, str)):
                literals.append(json.dumps(value))
            else:
                return "unknown"
        return _union(literals)
    if annotation is tuple or origin is tuple:
        if not args:
            return "unknown[]"
        if len(args) == 2 and args[1] is Ellipsis:
            return _array(ts_type(args[0]))
        if args == ((),):
            return "[]"
        return "[" + ", ".join(ts_type(a) for a in args) + "]"
    if annotation in _SEQUENCES:
        return "unknown[]"
    if annotation in _SETS:
        return "Set<unknown>"
    if annotation in _MAPPINGS:
        return "Record<string, unknown>"
    if origin in _SEQUENCES:
        return _array(ts_type(args[0]) if args else "unknown")
    if origin in _SETS:
        return f"Set<{ts_type(args[0]) if args else 'unknown'}>"
    if origin in _MAPPINGS:
        if not args:
            return "Record<string, unknown>"
        if args[0] in (str, Any):
            return f"Record<string, {ts_type(args[1])}>"
        return "unknown"
    return "unknown"


@dataclass(frozen=True)
class _Param:
    name: str
    ts: str
    optional: bool
    rest: bool


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    params: tuple[_Param, ...]
    returns: str
    doc: str
    required_keyword_only: tuple[str, ...]

    def signature(self) -> str:
        rendered = []
        for p in self.params:
            if p.rest:
                rendered.append(f"...{p.name}: {_array(p.ts)}")
            else:
                rendered.append(f"{p.name}{'?' if p.optional else ''}: {p.ts}")
        return f"{self.name}({', '.join(rendered)}): Promise<{self.returns}>"

    def example(self, prefix: str) -> str:
        args = [
            _placeholder(p.ts, p.name)
            for p in self.params
            if not p.optional and not p.rest
        ]
        return f"const result = await {prefix}{self.name}({', '.join(args)});"


def _placeholder(ts: str, name: str) -> str:
    if ts.endswith("[]") or ts.startswith("["):
        return "[]"
    first = ts.split(" | ")[0]
    if (
        first.startswith('"')
        or first in ("true", "false")
        or re.fullmatch(r"-?[\d.]+", first)
    ):
        return first
    if first.endswith("[]"):
        return "[]"
    return {
        "string": json.dumps(name),
        "number": "1",
        "boolean": "true",
        "Uint8Array": "new Uint8Array([1, 2, 3])",
        "Date": "new Date()",
        "null": "null",
        "undefined": "undefined",
    }.get(
        first,
        "{}"
        if first.startswith("Record<")
        else "new Set()"
        if first.startswith("Set<")
        else "null",
    )


def _js_param_name(name: str) -> str:
    return f"{name}_" if name in _JS_RESERVED else name


def _hints(func: Callable[..., Any]) -> dict[str, Any]:
    target = (
        func
        if inspect.isfunction(func) or inspect.ismethod(func)
        else getattr(func, "__call__", func)
    )
    try:
        return typing.get_type_hints(target, include_extras=True)
    except Exception:  # noqa: BLE001 - unresolvable forward references: fall back below
        return {}


def _spec(name: str, func: Callable[..., Any]) -> _ToolSpec:
    doc = inspect.getdoc(func) or ""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return _ToolSpec(
            name, (_Param("args", "unknown", False, True),), "unknown", doc, ()
        )
    hints = _hints(func)

    def annotation(param_name: str, raw: Any) -> Any:
        found = hints.get(param_name, raw)
        return inspect.Parameter.empty if isinstance(found, str) else found

    params: list[_Param] = []
    required_kw: list[str] = []
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            params.append(
                _Param(
                    _js_param_name(p.name),
                    ts_type(annotation(p.name, p.annotation)),
                    p.default is not p.empty,
                    False,
                )
            )
        elif p.kind is p.VAR_POSITIONAL:
            params.append(
                _Param(
                    _js_param_name(p.name),
                    ts_type(annotation(p.name, p.annotation)),
                    True,
                    True,
                )
            )
        elif p.kind is p.KEYWORD_ONLY and p.default is p.empty:
            required_kw.append(p.name)
    returns = ts_type(annotation("return", sig.return_annotation))
    return _ToolSpec(name, tuple(params), returns, doc, tuple(required_kw))


def _specs(tools: Mapping[str, Callable[..., Any]]) -> list[_ToolSpec]:
    return [_spec(name, func) for name, func in tools.items()]


_PREAMBLE = """\
You can run JavaScript in a sandbox. Write the code as the body of an async function:
call tools with `await` and `return` the final result. Top-level `const`, `let`, `var`,
`function` and `class` declarations written at the start of a line are kept for later
runs; to keep anything else, store it on `globalThis`. There is no network, filesystem,
`require` or `import`. `Date.now()` is frozen and `Math.random()` is seeded. A tool that
fails throws an Error whose `name` is the failure's type.
"""


def describe_tools(
    tools: Mapping[str, Callable[..., Any]], *, namespace: str | None = None
) -> str:
    """A block for an LLM's system prompt: how code runs, then each tool's signature, docstring
    and an example call. The signatures are the ones `typescript_stubs` declares."""
    prefix = f"{namespace}." if namespace else ""
    where = f"on the `{namespace}` object" if namespace else "as global functions"
    lines = [
        _PREAMBLE,
        f"These tools are available {where}; each returns a Promise.",
        "",
    ]
    for spec in _specs(tools):
        lines.append(prefix + spec.signature())
        for doc_line in spec.doc.splitlines():
            lines.append(f"    {doc_line}".rstrip())
        lines.append(f"    Example: {spec.example(prefix)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _jsdoc(doc: str, indent: str) -> list[str]:
    if not doc:
        return []
    body = doc.replace("*/", "*\\/").splitlines()
    return (
        [f"{indent}/**"]
        + [f"{indent} * {line}".rstrip() for line in body]
        + [f"{indent} */"]
    )


def typescript_stubs(
    tools: Mapping[str, Callable[..., Any]], *, namespace: str | None = None
) -> str:
    """A `.d.ts` declaring the tools, from their Python signatures, annotations and docstrings.

    `int`/`float` become `number`, `str` `string`, `bool` `boolean`, `bytes` `Uint8Array`,
    `list[T]` `T[]`, `dict[str, T]` `Record<string, T>`, `X | None` `X | null`; a parameter with a
    default is optional; anything else is `unknown`. Every tool returns a `Promise`."""
    out = ["// Tools provided by the host. Every call returns a Promise.", ""]
    indent = "  " if namespace else ""
    if namespace:
        out.append(f"declare namespace {namespace} {{")
    for spec in _specs(tools):
        out.extend(_jsdoc(spec.doc, indent))
        keyword = "function" if namespace else "declare function"
        out.append(f"{indent}{keyword} {spec.signature()};")
    if namespace:
        out.append("}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


class _Run:
    """One `start()`: its event queue (read by the caller) and its unanswered calls."""

    __slots__ = ("events", "final", "pending")

    def __init__(self) -> None:
        self.events: collections.deque[Step] = collections.deque()
        self.final: Step | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}


class _Core:
    """Everything the loop thread touches. It never refers to the `AgentSandbox`, so a session
    the caller drops can be collected, and its finalizer can shut this down."""

    def __init__(
        self, rt: IsolatedRuntime, session_id: int, max_tool_calls: int | None
    ) -> None:
        self.rt = rt
        self.session_id = session_id
        self.max_tool_calls = max_tool_calls
        self.calls_made = 0
        self.ids = itertools.count(1)
        self.cond = threading.Condition()
        self.run: _Run | None = None
        self.abandoned: list[asyncio.Future[Any]] = []
        self.closed = False
        self.pid = os.getpid()
        self.task: asyncio.Future[Any] | None = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._serve, name="pydeno-agent-loop", daemon=True
        )
        self.thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                self.loop.run_until_complete(
                    asyncio.gather(
                        *asyncio.all_tasks(self.loop), return_exceptions=True
                    )
                )
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
            finally:
                self.loop.close()

    # -- on the loop thread ------------------------------------------------

    async def on_tool_call(self, name: str, args: list[Any]) -> Any:
        """A bound tool, as the guest sees it: charge the budget, hand the call to whoever drives
        the session, and wait for their answer."""
        if self.max_tool_calls is not None and self.calls_made >= self.max_tool_calls:
            raise ToolBudgetError(
                f"tool call budget exhausted ({self.max_tool_calls} calls); refused {name!r}"
            )
        self.calls_made += 1
        future: asyncio.Future[Any] = self.loop.create_future()
        run = self.run
        with self.cond:
            if run is None or run.final is not None or self.closed:
                # A call the run did not wait for, arriving after it finished. It is never
                # answered: a reply reaching the worker after its command ended would break it.
                if len(self.abandoned) >= _MAX_ABANDONED_CALLS:
                    raise RuntimeError("too many abandoned tool calls in this session")
                self.abandoned.append(future)
            else:
                call = ToolCall(name, tuple(args), next(self.ids), self.session_id)
                run.pending[call.call_id] = future
                run.events.append(call)
                self.cond.notify_all()
        return await future

    async def execute(self, run: _Run, code: str) -> None:
        try:
            final: Step = Done(await self.rt.eval_async(_wrap(code)))
        except BaseException as exc:  # noqa: BLE001 - every failure is the run's outcome
            final = Failed(exc)
        with self.cond:
            run.final = final
            # Calls still unanswered now belong to nobody: drop them from what the caller will
            # see, and keep their futures pending (see `on_tool_call`).
            run.events = collections.deque(
                e for e in run.events if not isinstance(e, ToolCall)
            )
            self.abandoned.extend(f for f in run.pending.values() if not f.done())
            run.pending.clear()
            run.events.append(final)
            self.cond.notify_all()

    def answer(
        self, run: _Run, call_id: int, value: Any, error: BaseException | None
    ) -> bool:
        with self.cond:
            future = run.pending.pop(call_id, None)
            if future is None or future.done() or run.final is not None:
                return False
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)
        return True

    # -- on the caller's thread --------------------------------------------

    def on_loop(self, coro: Any) -> Any:
        """Run a coroutine on the session's loop and wait for it, without hanging if the session
        is closed meanwhile."""
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:  # the loop is closed
            coro.close()
            raise RuntimeError("the session is closed") from None
        while True:
            try:
                return future.result(0.25)
            except TimeoutError:
                if not self.thread.is_alive():
                    raise RuntimeError("the session is closed") from None

    def call_on_loop(self, fn: Callable[..., Any], *args: Any) -> Any:
        async def call() -> Any:
            return fn(*args)

        return self.on_loop(call())

    def next_step(self, run: _Run) -> Step:
        with self.cond:
            while True:
                if run.events:
                    return run.events.popleft()
                if self.closed:
                    return Failed(RuntimeError("the session was closed"))
                self.cond.wait(0.1)

    def shutdown(self) -> None:
        if os.getpid() != self.pid:
            # A fork()ed child: the worker and the loop thread belong to the parent. The thread does
            # not even exist here, so waiting on its loop would stall for the whole drain timeout.
            self.closed = True
            return
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.cond.notify_all()
        try:
            self.rt.close()
        except Exception:  # noqa: BLE001, S110 - closing must not fail half-way
            pass
        on_loop_thread = threading.current_thread() is self.thread
        if self.loop.is_closed():
            return
        if not on_loop_thread:

            async def drain() -> None:
                # The worker is gone, so the run's command has ended or is about to.
                if self.task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(self.task), 15)
                    except BaseException:  # noqa: BLE001, S110
                        pass

            try:
                asyncio.run_coroutine_threadsafe(drain(), self.loop).result(20)
            except Exception:  # noqa: BLE001, S110
                pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:  # the loop already closed
            return
        if not on_loop_thread:
            self.thread.join(20)


class AgentSandbox:
    """A stateful, pausable JavaScript session for an AI agent, in an `IsolatedRuntime`.

    Args:
        tools: ``name -> callable`` (sync or async). Names follow `ToolBridge`'s rules. The guest
            calls them with positional arguments and gets a Promise.
        max_tool_calls: Total tool calls the guest may make over the session's life (across
            every `start`, `resume` and `run`); further calls throw a ``ToolBudgetError`` in the
            guest. ``None``: unlimited.
        namespace: Install the tools on this global object (``tools.search(...)``) instead of
            as bare globals.
        clock: The guest's frozen clock (a `datetime`, naive meaning UTC, or epoch seconds).
            Default: the moment the session is created. It never advances, and a loaded session
            gets the recorded one, which is what makes replay deterministic.
        random_seed: Seed for `Math.random` (default: a random one, recorded in the journal).
        timeout: Hard limit, in seconds, on the guest's own running time per run. Time paused at
            a tool call does not count. Exceeding it kills the worker and closes the session.
        max_pause: Most time (seconds) one run may spend waiting on tool answers in total, so a
            session nobody resumes does not hold a worker forever. Exceeding it closes the session.
        max_journal_bytes: Cap on the recorded journal. Past it the session keeps working, but
            `dump()` raises `JournalError`.
        **runtime_options: Passed to `IsolatedRuntime` (``config``, ``max_memory``, ``sandbox``,
            ``redact_host_errors``, ...). ``config.timeout`` is refused: a soft timeout would also
            count the time paused at a tool call.
    """

    def __init__(
        self,
        tools: Mapping[str, Callable[..., Any]],
        *,
        max_tool_calls: int | None = None,
        namespace: str | None = None,
        clock: datetime | float | int | None = None,
        random_seed: int | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        max_pause: float | None = DEFAULT_MAX_PAUSE,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        **runtime_options: Any,
    ) -> None:
        self._tools = _check_tools(tools)
        if max_tool_calls is not None and (
            not isinstance(max_tool_calls, int)
            or isinstance(max_tool_calls, bool)
            or max_tool_calls < 0
        ):
            raise ValueError("max_tool_calls must be a non-negative int or None")
        if namespace is not None:
            ToolBridge._check_name(namespace, what="namespace")  # noqa: SLF001
        owned = _OWNED_OPTIONS & runtime_options.keys()
        if owned:
            raise TypeError(
                f"AgentSandbox sets {sorted(owned)} itself (use clock=, random_seed=, "
                "timeout=, max_pause=)"
            )
        config = runtime_options.get("config")
        if isinstance(config, RuntimeConfig) and config.timeout is not None:
            raise ValueError(
                "RuntimeConfig.timeout is not supported by AgentSandbox: it would count the time "
                "a run is paused at a tool call. Use AgentSandbox(timeout=...)."
            )
        if clock is None:
            clock = datetime.now(timezone.utc)
        if isinstance(clock, datetime):
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=timezone.utc)
            clock_ms = int(clock.timestamp() * 1000)
        elif isinstance(clock, (int, float)) and not isinstance(clock, bool):
            clock_ms = int(clock * 1000)
        else:
            raise ValueError("clock must be a datetime, epoch seconds, or None (now)")
        if random_seed is None:
            random_seed = secrets.randbelow(2**31)
        if not isinstance(max_journal_bytes, int) or max_journal_bytes <= 0:
            raise ValueError("max_journal_bytes must be a positive int")

        self._namespace = namespace
        self._max_tool_calls = max_tool_calls
        self._redact = bool(runtime_options.get("redact_host_errors", True))
        self._clock_ms = clock_ms
        self._random_seed = random_seed
        self._max_journal_bytes = max_journal_bytes
        self._redact = bool(runtime_options.get("redact_host_errors", True))
        self._lock = threading.Lock()
        self._records: list[list[Any]] | None = []
        self._journal_size = 0
        self._dead = False
        self._paused: ToolCall | None = None
        self._run: _Run | None = None

        rt = IsolatedRuntime(
            clock=clock_ms / 1000,
            random_seed=random_seed,
            request_timeout=timeout,
            max_host_wait=max_pause,
            **runtime_options,
        )
        try:
            self._core = _Core(rt, next(_SESSION_IDS), max_tool_calls)
        except BaseException:
            rt.close()
            raise
        self._finalizer = weakref.finalize(self, self._core.shutdown)
        try:
            self._bind()
        except BaseException:
            self.close()
            raise

    def _bind(self) -> None:
        core = self._core

        def shim_for(name: str) -> Callable[..., Any]:
            async def shim(*args: Any) -> Any:
                return await core.on_tool_call(name, list(args))

            shim.__name__ = name
            return shim

        shims = {name: shim_for(name) for name in self._tools}
        if self._namespace is None:
            for name, shim in shims.items():
                core.rt.bind_function(name, shim)
        elif shims:
            core.rt.bind_object(self._namespace, shims)
        core.rt.eval(_prelude(list(self._tools), self._namespace))

    # -- introspection -------------------------------------------------------

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    @property
    def calls_made(self) -> int:
        """Tool calls the guest has made so far (across all runs)."""
        return self._core.calls_made

    @property
    def calls_remaining(self) -> int | None:
        if self._max_tool_calls is None:
            return None
        return max(0, self._max_tool_calls - self._core.calls_made)

    @property
    def clock(self) -> datetime:
        """The guest's frozen clock."""
        return datetime.fromtimestamp(self._clock_ms / 1000, tz=timezone.utc)

    @property
    def random_seed(self) -> int:
        return self._random_seed

    @property
    def pending(self) -> ToolCall | None:
        """The tool call the session is paused at, if any (also after `load`)."""
        return self._paused

    def is_closed(self) -> bool:
        return self._core.closed or self._dead

    def describe_tools(self) -> str:
        """See the module-level `describe_tools`."""
        return describe_tools(self._tools, namespace=self._namespace)

    def typescript_stubs(self) -> str:
        """See the module-level `typescript_stubs`."""
        return typescript_stubs(self._tools, namespace=self._namespace)

    # -- running -------------------------------------------------------------

    def _enter(self) -> None:
        if threading.current_thread() is self._core.thread:
            raise RuntimeError(
                "an AgentSandbox cannot be driven from one of its own tools"
            )
        if not self._lock.acquire(blocking=False):
            raise RuntimeError(
                "AgentSandbox is busy (another thread is using it, or a tool called back into "
                "its own session)"
            )

    def start(self, code: str) -> Step:
        """Run `code` until it calls a tool (`ToolCall`), finishes (`Done`) or fails (`Failed`).

        The code is the body of an async function: ``await`` tools, ``return`` the result. When
        it ends, its top-level ``const``/``let``/``var``/``function``/``class`` declarations
        written at the start of a line are copied to ``globalThis``, so later runs see them; any
        other state must be put on ``globalThis`` explicitly. A run does not end while one of its
        tool calls is unanswered, even one it did not ``await``.
        """
        self._enter()
        try:
            return self._start(code)
        finally:
            self._lock.release()

    def resume(
        self,
        step: ToolCall,
        value: Any = _MISSING,
        *,
        error: BaseException | None = None,
    ) -> Step:
        """Answer the tool call the session is paused at, with a return `value` or an `error`
        (the guest sees an Error whose ``name`` is the exception's class; its message is replaced
        unless the session was made with ``redact_host_errors=False``)."""
        self._enter()
        try:
            return self._resume(step, value, error)
        finally:
            self._lock.release()

    def run(self, code: str) -> Any:
        """`start` the code and answer every tool call with the real tool; return the result, or
        raise what the run failed with. A tool that raises is reported to the guest, which may
        catch it. Tool calls are answered one at a time, in the order the guest made them."""
        self._enter()
        try:
            step = self._start(code)
            while isinstance(step, ToolCall):
                try:
                    result = self._call_tool(step)
                except Exception as exc:  # noqa: BLE001 - the guest sees the failure
                    step = self._resume(step, _MISSING, exc)
                else:
                    step = self._resume(step, result, None)
        finally:
            self._lock.release()
        if isinstance(step, Failed):
            raise step.error
        return step.value

    def _call_tool(self, call: ToolCall) -> Any:
        tool = self._tools[call.name]
        result = tool(*call.args)
        if inspect.isawaitable(result):

            async def wait() -> Any:
                return await result

            result = self._core.on_loop(wait())
        try:
            _encode(result)
        except TypeError as exc:
            raise TypeError(
                f"tool {call.name!r} returned a value the sandbox cannot hold"
            ) from exc
        return result

    def _check_usable(self) -> None:
        if self._dead:
            raise RuntimeError(
                "the session's worker is gone (crashed, killed or timed out)"
            )
        if self._core.closed:
            raise RuntimeError("the session is closed")

    def _start(self, code: str) -> Step:
        if not isinstance(code, str):
            raise TypeError("code must be a string")
        self._check_usable()
        if self._paused is not None:
            raise RuntimeError("the session is paused at a tool call; resume it first")
        run = _Run()
        core = self._core
        self._run = run

        def begin() -> None:
            core.run = run
            core.task = asyncio.ensure_future(core.execute(run, code))

        core.call_on_loop(begin)
        self._record(["run", code])
        return self._observe(core.next_step(run))

    def _resume(self, step: ToolCall, value: Any, error: BaseException | None) -> Step:
        if not isinstance(step, ToolCall):
            raise TypeError("resume() takes the ToolCall the session is paused at")
        self._check_usable()
        if (
            self._paused is None
            or step._session != self._core.session_id  # noqa: SLF001
            or step.call_id != self._paused.call_id
        ):
            raise RuntimeError(
                "that tool call is not the one this session is paused at"
            )
        if (value is _MISSING) == (error is None):
            raise TypeError("resume() takes exactly one of value= or error=")
        if error is not None:
            if not isinstance(error, Exception):
                raise TypeError("error must be an Exception instance")
            name = type(error).__name__
            message = "host function failed" if self._redact else str(error)
            record = ["ans", "e", name, message]
            sent: BaseException = _error_class(name)(message)
        else:
            # A TypeError here is the caller's to fix; nothing has been answered yet.
            encoded = _encode(value)
            record = ["ans", "v", encoded]
            sent_value = _decode(encoded)  # exactly what a replay will send
        self._record(record)
        run = self._run
        assert run is not None
        self._paused = None
        self._core.call_on_loop(
            self._core.answer,
            run,
            step.call_id,
            None if error is not None else sent_value,
            sent if error is not None else None,
        )
        return self._observe(self._core.next_step(run))

    def _observe(self, step: Step) -> Step:
        kind, digest = _outcome(step)
        self._record(["obs", kind, digest])
        if isinstance(step, ToolCall):
            self._paused = step
        else:
            self._paused = None
            if self._core.rt.is_closed():
                # The worker is gone (crash, hard timeout, memory kill, `max_pause`): nothing
                # more can run, so give back the thread now rather than at `close()`.
                self._dead = True
                self._core.shutdown()
        return step

    def _record(self, record: list[Any]) -> None:
        if self._records is None:
            return
        self._journal_size += len(json.dumps(record, separators=(",", ":")))
        if self._journal_size > self._max_journal_bytes:
            self._records = None  # free it; `dump` explains
            return
        self._records.append(record)

    # -- durability ----------------------------------------------------------

    def dump(self, key: bytes, *, associated_data: bytes = b"") -> bytes:
        """The session's journal, HMAC-SHA256-signed with `key` (at least 16 bytes).

        It holds every run's code and every tool answer (errors as their class name, and their
        message only with ``redact_host_errors=False``), plus a hash of every outcome. It is
        signed, not encrypted. Restore with `AgentSandbox.load`.

        `associated_data` (for example a tenant or session id) is folded into the signature but not
        stored: `load` must be given the same bytes, so a journal cannot be loaded as another tenant's.
        A journal alone cannot stop *rollback* (loading an older dump of the same session restores
        its spent tool budget): keep a counter in your own store and put it in `associated_data`."""
        self._enter()
        try:
            if self._dead:
                raise JournalError(
                    "the session's worker is gone; its last run cannot be replayed (dump after "
                    "each step you want to be able to return to)"
                )
            if self._records is None:
                raise JournalError(
                    f"the journal grew past max_journal_bytes={self._max_journal_bytes}"
                )
            payload = json.dumps(
                {
                    "format": _JOURNAL_FORMAT,
                    "config": {
                        "clock_ms": self._clock_ms,
                        "random_seed": self._random_seed,
                        "max_tool_calls": self._max_tool_calls,
                        "namespace": self._namespace,
                        "tools": list(self._tools),
                        "release": _engine_version().decode(errors="replace"),
                        "redact": self._redact,
                    },
                    "records": self._records,
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
            return _seal(payload, key, associated_data)
        finally:
            self._lock.release()

    @classmethod
    def load(
        cls,
        blob: bytes,
        key: bytes,
        tools: Mapping[str, Callable[..., Any]],
        *,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        associated_data: bytes = b"",
        **options: Any,
    ) -> AgentSandbox:
        """Rebuild a session from `dump()` output by replaying it on a fresh worker.

        The MAC is checked before anything runs. Recorded tool answers are replayed; the real
        tools are never called. If the session was dumped while paused at a tool call, the
        returned session is paused at the same call (`pending`). Pass the same tools (by name)
        and the same runtime options as the original. Raises `JournalError` for a blob that is
        not authentic or not well-formed, and `ReplayDivergence` (closing the new session) if
        the guest does not behave exactly as recorded."""
        if not isinstance(blob, (bytes, bytearray)):
            raise TypeError("blob must be bytes")
        if len(blob) > max_journal_bytes * 2 + len(_MAGIC) + _MAC_LEN + 4096:
            raise JournalError("journal is larger than max_journal_bytes allows")
        journal = _parse(_open(bytes(blob), key, associated_data))
        config = journal["config"]
        made_by = _engine_version().decode(errors="replace")
        if config["release"] != made_by:
            # Before any worker starts: replaying under another engine would only fail later, as
            # a divergence, after running the guest's code.
            raise JournalError(
                f"the journal was recorded by pydeno {config['release']!r}, this is {made_by!r}"
            )
        if bool(options.get("redact_host_errors", True)) != config["redact"]:
            raise JournalError(
                "the journal was recorded with a different redact_host_errors setting"
            )
        tools = _check_tools(tools)
        if list(tools) != config["tools"]:
            raise JournalError(
                f"the journal was recorded with tools {config['tools']}, not {list(tools)}"
            )
        for owned in ("clock", "random_seed", "max_tool_calls", "namespace"):
            if owned in options:
                raise TypeError(f"{owned} comes from the journal")
        session = cls(
            tools,
            max_tool_calls=config["max_tool_calls"],
            namespace=config["namespace"],
            clock=config["clock_ms"] / 1000,
            random_seed=config["random_seed"],
            max_journal_bytes=max_journal_bytes,
            **options,
        )
        try:
            session._replay(journal["records"])
        except (ValueError, TypeError, OverflowError) as exc:
            session.close()
            raise JournalError(f"malformed journal: {type(exc).__name__}") from None
        except BaseException:
            session.close()
            raise
        return session

    def _replay(self, records: list[list[Any]]) -> None:
        step: Step | None = None
        # The input (a run's code, or a tool answer) that produces the next recorded outcome.
        pending: tuple[str, Any] | None = None
        for index, record in enumerate(records):
            op = record[0]
            if op == "run":
                if pending is not None or isinstance(step, ToolCall):
                    raise JournalError(
                        f"record {index}: a run starts before the last one ended"
                    )
                pending = ("run", record[1])
            elif op == "ans":
                if pending is not None or not isinstance(step, ToolCall):
                    raise JournalError(
                        f"record {index}: an answer with no tool call to answer"
                    )
                pending = ("ans", record)
            else:  # "obs"
                if pending is None:
                    raise JournalError(
                        f"record {index}: an outcome with no input before it"
                    )
                kind, input_ = pending
                pending = None
                if kind == "run":
                    step = self._start(input_)
                else:
                    assert isinstance(step, ToolCall)
                    if input_[1] == "v":
                        step = self._resume(step, _decode(input_[2]), None)
                    else:
                        step = self._resume(
                            step, _MISSING, _error_class(input_[2])(input_[3])
                        )
                got_kind, got_digest = _outcome(step)
                if (got_kind, got_digest) != (record[1], record[2]):
                    detail = (
                        f"{got_kind} {step.name!r}"
                        if isinstance(step, ToolCall)
                        else got_kind
                    )
                    raise ReplayDivergence(
                        f"replay diverged at journal record {index}: recorded a {record[1]}, "
                        f"got a different {detail} (the guest read something nondeterministic, "
                        "or the journal does not belong to this code and these tools)"
                    )
        if pending is not None:
            raise JournalError("the journal ends with an input that has no outcome")

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Stop the worker and the session's thread. Idempotent; safe while paused."""
        if threading.current_thread() is self._core.thread:
            raise RuntimeError("a tool cannot close the session that is running it")
        self._paused = None
        self._finalizer()

    def __enter__(self) -> AgentSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self.is_closed() else "paused" if self._paused else "idle"
        return f"AgentSandbox(tools={list(self._tools)!r}, {state}, calls={self.calls_made})"


def _prelude(names: list[str], namespace: str | None) -> str:
    """Installed once, before any guest code: wraps each tool so the session knows which calls are
    in flight, and defines the settle step every run ends with.

    Why: a worker that finishes a command while one of its tool calls is unanswered breaks when
    that answer arrives later, so a run must not end with a call in flight (a call the code did
    not `await`, or one still running when `Promise.all` rejected). Intrinsics are captured first,
    so a guest that later replaces `Promise` or `Set` methods only breaks its own runs."""
    holder = f"globalThis[{json.dumps(namespace)}]" if namespace else "globalThis"
    return f"""(() => {{
  "use strict";
  const call = Function.prototype.call.bind.bind(Function.prototype.call);
  const apply = Reflect.apply;
  const then = call(Promise.prototype.then);
  const add = call(Set.prototype.add);
  const remove = call(Set.prototype.delete);
  const forEach = call(Set.prototype.forEach);
  const size = call(Object.getOwnPropertyDescriptor(Set.prototype, "size").get);
  const push = call(Array.prototype.push);
  const allSettled = Promise.allSettled.bind(Promise);
  const inflight = new Set();
  const holder = {holder};
  for (const name of {json.dumps(names)}) {{
    const raw = holder[name];
    holder[name] = {{ [name](...args) {{
      const p = apply(raw, undefined, args);
      add(inflight, p);
      const done = () => {{ remove(inflight, p); }};
      then(p, done, done);
      return p;
    }} }}[name];
  }}
  Object.defineProperty(globalThis, "{_SETTLE}", {{
    value: async () => {{
      while (size(inflight)) {{
        const pending = [];
        forEach(inflight, (p) => {{ push(pending, p); }});
        await allSettled(pending);
      }}
    }},
    writable: false,
    enumerable: false,
    configurable: false,
  }});
}})();"""


def _wrap(code: str) -> str:
    """A run as the body of an async function, which keeps its top-level declarations and does
    not end while one of its tool calls is in flight.

    The declarations are copied by a closure defined in the same block as the code, so it sees
    their bindings; one not yet initialised (or not a declaration at all: the pattern has no
    parser behind it) is skipped. The code starts on line 1, so error line numbers match it."""
    names = dict.fromkeys(
        n for n in _DECLARATION.findall(code) if n not in _JS_RESERVED
    )
    saves = "".join(f"try {{ globalThis.{n} = {n}; }} catch {{}} " for n in names)
    return (
        f"(async () => {{ let {_PERSIST}; try {{ {_PERSIST} = () => {{ {saves}}}; "
        + code
        + f"\n}} finally {{ if ({_PERSIST}) {_PERSIST}(); await {_SETTLE}(); }} }})()"
    )


def _check_tools(
    tools: Mapping[str, Callable[..., Any]],
) -> dict[str, Callable[..., Any]]:
    if not isinstance(tools, Mapping):
        raise TypeError("tools must be a mapping of name -> callable")
    checked: dict[str, Callable[..., Any]] = {}
    for name, func in tools.items():
        ToolBridge._check_name(name, what="tool name")  # noqa: SLF001
        if not callable(func):
            raise TypeError(f"tool {name!r} is not callable")
        required_kw = _spec(name, func).required_keyword_only
        if required_kw:
            raise ValueError(
                f"tool {name!r} has required keyword-only parameters {list(required_kw)}, "
                "which JavaScript cannot pass (it calls tools positionally)"
            )
        checked[name] = func
    return checked


# ---------------------------------------------------------------------------
# signing and parsing the journal
# ---------------------------------------------------------------------------


def _bound(associated_data: bytes) -> bytes:
    if not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data must be bytes")
    if len(associated_data) > _MAX_ASSOCIATED_DATA:
        raise ValueError(f"associated_data is limited to {_MAX_ASSOCIATED_DATA} bytes")
    # The length goes in first, so no payload can be read as associated data or the reverse.
    return len(associated_data).to_bytes(4, "big") + bytes(associated_data)


def _seal(payload: bytes, key: bytes, associated_data: bytes = b"") -> bytes:
    mac = hmac.new(
        _checked_key(key), _MAGIC + _bound(associated_data) + payload, hashlib.sha256
    ).digest()
    return _MAGIC + mac + payload


def _open(blob: bytes, key: bytes, associated_data: bytes = b"") -> bytes:
    secret = _checked_key(key)
    if len(blob) < len(_MAGIC) + _MAC_LEN or not blob.startswith(_MAGIC):
        raise JournalError("not a signed pydeno agent journal")
    mac = blob[len(_MAGIC) : len(_MAGIC) + _MAC_LEN]
    payload = blob[len(_MAGIC) + _MAC_LEN :]
    expected = hmac.new(
        secret, _MAGIC + _bound(associated_data) + payload, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(mac, expected):
        raise JournalError(
            "journal authentication failed (tampered, signed with a different key, or loaded "
            "with different associated_data)"
        )
    return payload


def _parse(payload: bytes) -> dict[str, Any]:
    """Validate the shape of an authenticated journal. Authentic means we wrote it, but a key
    shared by two versions of a program is not proof the shape is what this one expects."""
    try:
        journal = json.loads(payload)
    except (ValueError, RecursionError):
        raise JournalError("journal is not valid JSON") from None

    def bad(what: str) -> JournalError:
        return JournalError(f"malformed journal: {what}")

    if not isinstance(journal, dict) or journal.get("format") != _JOURNAL_FORMAT:
        raise bad("unknown format")
    config, records = journal.get("config"), journal.get("records")
    if not isinstance(config, dict) or not isinstance(records, list):
        raise bad("missing config or records")

    def plain_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    clock, seed = config.get("clock_ms"), config.get("random_seed")
    if (
        not plain_int(clock) or abs(clock) > 8_640_000_000_000_000
    ):  # JavaScript's Date range
        raise bad("clock")
    if not plain_int(seed) or not 0 <= seed < 2**31:
        raise bad("seed")
    budget = config.get("max_tool_calls")
    if budget is not None and (not plain_int(budget) or budget < 0):
        raise bad("max_tool_calls")
    if not isinstance(config.get("release"), str) or not isinstance(
        config.get("redact"), bool
    ):
        raise bad("release or redact")
    namespace = config.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        raise bad("namespace")
    names = config.get("tools")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise bad("tools")
    for record in records:
        ok = (
            isinstance(record, list)
            and record
            and (
                (record[0] == "run" and len(record) == 2 and isinstance(record[1], str))
                or (
                    record[0] == "obs"
                    and len(record) == 3
                    and record[1] in ("call", "done", "failed")
                    and isinstance(record[2], str)
                )
                or (record[0] == "ans" and len(record) == 3 and record[1] == "v")
                or (
                    record[0] == "ans"
                    and len(record) == 4
                    and record[1] == "e"
                    and isinstance(record[2], str)
                    and isinstance(record[3], str)
                )
            )
        )
        if not ok:
            raise bad("record")
    return journal
