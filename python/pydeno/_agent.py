"""`AgentSandbox`: a stateful JavaScript sandbox for AI agents, with pause/resume at tool calls.

What pydantic/monty offers an agent that writes Python, for an agent that writes JavaScript, built
only on `IsolatedRuntime`'s public API:

- **Tools** from a plain ``name -> callable`` mapping, or described by JSON Schema
  (`SchemaTool`, one object argument), checked like `ToolBridge` names, with one call budget for
  the whole session. A lazy catalog (``tools_catalog=``) declares only ``search_tools`` and
  ``describe_tool``; the host decides which catalog tools the guest has found and may call.
- **Prompt helpers**: `describe_tools()` (a block for the system prompt) and `typescript_stubs()`
  (a ``.d.ts`` the model can be shown, and its code checked against).
- **Results**: `execute()` (and every `Done`/`Failed`) gives an `ExecutionResult` with the run's
  console output and a JSON result, both bounded, and a stable `error_type`.
- **Session state**: one worker per session, so globals and functions survive between runs.
- **Pause/resume** (Monty's ``FunctionSnapshot``): `start()` stops at every tool call and hands it
  to the caller, who answers with `resume()`; `run()` answers them with the real tools.
- **Durability by deterministic replay**: V8 cannot serialise a half-run isolate, so a session is
  persisted as a signed journal of its inputs (code and tool results) plus a hash of every outcome.
  `AgentSandbox.load()` replays it on a fresh worker with the same frozen clock and random seed,
  substituting the recorded tool results, and raises `ReplayDivergence` if any outcome differs.
  After a run kills the worker, `dump()` returns the journal as of the last good run.

Everything the guest sends (tool names it calls, their arguments, its results) is untrusted data.
The tools run with the host's full authority; validate their arguments.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import collections.abc
import dataclasses
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

from ._isolated import _CONFIG_KEYS, IsolatedRuntime
from ._pydeno import JsUndefined, RuntimeConfig, undefined
from ._result import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    ExecutionResult,
    OutputCapture,
    ResultTooLarge,
    bounded_result,
    check_limit,
    error_type,
    failed_result,
    to_jsonable,
)
from ._schema import (
    _JS_RESERVED,
    SchemaTool,
    _union,
    as_schema_tool,
    schema_declarations,
)
from ._snapshot_auth import _engine_version
from ._snapshot_auth import _key as _checked_key
from ._tools import ToolBridge, ToolBudgetError, ToolError, ToolNotFoundError

__all__ = [
    "AgentSandbox",
    "Done",
    "ExecutionResult",
    "Failed",
    "JournalError",
    "ReplayDivergence",
    "ResultTooLarge",
    "SchemaTool",
    "Step",
    "ToolCall",
    "ToolNotDiscoveredError",
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
# Owned by the session because replay depends on them, or because the pause model needs them.
_OWNED_OPTIONS = frozenset(
    {"clock", "random_seed", "request_timeout", "max_host_wait", "capture_console"}
)
# The lazy catalog: the two tools declared up front, and the hidden host function every catalog
# tool is called through (one capability for the whole catalog, whatever its size).
_SEARCH = "search_tools"
_DESCRIBE = "describe_tool"
_CATALOG_CALL = "__pydeno_agent_catalog"
_MAX_SEARCH_RESULTS = 50
_DEFAULT_SEARCH_RESULTS = 10
_MAX_QUERY_CHARS = 1000
_SUMMARY_CHARS = 200
_MAX_LOST_CALLS = 2**53


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
    """The run finished; `value` is its result (`pydeno.undefined` when it had none).

    `stdout`/`stderr` are the run's console output (``log``/``info``/``debug`` and
    ``warn``/``error``/``trace``), each capped at the session's ``max_output_bytes``;
    `truncated` says one was cut. They are not part of equality: ``Done(3) == step`` compares
    the value only."""

    value: Any
    stdout: str = field(default="", compare=False)
    stderr: str = field(default="", compare=False)
    truncated: bool = field(default=False, compare=False)

    status = "Succeeded"
    error_type = None

    @property
    def result(self) -> Any:
        """`value` as plain JSON data (see `ExecutionResult.result`)."""
        return to_jsonable(self.value)

    def to_result(
        self, *, max_error_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    ) -> ExecutionResult:
        """This step as an `ExecutionResult` (`max_error_bytes` is for `Failed`'s message)."""
        del max_error_bytes
        return ExecutionResult(
            status="Succeeded",
            stdout=self.stdout,
            stderr=self.stderr,
            result=self.result,
            truncated=self.truncated,
        )


@dataclass(frozen=True)
class Failed:
    """The run failed. A JavaScript error (and a result over ``max_result_bytes``) leaves the
    session usable; a crash, a hard timeout or a memory kill closes it
    (`AgentSandbox.is_closed()`). Carries the run's console output like `Done`."""

    error: BaseException
    stdout: str = field(default="", compare=False)
    stderr: str = field(default="", compare=False)
    truncated: bool = field(default=False, compare=False)

    status = "Failed"
    result = None

    @property
    def error_type(self) -> str:
        """A stable name for the failure (see `ExecutionResult.error_type`)."""
        return error_type(self.error)

    def to_result(
        self, *, max_error_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    ) -> ExecutionResult:
        return failed_result(
            self.error,
            stdout=self.stdout,
            stderr=self.stderr,
            truncated=self.truncated,
            max_error_bytes=max_error_bytes,
        )


Step = ToolCall | Done | Failed


class ReplayDivergence(RuntimeError):
    """Replaying a journal produced a different outcome than the one recorded."""


class JournalError(ValueError):
    """A journal is too large, malformed, or not authentic (tampered or wrongly keyed)."""


class ToolNotDiscoveredError(ToolError):
    """The guest called a catalog tool it has not found yet (or one that does not exist).

    With ``tools_catalog=``, only ``search_tools(query)`` and ``describe_tool(name)`` are declared
    up front; a catalog tool becomes callable once one of them has returned it to the guest. The
    guest sees an Error with this ``name`` and a message telling it to search first (never
    redacted: pydeno wrote it, and it holds nothing of the host's)."""


def _public(exc: Exception) -> Exception:
    """Mark an error written by pydeno itself (no host data in it) as shown to the guest even
    with ``redact_host_errors``: the catalog tools' usage errors are guidance for the model."""
    exc._pydeno_public = True  # type: ignore[attr-defined]
    return exc


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


_PREAMBLE = """\
You can run JavaScript in a sandbox. Write the code as the body of an async function:
call tools with `await` and `return` the final result. Top-level `const`, `let`, `var`,
`function` and `class` declarations written at the start of a line are kept for later
runs; to keep anything else, store it on `globalThis`. There is no network, filesystem,
`require` or `import`. `Date.now()` is frozen and `Math.random()` is seeded. A tool that
fails throws an Error whose `name` is the failure's type.
"""


def _schema_placeholder(schema: Any, name: str) -> str:
    if not isinstance(schema, Mapping):
        return "null"
    if "const" in schema:
        return json.dumps(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return json.dumps(enum[0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    return {
        "string": json.dumps(name),
        "integer": "1",
        "number": "1",
        "boolean": "true",
        "array": "[]",
        "object": "{}",
        "null": "null",
    }.get(kind, "null")  # type: ignore[arg-type]


def _schema_example(tool: SchemaTool, prefix: str) -> str:
    props = tool.input_schema.get("properties")
    required = tool.input_schema.get("required")
    fields = []
    if isinstance(props, Mapping) and isinstance(required, list):
        for key in required:
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z_$][\w$]*", key):
                fields.append(f"{key}: {_schema_placeholder(props.get(key), key)}")
    args = "{ " + ", ".join(fields) + " }" if fields else "{}"
    return f"const result = await {prefix}{tool.name}({args});"


_CATALOG_GUIDE = """\
More tools are in a catalog and are not declared here. Find them with `search_tools(query)`
(returns `[{{name, description}}]`), read one with `describe_tool(name)` (returns its JSON
Schemas and a TypeScript declaration), then call it as `await {ns}.<name>({{...}})` with ONE
object argument. A tool you have not found with `search_tools` or `describe_tool` in this
session throws a `ToolNotDiscoveredError`: search first."""


def describe_tools(
    tools: Mapping[str, Any] | collections.abc.Sequence[Any],
    *,
    namespace: str | None = None,
) -> str:
    """A block for an LLM's system prompt: how code runs, then each tool's signature, docstring
    and an example call. The signatures are the ones `typescript_stubs` declares.

    `tools` maps names to callables (described from their signatures) or to `SchemaTool`s
    (described from their JSON Schemas); a sequence of `SchemaTool`s works too."""
    entries = _normalize_tools(tools)
    prefix = f"{namespace}." if namespace else ""
    where = f"on the `{namespace}` object" if namespace else "as global functions"
    lines = [
        _PREAMBLE,
        f"These tools are available {where}; each returns a Promise.",
        "",
    ]
    decls, functions = schema_declarations(
        [t for t in entries.values() if isinstance(t, SchemaTool)], declare=False
    )
    if decls:
        lines += ["Types used by the tools below:", *decls, ""]
    for name, tool in entries.items():
        if isinstance(tool, SchemaTool):
            lines.append(prefix + functions[name][1])
            doc, example = tool.description, _schema_example(tool, prefix)
        else:
            spec = _spec(name, tool)
            lines.append(prefix + spec.signature())
            doc, example = spec.doc, spec.example(prefix)
        for doc_line in doc.splitlines():
            lines.append(f"    {doc_line}".rstrip())
        lines.append(f"    Example: {example}")
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
    tools: Mapping[str, Any] | collections.abc.Sequence[Any],
    *,
    namespace: str | None = None,
) -> str:
    """A `.d.ts` declaring the tools.

    A callable is declared from its Python signature, annotations and docstring: `int`/`float`
    become `number`, `str` `string`, `bool` `boolean`, `bytes` `Uint8Array`, `list[T]` `T[]`,
    `dict[str, T]` `Record<string, T>`, `X | None` `X | null`; a parameter with a default is
    optional; anything else is `unknown`.

    A `SchemaTool` is declared from its JSON Schemas as a function of ONE object argument:
    objects (required and optional properties, ``additionalProperties`` index signatures),
    arrays and tuples, ``enum``/``const`` literals, ``anyOf``/``oneOf`` unions, ``allOf``
    intersections, ``nullable``/``type: [..., "null"]``, and ``$ref`` to ``$defs`` (nested and
    recursive ones too) as named ``interface``/``type`` declarations. Its ``output_schema`` is
    the resolved type (``unknown`` without one). Every tool returns a `Promise`."""
    entries = _normalize_tools(tools)
    out = ["// Tools provided by the host. Every call returns a Promise.", ""]
    indent = "  " if namespace else ""
    decls, functions = schema_declarations(
        [t for t in entries.values() if isinstance(t, SchemaTool)],
        declare=not namespace,
        indent=indent,
    )
    if namespace:
        out.append(f"declare namespace {namespace} {{")
    if decls:
        out.extend(decls)
        out.append("")
    keyword = "function" if namespace else "declare function"
    for name, tool in entries.items():
        if isinstance(tool, SchemaTool):
            doc_lines, signature = functions[name]
            out.extend(doc_lines)
        else:
            spec = _spec(name, tool)
            out.extend(_jsdoc(spec.doc, indent))
            signature = spec.signature()
        out.append(f"{indent}{keyword} {signature};")
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


class _ConsoleSink:
    """The session's `on_console`: the capture of the run in flight, then the caller's own
    `on_console` (if the `RuntimeConfig` they passed had one)."""

    __slots__ = ("capture", "user")

    def __init__(self, user: Callable[..., Any] | None) -> None:
        self.capture: OutputCapture | None = None
        self.user = user

    def __call__(self, level: str, args: list[Any]) -> None:
        capture = self.capture
        if capture is not None:
            capture(level, args)
        if self.user is not None:
            self.user(level, args)


class _Core:
    """Everything the loop thread touches. It never refers to the `AgentSandbox`, so a session
    the caller drops can be collected, and its finalizer can shut this down."""

    def __init__(
        self,
        rt: IsolatedRuntime,
        session_id: int,
        max_tool_calls: int | None,
        *,
        console: _ConsoleSink,
        max_output_bytes: int,
        max_result_bytes: int,
        catalog: frozenset[str] = frozenset(),
    ) -> None:
        self.rt = rt
        self.session_id = session_id
        self.max_tool_calls = max_tool_calls
        self.console = console
        self.max_output_bytes = max_output_bytes
        self.max_result_bytes = max_result_bytes
        self.catalog = catalog
        # Catalog tools a `search_tools`/`describe_tool` answer has shown the guest. Added on the
        # caller's thread before that answer is delivered, read on the loop thread.
        self.discovered: set[str] = set()
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

    async def on_catalog_call(self, name: Any, args: list[Any]) -> Any:
        """A catalog tool, called through the one hidden dispatcher. Refused, without charging
        the budget, unless it is a catalog tool the guest has already found."""
        if (
            not isinstance(name, str)
            or name not in self.catalog
            or name not in self.discovered
        ):
            shown = name if isinstance(name, str) and len(name) <= 64 else "?"
            raise _public(
                ToolNotDiscoveredError(
                    f"no tool {shown!r} has been found in this session: call "
                    "search_tools(query) to find tools and describe_tool(name) to see how "
                    "to call one, then call it"
                )
            )
        return await self.on_tool_call(name, args)

    async def execute(self, run: _Run, code: str) -> None:
        capture = OutputCapture(self.max_output_bytes)
        self.console.capture = capture
        try:
            value = await self.rt.eval_async(_wrap(code))
            # Over the cap, the run fails but the session goes on (the value is dropped here).
            bounded_result(value, self.max_result_bytes)
            final: Step = Done(value)
        except BaseException as exc:  # noqa: BLE001 - every failure is the run's outcome
            final = Failed(exc)
        finally:
            # Every console call of the command was answered before its result arrived.
            self.console.capture = None
        final = dataclasses.replace(
            final,
            stdout=capture.stdout,
            stderr=capture.stderr,
            truncated=capture.truncated,
        )
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


class _SessionBase:
    """What `AgentSandbox` and `AsyncAgentSandbox` share: configuration, the journal (with its
    checkpoint and ``lost`` records), answer checking, outcome bookkeeping, the prompt helpers and
    the load checks. No I/O happens here; a subclass drives its worker (a loop thread, or the
    caller's event loop) and provides `_core` with ``calls_made``, ``discovered``, ``closed``,
    ``rt.is_closed()`` and ``shutdown()``."""

    _core: Any
    _tools: dict[str, Any]
    _catalog: dict[str, SchemaTool]

    def _configure(
        self,
        tools: Mapping[str, Any] | collections.abc.Sequence[Any],
        *,
        max_tool_calls: int | None,
        namespace: str | None,
        tools_catalog: Mapping[str, Any] | collections.abc.Sequence[Any] | None,
        clock: datetime | float | int | None,
        random_seed: int | None,
        max_journal_bytes: int,
        max_output_bytes: int,
        max_result_bytes: int,
        runtime_options: dict[str, Any],
    ) -> tuple[RuntimeConfig, _ConsoleSink]:
        """Validate the arguments and set up the session's state. Pops ``config`` from
        `runtime_options`; returns the runtime config to start the worker with (its console goes
        to the returned sink) ."""
        who = type(self).__name__
        entries = _normalize_tools(tools)
        catalog = _normalize_catalog(tools_catalog)
        if max_tool_calls is not None and (
            not isinstance(max_tool_calls, int)
            or isinstance(max_tool_calls, bool)
            or max_tool_calls < 0
        ):
            raise ValueError("max_tool_calls must be a non-negative int or None")
        if namespace is not None:
            ToolBridge._check_name(namespace, what="namespace")  # noqa: SLF001
        check_limit("max_output_bytes", max_output_bytes)
        check_limit("max_result_bytes", max_result_bytes)
        catalog_ns = namespace or "tools"
        if catalog:
            reserved = {_SEARCH, _DESCRIBE} & entries.keys()
            if reserved:
                raise ValueError(
                    f"with tools_catalog=, {sorted(reserved)} are the session's own tools; "
                    "rename yours"
                )
            overlap = sorted(entries.keys() & catalog.keys())
            if overlap:
                raise ValueError(
                    f"tools {overlap} are both declared and in the catalog"
                )
            if namespace is None and catalog_ns in entries:
                raise ValueError(
                    f"catalog tools are called as {catalog_ns}.<name>(...), and a tool named "
                    f"{catalog_ns!r} would hide them; rename it or pass namespace="
                )
            entries[_SEARCH] = _search_tool(catalog)
            entries[_DESCRIBE] = _describe_tool(catalog, catalog_ns)
        owned = _OWNED_OPTIONS & runtime_options.keys()
        if owned:
            raise TypeError(
                f"{who} sets {sorted(owned)} itself (use clock=, random_seed=, "
                "timeout=, max_pause=)"
            )
        config = runtime_options.pop("config", None)
        if config is None:
            config = RuntimeConfig()
        if not isinstance(config, RuntimeConfig):
            raise TypeError("config must be a pydeno.RuntimeConfig")
        if config.timeout is not None:
            raise ValueError(
                f"RuntimeConfig.timeout is not supported by {who}: it would count the time "
                f"a run is paused at a tool call. Use {who}(timeout=...)."
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

        self._tools = entries
        self._catalog = catalog
        self._catalog_ns = catalog_ns
        self._namespace = namespace
        self._max_tool_calls = max_tool_calls
        self._redact = bool(runtime_options.get("redact_host_errors", True))
        self._clock_ms = clock_ms
        self._random_seed = random_seed
        self._max_journal_bytes = max_journal_bytes
        self._max_output_bytes = max_output_bytes
        self._max_result_bytes = max_result_bytes
        self._records: list[list[Any]] | None = []
        self._journal_size = 0
        # The journal as of the last run that ended with the worker alive: its length, and the
        # tool calls made by then. A run that kills the worker is cut off here by `dump()`.
        self._checkpoint = 0
        self._checkpoint_calls = 0
        self._lost: list[Any] | None = None
        self._lost_runs = 0
        self._dead = False
        self._paused: ToolCall | None = None
        self._run: _Run | None = None

        # Console output is collected per run; the caller's own `on_console` still sees it all.
        sink = _ConsoleSink(config.on_console)
        rt_config = RuntimeConfig(
            **{key: getattr(config, key) for key in _CONFIG_KEYS},
            on_console=sink,
            inspector=config.inspector,
            snapshot=config.snapshot,
        )
        return rt_config, sink

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

    @property
    def catalog_names(self) -> tuple[str, ...]:
        """The names in ``tools_catalog`` (not declared to the guest up front)."""
        return tuple(self._catalog)

    @property
    def discovered_tools(self) -> frozenset[str]:
        """Catalog tools the guest has found, and so may call."""
        return frozenset(self._core.discovered)

    @property
    def lost_runs(self) -> int:
        """Runs this session's history dropped because the worker died during them (see
        `dump`), including ones recorded in the journal it was loaded from."""
        return self._lost_runs

    def is_closed(self) -> bool:
        return self._core.closed or self._dead

    def describe_tools(self) -> str:
        """See the module-level `describe_tools`. With a catalog, the declared part is the same
        whatever the catalog's size: its tools are not listed."""
        text = describe_tools(self._tools, namespace=self._namespace)
        if self._catalog:
            text += "\n" + _CATALOG_GUIDE.format(ns=self._catalog_ns) + "\n"
        return text

    def typescript_stubs(self) -> str:
        """See the module-level `typescript_stubs`. Catalog tools are not declared (the guest
        reads one's declaration with `describe_tool`)."""
        text = typescript_stubs(self._tools, namespace=self._namespace)
        if self._catalog:
            text += (
                "\n"
                + "\n".join(
                    "// " + line
                    for line in _CATALOG_GUIDE.format(ns=self._catalog_ns).splitlines()
                )
                + "\n"
            )
        return text

    # -- shared steps --------------------------------------------------------

    def _check_usable(self) -> None:
        if self._dead:
            raise RuntimeError(
                "the session's worker is gone (crashed, killed or timed out)"
            )
        if self._core.closed:
            raise RuntimeError("the session is closed")

    def _check_call(self, step: Any) -> tuple[Callable[..., Any], tuple[Any, ...]]:
        """The callable and arguments the real tool for `step` is called with (`call`)."""
        if not isinstance(step, ToolCall) or step.name not in self._tools.keys() | set(
            self._catalog
        ):
            raise TypeError("call() takes a ToolCall from this session")
        tool = self._tools.get(step.name) or self._catalog[step.name]
        if isinstance(tool, SchemaTool):
            return tool.callable, (_schema_argument(step),)
        return tool, step.args

    @staticmethod
    def _check_result(call: ToolCall, result: Any) -> Any:
        try:
            _encode(result)
        except TypeError as exc:
            raise TypeError(
                f"tool {call.name!r} returned a value the sandbox cannot hold"
            ) from exc
        return result

    def _answer(
        self, step: ToolCall, value: Any, error: BaseException | None
    ) -> tuple[Any, BaseException | None]:
        """Check an answer to the paused call and record it; returns what to send (a value, or
        an error), exactly what a replay of the record will send."""
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
        sent_value: Any = None
        sent: BaseException | None = None
        if error is not None:
            if not isinstance(error, Exception):
                raise TypeError("error must be an Exception instance")
            name = type(error).__name__
            public = getattr(error, "_pydeno_public", False) is True
            message = (
                "host function failed" if self._redact and not public else str(error)
            )
            record = ["ans", "e", name, message]
            sent = _error_class(name)(message)
            # Redaction was decided just above (and recorded); the runtime must not redo it.
            sent._pydeno_public = True  # type: ignore[attr-defined]
        else:
            # A TypeError here is the caller's to fix; nothing has been answered yet.
            encoded = _encode(value)
            record = ["ans", "v", encoded]
            sent_value = _decode(encoded)  # exactly what a replay will send
            if self._catalog and step.name in (_SEARCH, _DESCRIBE):
                # Before the answer is delivered, so the guest can call what it just found. From
                # the answer itself (live and on replay alike), not from who produced it.
                self._core.discovered.update(_found(sent_value, self._catalog))
        self._record(record)
        self._paused = None
        return sent_value, sent

    def _observe(self, step: Step) -> Step:
        kind, digest = _outcome(step)
        self._record(["obs", kind, digest])
        if isinstance(step, ToolCall):
            self._paused = step
        else:
            self._paused = None
            if self._core.rt.is_closed():
                # The worker is gone (crash, hard timeout, memory kill, `max_pause`): nothing
                # more can run, so give its resources back now rather than at `close()`.
                # `dump()` cuts this run off at the last checkpoint, keeping what it spent.
                self._mark_dead(_lost_reason(step))
                self._core.shutdown()
            elif self._records is not None:
                self._checkpoint = len(self._records)
                self._checkpoint_calls = self._core.calls_made
        return step

    def _mark_dead(self, reason: str | None) -> None:
        """The worker is gone. With a `reason`, a run was in progress: `dump()` replaces it with a
        ``lost`` record carrying the tool calls it made (dying refunds nothing)."""
        if self._dead:
            return
        self._dead = True
        self._paused = None
        if reason is not None:
            self._lost = [
                "lost",
                min(self._core.calls_made - self._checkpoint_calls, _MAX_LOST_CALLS),
                reason if _SAFE_ERROR_NAME.fullmatch(reason) else "Error",
            ]

    def _record(self, record: list[Any]) -> None:
        if self._records is None:
            return
        self._journal_size += len(json.dumps(record, separators=(",", ":")))
        if self._journal_size > self._max_journal_bytes:
            self._records = None  # free it; `dump` explains
            return
        self._records.append(record)

    def _replay_lost(self, record: list[Any]) -> None:
        """A run the worker died in, left out of the journal: only what it spent of the tool
        budget is carried over."""
        self._core.calls_made += record[1]
        self._lost_runs += 1
        self._record(record)
        if self._records is not None:
            self._checkpoint = len(self._records)
            self._checkpoint_calls = self._core.calls_made

    # -- durability ----------------------------------------------------------

    def _journal(self) -> tuple[dict[str, Any], list[list[Any]]]:
        """The config and records `dump()` signs (see `AgentSandbox.dump`)."""
        if self._records is None:
            raise JournalError(
                f"the journal grew past max_journal_bytes={self._max_journal_bytes}"
            )
        records = self._records
        if self._dead:
            records = records[: self._checkpoint] + (
                [self._lost] if self._lost is not None else []
            )
        config: dict[str, Any] = {
            "clock_ms": self._clock_ms,
            "random_seed": self._random_seed,
            "max_tool_calls": self._max_tool_calls,
            "namespace": self._namespace,
            "tools": list(self._tools),
            "release": _engine_version().decode(errors="replace"),
            "redact": self._redact,
        }
        # Only when they differ from what a journal without them means, so a session that
        # uses neither writes exactly the journal it always did.
        if self._max_result_bytes != DEFAULT_MAX_RESULT_BYTES:
            config["max_result_bytes"] = self._max_result_bytes
        if self._catalog:
            config["catalog"] = list(self._catalog)
        return config, list(records)

    @staticmethod
    def _load_arguments(
        journal: dict[str, Any],
        tools: Mapping[str, Any] | collections.abc.Sequence[Any],
        tools_catalog: Mapping[str, Any] | collections.abc.Sequence[Any] | None,
        max_journal_bytes: int,
        options: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """`load`'s checks, before any worker starts: (tools, constructor keyword arguments)."""
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
        entries = _normalize_tools(tools)
        catalog = _normalize_catalog(tools_catalog)
        names = list(entries) + ([_SEARCH, _DESCRIBE] if catalog else [])
        if names != config["tools"]:
            raise JournalError(
                f"the journal was recorded with tools {config['tools']}, not {names}"
            )
        if list(catalog) != config.get("catalog", []):
            raise JournalError(
                "the journal was recorded with a different tools_catalog "
                f"({len(config.get('catalog', []))} tools, not {len(catalog)})"
            )
        for owned in (
            "clock",
            "random_seed",
            "max_tool_calls",
            "namespace",
            "max_result_bytes",
        ):
            if owned in options:
                raise TypeError(f"{owned} comes from the journal")
        return entries, {
            "max_tool_calls": config["max_tool_calls"],
            "namespace": config["namespace"],
            "tools_catalog": catalog or None,
            "clock": config["clock_ms"] / 1000,
            "random_seed": config["random_seed"],
            "max_journal_bytes": max_journal_bytes,
            "max_result_bytes": config.get(
                "max_result_bytes", DEFAULT_MAX_RESULT_BYTES
            ),
        }


class AgentSandbox(_SessionBase):
    """A stateful, pausable JavaScript session for an AI agent, in an `IsolatedRuntime`.

    Args:
        tools: ``name -> callable`` (sync or async), called with the guest's positional
            arguments; or ``name -> SchemaTool`` (or a mapping with its keys, or a list of
            them), a tool described by JSON Schema that takes ONE object argument and whose
            callable gets it as a ``dict``. Names follow `ToolBridge`'s rules. Every call
            returns a Promise in the guest.
        max_tool_calls: Total tool calls the guest may make over the session's life (across
            every `start`, `resume` and `run`); further calls throw a ``ToolBudgetError`` in the
            guest. ``None``: unlimited. For a budget per tool, count inside the tool (see
            ``docs/guides/agent-sessions.md``).
        namespace: Install the tools on this global object (``tools.search(...)``) instead of
            as bare globals.
        tools_catalog: Many more `SchemaTool`s, declared lazily: only ``search_tools(query,
            limit?)`` and ``describe_tool(name)`` are added to the declared tools, whatever the
            catalog's size, and a catalog tool is called as ``<namespace or "tools">.<name>(args)``
            once one of those two has returned it to the guest. Calling one before throws a
            `ToolNotDiscoveredError` telling the guest to search first. Catalog calls are tool
            calls like any other (`ToolCall` steps, the same budget).
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
        max_output_bytes: Cap on each of a run's `stdout` and `stderr` (console output, carried
            by `Done`/`Failed` and `execute()`), in UTF-8 bytes; past it the stream ends with a
            ``[truncated]`` line. Default 64 KiB.
        max_result_bytes: Cap on a run's result as compact JSON. A larger result makes the run
            `Failed` with ``error_type == "ResultTooLarge"``; the session stays usable. Default
            1 MiB. Recorded in the journal (it decides outcomes, so replay needs the same one).
        **runtime_options: Passed to `IsolatedRuntime` (``config``, ``max_memory``, ``sandbox``,
            ``redact_host_errors``, ...). ``config.timeout`` is refused: a soft timeout would also
            count the time paused at a tool call. ``config.on_console`` still gets every console
            call (each one is a host call, counted by ``max_host_calls``).
    """

    def __init__(
        self,
        tools: Mapping[str, Any] | collections.abc.Sequence[Any],
        *,
        max_tool_calls: int | None = None,
        namespace: str | None = None,
        tools_catalog: Mapping[str, Any] | collections.abc.Sequence[Any] | None = None,
        clock: datetime | float | int | None = None,
        random_seed: int | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        max_pause: float | None = DEFAULT_MAX_PAUSE,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        **runtime_options: Any,
    ) -> None:
        rt_config, sink = self._configure(
            tools,
            max_tool_calls=max_tool_calls,
            namespace=namespace,
            tools_catalog=tools_catalog,
            clock=clock,
            random_seed=random_seed,
            max_journal_bytes=max_journal_bytes,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
            runtime_options=runtime_options,
        )
        self._lock = threading.Lock()
        rt = IsolatedRuntime(
            rt_config,
            clock=self._clock_ms / 1000,
            random_seed=self._random_seed,
            request_timeout=timeout,
            max_host_wait=max_pause,
            **runtime_options,
        )
        try:
            self._core = _Core(
                rt,
                next(_SESSION_IDS),
                max_tool_calls,
                console=sink,
                max_output_bytes=max_output_bytes,
                max_result_bytes=max_result_bytes,
                catalog=frozenset(self._catalog),
            )
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
        if self._catalog:

            async def catalog_call(name: Any = None, *args: Any) -> Any:
                return await core.on_catalog_call(name, list(args))

            core.rt.bind_function(_CATALOG_CALL, catalog_call)
        core.rt.eval(
            _prelude(
                list(self._tools),
                self._namespace,
                self._catalog_ns if self._catalog else None,
            )
        )

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
        step = self._drive(code)
        if isinstance(step, Failed):
            raise step.error
        return step.value

    def execute(self, code: str) -> ExecutionResult:
        """`run` the code, but return an `ExecutionResult` instead of raising.

        ``{status, stdout, stderr, result, error, error_type, truncated}``: the run's console
        output (each stream capped at ``max_output_bytes``), its result as JSON data (capped at
        ``max_result_bytes``: over it, ``status="Failed"``, ``error_type="ResultTooLarge"``),
        and for a failure a stable `error_type` (the guest's error ``name``, or the host's
        exception class). A run that kills the worker is a ``Failed`` result too; calling this
        on a closed session (or while paused) still raises, as `run` does."""
        return self._drive(code).to_result(max_error_bytes=self._max_output_bytes)

    def _drive(self, code: str) -> Done | Failed:
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
        return step

    def call(self, step: ToolCall) -> Any:
        """Run the real tool for a `ToolCall` (what `run` does for each one) and return its
        result, or raise what it raised; answer the guest with `resume`. For a driver of
        `start`/`resume` that wants some calls (``search_tools``, say) handled as usual."""
        self._check_call(step)
        if threading.current_thread() is self._core.thread:
            raise RuntimeError(
                "an AgentSandbox cannot be driven from one of its own tools"
            )
        return self._call_tool(step)

    def _call_tool(self, call: ToolCall) -> Any:
        fn, args = self._check_call(call)
        result = fn(*args)
        if inspect.isawaitable(result):

            async def wait() -> Any:
                return await result

            result = self._core.on_loop(wait())
        return self._check_result(call, result)

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
        sent_value, sent = self._answer(step, value, error)
        run = self._run
        assert run is not None
        self._core.call_on_loop(self._core.answer, run, step.call_id, sent_value, sent)
        return self._observe(self._core.next_step(run))

    # -- durability ----------------------------------------------------------

    def dump(self, key: bytes, *, associated_data: bytes = b"") -> bytes:
        """The session's journal, HMAC-SHA256-signed with `key` (at least 16 bytes).

        It holds every run's code and every tool answer (errors as their class name, and their
        message only with ``redact_host_errors=False``), plus a hash of every outcome. It is
        signed, not encrypted. Restore with `AgentSandbox.load`.

        `associated_data` (for example a tenant or session id) is folded into the signature but not
        stored: `load` must be given the same bytes, so a journal cannot be loaded as another tenant's.
        A journal alone cannot stop *rollback* (loading an older dump of the same session restores
        its spent tool budget): keep a counter in your own store and put it in `associated_data`.

        After the worker died (a crash, a hard timeout, a memory kill, ``max_pause``) the journal
        is the one as of the last run that ended with the worker alive: the run that killed it
        is left out (it is never replayed) and marked by a ``lost`` record, which also carries
        the tool calls it made, so a loaded session has spent them too (dying cannot refund the
        budget). `load` restores the state after the last good run. Tool calls that run made
        did run, with their side effects; a loaded session does not know about them."""
        self._enter()
        try:
            config, records = self._journal()
            return _seal_journal(config, records, key, associated_data)
        finally:
            self._lock.release()

    @classmethod
    def load(
        cls,
        blob: bytes,
        key: bytes,
        tools: Mapping[str, Any] | collections.abc.Sequence[Any],
        *,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        associated_data: bytes = b"",
        tools_catalog: Mapping[str, Any] | collections.abc.Sequence[Any] | None = None,
        **options: Any,
    ) -> AgentSandbox:
        """Rebuild a session from `dump()` output by replaying it on a fresh worker.

        The MAC is checked before anything runs. Recorded tool answers are replayed; the real
        tools are never called. If the session was dumped while paused at a tool call, the
        returned session is paused at the same call (`pending`). Pass the same tools and
        ``tools_catalog`` (by name) and the same runtime options as the original. Raises `JournalError` for a blob that is
        not authentic or not well-formed, and `ReplayDivergence` (closing the new session) if
        the guest does not behave exactly as recorded."""
        journal = _open_journal(blob, key, associated_data, max_journal_bytes)
        entries, arguments = cls._load_arguments(
            journal, tools, tools_catalog, max_journal_bytes, options
        )
        session = cls(entries, **arguments, **options)
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
        plan = _replay_plan(records)
        step: Step | None = None
        try:
            request = next(plan)
            while True:
                if request[0] == "lost":
                    self._replay_lost(request[1])
                    step = None
                elif request[0] == "run":
                    step = self._start(request[1])
                else:
                    step = self._resume(request[1], request[2], request[3])
                request = plan.send(step)  # type: ignore[arg-type]
        except StopIteration:
            return

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


def _replay_plan(
    records: list[list[Any]],
) -> collections.abc.Generator[tuple[Any, ...], Step | None, None]:
    """A journal's records as the inputs to replay, for any driver (a thread, an event loop).

    Yields ``("lost", record)`` (charge the lost run's tool calls; send None back),
    ``("run", code)`` or ``("ans", tool_call, value, error)`` (apply it; send back the step it
    produced). Checks the records' order and raises `ReplayDivergence` at the first outcome that
    differs from the recorded one."""
    step: Step | None = None
    # The input (a run's code, or a tool answer) that produces the next recorded outcome.
    pending: tuple[str, Any] | None = None
    for index, record in enumerate(records):
        op = record[0]
        if op == "lost":
            if pending is not None or isinstance(step, ToolCall):
                raise JournalError(
                    f"record {index}: a lost run in the middle of another one"
                )
            yield ("lost", record)
            step = None
            continue
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
                step = yield ("run", input_)
            else:
                assert isinstance(step, ToolCall)
                if input_[1] == "v":
                    step = yield ("ans", step, _decode(input_[2]), None)
                else:
                    # The recorded message is already the redacted one (or a public one): the
                    # replay must not redact it a second time, or it diverges from the live run.
                    recorded = _error_class(input_[2])(input_[3])
                    recorded._pydeno_public = True  # type: ignore[attr-defined]
                    step = yield ("ans", step, _MISSING, recorded)
            assert step is not None
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


def _seal_journal(
    config: dict[str, Any],
    records: list[list[Any]],
    key: bytes,
    associated_data: bytes,
) -> bytes:
    payload = json.dumps(
        {"format": _JOURNAL_FORMAT, "config": config, "records": records},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return _seal(payload, key, associated_data)


def _open_journal(
    blob: bytes, key: bytes, associated_data: bytes, max_journal_bytes: int
) -> dict[str, Any]:
    """Size-check, authenticate and parse a journal (before any worker starts)."""
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError("blob must be bytes")
    if len(blob) > max_journal_bytes * 2 + len(_MAGIC) + _MAC_LEN + 4096:
        raise JournalError("journal is larger than max_journal_bytes allows")
    return _parse(_open(bytes(blob), key, associated_data))


def _prelude(
    names: list[str], namespace: str | None, catalog_ns: str | None = None
) -> str:
    """Installed once, before any guest code: wraps each tool so the session knows which calls are
    in flight, and defines the settle step every run ends with.

    Why: a worker that finishes a command while one of its tool calls is unanswered breaks when
    that answer arrives later, so a run must not end with a call in flight (a call the code did
    not `await`, or one still running when `Promise.all` rejected). Intrinsics are captured first,
    so a guest that later replaces `Promise` or `Set` methods only breaks its own runs.

    With a catalog, `catalog_ns` becomes a Proxy whose unknown properties are functions calling
    the one hidden catalog dispatcher with their own name. The host decides what such a call may
    reach (`_Core.on_catalog_call`); the proxy only spells it `tools.name(args)`. No catalog name
    appears here, so this script is the same size whatever the catalog holds."""
    holder = f"globalThis[{json.dumps(namespace)}]" if namespace else "globalThis"
    return f"""(() => {{
  "use strict";
  const call = Function.prototype.call.bind.bind(Function.prototype.call);
  const apply = Reflect.apply;
  const has = Reflect.has;
  const get = Reflect.get;
  const then = call(Promise.prototype.then);
  const add = call(Set.prototype.add);
  const remove = call(Set.prototype.delete);
  const forEach = call(Set.prototype.forEach);
  const size = call(Object.getOwnPropertyDescriptor(Set.prototype, "size").get);
  const push = call(Array.prototype.push);
  const allSettled = Promise.allSettled.bind(Promise);
  const inflight = new Set();
  const track = (raw, name) => ({{ [name](...args) {{
    const p = apply(raw, undefined, args);
    add(inflight, p);
    const done = () => {{ remove(inflight, p); }};
    then(p, done, done);
    return p;
  }} }})[name];
  const holder = {holder};
  for (const name of {json.dumps(names)}) {{
    holder[name] = track(holder[name], name);
  }}
  const catalogNs = {json.dumps(catalog_ns)};
  if (catalogNs !== null) {{
    const dispatch = track(globalThis["{_CATALOG_CALL}"], "{_CATALOG_CALL}");
    try {{ delete globalThis["{_CATALOG_CALL}"]; }} catch {{}}
    let target = globalThis[catalogNs];
    if (typeof target !== "object" || target === null) target = {{}};
    globalThis[catalogNs] = new Proxy(target, {{
      get(t, key, receiver) {{
        if (typeof key !== "string" || key === "then" || has(t, key)) return get(t, key, receiver);
        return {{ [key](...args) {{ return dispatch(key, ...args); }} }}[key];
      }},
    }});
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


# Inherited by every object, so a catalog tool with one of these names would be unreachable through
# the proxy (it answers names the namespace object does not already have).
_OBJECT_MEMBERS = frozenset(
    {
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
        "isPrototypeOf",
        "propertyIsEnumerable",
        "toLocaleString",
        "then",
    }
)


def _normalize_tools(
    tools: Mapping[str, Any] | collections.abc.Sequence[Any],
) -> dict[str, Any]:
    """``name -> callable | SchemaTool``, checked. Accepts a mapping (values: callables,
    `SchemaTool`s or mappings describing one) or a sequence of schema tools."""
    if isinstance(tools, Mapping):
        items: list[tuple[str | None, Any]] = list(tools.items())
    elif isinstance(tools, collections.abc.Sequence) and not isinstance(
        tools, (str, bytes)
    ):
        items = [(None, tool) for tool in tools]
    else:
        raise TypeError(
            "tools must be a mapping of name -> callable or SchemaTool, or a list of SchemaTools"
        )
    checked: dict[str, Any] = {}
    for name, tool in items:
        if name is None or isinstance(tool, (SchemaTool, Mapping)):
            schema = as_schema_tool(tool, name)
            if schema.name in checked:
                raise ValueError(f"tool {schema.name!r} is given twice")
            checked[schema.name] = schema
            continue
        ToolBridge._check_name(name, what="tool name")  # noqa: SLF001
        if not callable(tool):
            raise TypeError(f"tool {name!r} is not callable")
        required_kw = _spec(name, tool).required_keyword_only
        if required_kw:
            raise ValueError(
                f"tool {name!r} has required keyword-only parameters {list(required_kw)}, "
                "which JavaScript cannot pass (it calls tools positionally)"
            )
        checked[name] = tool
    return checked


def _normalize_catalog(
    catalog: Mapping[str, Any] | collections.abc.Sequence[Any] | None,
) -> dict[str, SchemaTool]:
    if catalog is None:
        return {}
    entries = _normalize_tools(catalog)
    for name, tool in entries.items():
        if not isinstance(tool, SchemaTool):
            raise TypeError(
                f"catalog tool {name!r} must be a SchemaTool (or a mapping describing one): "
                "the catalog is searched by name and description and declared from its schema"
            )
        if name in _OBJECT_MEMBERS or name == _CATALOG_CALL:
            raise ValueError(f"{name!r} cannot be a catalog tool name")
    return entries  # type: ignore[return-value]


def _search_tool(catalog: Mapping[str, SchemaTool]) -> Callable[..., Any]:
    def search_tools(
        query: str, limit: int = _DEFAULT_SEARCH_RESULTS
    ) -> list[dict[str, str]]:
        """Search the tool catalog by keywords. Returns up to `limit` (at most 50) matches as
        `{name, description}`, best first. Found tools become callable in this session."""
        if not isinstance(query, str):
            raise _public(
                TypeError("search_tools(query, limit?): query must be a string")
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_SEARCH_RESULTS
        ):
            raise _public(
                TypeError(
                    f"search_tools(query, limit?): limit must be an integer from 1 to "
                    f"{_MAX_SEARCH_RESULTS}"
                )
            )
        terms = list(
            dict.fromkeys(re.findall(r"[a-z0-9]+", query[:_MAX_QUERY_CHARS].lower()))
        )[:32]
        scored: list[tuple[int, str]] = []
        for name, tool in catalog.items():
            lowered = name.lower()
            words = set(re.findall(r"[a-z0-9]+", lowered.replace("_", " ")))
            description = tool.description.lower()
            score = 0
            for term in terms:
                if term in words:
                    score += 4
                elif term in lowered:
                    score += 2
                if term in description:
                    score += 1
            if score or not terms:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"name": name, "description": catalog[name].description[:_SUMMARY_CHARS]}
            for _, name in scored[:limit]
        ]

    return search_tools


def _describe_tool(catalog: Mapping[str, SchemaTool], ns: str) -> Callable[..., Any]:
    def describe_tool(name: str) -> dict[str, Any]:
        """Describe one catalog tool: `{name, description, input_schema, output_schema,
        typescript, usage}`. The tool becomes callable in this session."""
        tool = catalog.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _public(
                ToolNotFoundError(
                    "no such tool in the catalog; search_tools(query) lists the ones that exist"
                )
            )
        decls, functions = schema_declarations([tool], declare=False, indent="  ")
        doc, signature = functions[tool.name]
        typescript = "\n".join(
            [
                f"declare namespace {ns} {{",
                *decls,
                *([""] if decls else []),
                *doc,
                f"  function {signature};",
                "}",
            ]
        )
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": json.loads(json.dumps(tool.input_schema)),
            "output_schema": None
            if tool.output_schema is None
            else json.loads(json.dumps(tool.output_schema)),
            "typescript": typescript,
            "usage": f"const result = await {ns}.{tool.name}({{ ... }});",
        }

    return describe_tool


def _found(answer: Any, catalog: Mapping[str, SchemaTool]) -> set[str]:
    """Catalog names in a `search_tools`/`describe_tool` answer as the guest receives it."""
    items = answer if isinstance(answer, list) else [answer]
    return {
        item["name"]
        for item in items[:_MAX_SEARCH_RESULTS]
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"] in catalog
    }


def _plain(value: Any, depth: int = 0) -> Any:
    """Guest data as a schema tool's callable gets it: `undefined` object properties dropped,
    other `undefined`s as `None`, sets as lists."""
    if depth > 200:
        raise ValueError("arguments are nested too deeply")
    if isinstance(value, dict):
        return {
            k: _plain(v, depth + 1)
            for k, v in value.items()
            if not isinstance(v, JsUndefined)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, JsUndefined):
        return None
    return value


def _schema_argument(call: ToolCall) -> dict[str, Any]:
    args = call.args
    if len(args) == 1 and isinstance(args[0], dict):
        return _plain(args[0])
    if not args or (
        len(args) == 1 and (args[0] is None or isinstance(args[0], JsUndefined))
    ):
        return {}
    raise _public(
        TypeError(
            f"{call.name} takes one object argument, like {call.name}({{field: value}}); got "
            + (f"{len(args)} arguments" if len(args) > 1 else type(args[0]).__name__)
        )
    )


def _lost_reason(step: Step) -> str:
    name = step.error_type if isinstance(step, Failed) else "WorkerCrashed"
    return name if _SAFE_ERROR_NAME.fullmatch(name) else "Error"


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
    result_cap = config.get("max_result_bytes", DEFAULT_MAX_RESULT_BYTES)
    if not plain_int(result_cap) or result_cap <= 0:
        raise bad("max_result_bytes")
    catalog = config.get("catalog", [])
    if not isinstance(catalog, list) or not all(isinstance(n, str) for n in catalog):
        raise bad("catalog")
    for record in records:
        ok = (
            isinstance(record, list)
            and record
            and (
                (record[0] == "run" and len(record) == 2 and isinstance(record[1], str))
                or (
                    record[0] == "lost"
                    and len(record) == 3
                    and plain_int(record[1])
                    and 0 <= record[1] <= _MAX_LOST_CALLS
                    and isinstance(record[2], str)
                    and _SAFE_ERROR_NAME.fullmatch(record[2]) is not None
                )
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
