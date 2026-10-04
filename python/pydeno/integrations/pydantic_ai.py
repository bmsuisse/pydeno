"""Code mode for pydantic-ai agents, in JavaScript: `JSCodeMode`.

The JavaScript counterpart of pydantic-ai-harness's Monty-based ``CodeMode``. Instead of calling
the agent's tools one model turn at a time, the model writes one JavaScript snippet that calls
them (``await tools.get_weather({city: "Paris"})``, ``Promise.all`` for concurrency), and the
snippet runs in a `pydeno.IsolatedRuntime`: a V8 isolate in a supervised, OS-sandboxed worker
process with a hard deadline and a memory ceiling.

    from pydantic_ai import Agent
    from pydeno.integrations.pydantic_ai import JSCodeMode

    agent = Agent("openai:gpt-5", capabilities=[JSCodeMode()])

Every tool call the snippet makes goes through a nested pydantic-ai ``ToolManager``, so it gets
the same argument validation, capability hooks, approval handling and usage accounting as a
direct tool call. Requires pydantic-ai (tested against 2.46); importing this module imports it.

Everything the snippet sends (which tools it calls, their arguments, its result) is untrusted
model output. The tools themselves run in this process with its full authority: the sandbox
confines the JavaScript, not your tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import math
import secrets
import time
import warnings
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from datetime import date, datetime, time as dt_time, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from typing_extensions import NotRequired, Self, TypedDict

from pydantic_ai import (
    AbstractToolset,
    FunctionToolset,
    RunContext,
    Tool,
    ToolDefinition,
    WrapperToolset,
)
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, ToolSearch
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    ToolFailed,
    ToolRetryError,
    UsageLimitExceeded,
    UserError,
)
from pydantic_ai.messages import (
    InstructionPart,
    ToolCallPart,
    ToolReturn,
    ToolReturnPart,
)
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.tools import (
    AgentDepsT,
    ToolDenied,
    ToolSelector,
    matches_tool_selector,
)
from pydantic_ai.toolsets.abstract import SchemaValidatorProt, ToolsetTool

from .._agent import _DECLARATION, _error_class
from .._isolated import IsolatedRuntime, WorkerCrashed
from .._pydeno import JavaScriptError, JsUndefined, RuntimeConfig, RuntimeTimeout
from .._schema import (
    _JS_RESERVED,
    NAMESPACE,
    SchemaTool,
    as_schema_tool,
    js_tool_names,
    schema_tools_to_dts,
)
from .._tools import ToolBridge, ToolBudgetError, _is_async_callable

__all__ = [
    "JSCodeMode",
    "JSCodeModeReturnSchemaWarning",
    "JSCodeModeToolset",
    "SchemaTool",
    "js_tool_names",
    "schema_toolset",
    "schema_tools_to_dts",
]

TOOL_NAME = "run_javascript"

# Owned by the toolset: the snippet contract depends on them.
_OWNED_RUNTIME_OPTIONS = frozenset(
    {"config", "request_timeout", "max_memory", "redact_host_errors"}
)
# Hidden JS names. `_STATE` holds the prelude's helpers; `_BIND` is where a ToolBridge installs
# new tools for one moment, before the prelude moves them onto `tools`.
_STATE = "__pydeno_jscm"
_BIND = "__pydeno_jscm_bind"
_PERSIST = "__pydeno_jscm_persist"

# Bounds on what is echoed back to the model.
_MAX_CONSOLE_CHARS = 32_000
_PREVIEW_CHARS = 120
_PREVIEW_ITEMS = 5
_SUMMARY_MAX_CHARS = 2_000
_MAX_ERROR_CHARS = 4_000


# ---------------------------------------------------------------------------
# tools described by JSON Schema
# ---------------------------------------------------------------------------


def _schema_tool(tool: SchemaTool) -> Tool[Any]:
    fn = tool.callable
    if _is_async_callable(fn):

        async def call(**arguments: Any) -> Any:
            return await fn(arguments)

    else:

        def call(**arguments: Any) -> Any:  # type: ignore[misc]
            return fn(arguments)

    built = Tool.from_schema(
        call,
        name=tool.name,
        description=tool.description,
        json_schema=json.loads(json.dumps(tool.input_schema)),
    )
    if tool.output_schema is not None:
        # The declared return type in the TypeScript the model sees (`Promise<T>`).
        built.function_schema.return_schema = json.loads(json.dumps(tool.output_schema))
    return built


def schema_toolset(
    tools: Sequence[Any] | Mapping[str, Any],
) -> FunctionToolset[Any]:
    """A pydantic-ai ``FunctionToolset`` from tools described by JSON Schema.

    Args:
        tools: `pydeno.SchemaTool`s, or mappings with their keys (``name``, ``description``,
            ``input_schema`` / ``inputSchema``, optional ``output_schema`` / ``outputSchema``,
            ``callable``), as a list or as ``name -> tool``. Each callable gets the arguments
            object as one ``dict``.

    pydantic-ai does **not** validate the arguments against ``input_schema`` for such a tool
    (``Tool.from_schema`` skips it): validate them in the callable. ``output_schema`` becomes the
    tool's return schema, so `JSCodeMode` declares ``Promise<T>`` for it.
    """
    if isinstance(tools, Mapping):
        found = [as_schema_tool(tool, name) for name, tool in tools.items()]
    else:
        found = [as_schema_tool(tool) for tool in tools]
    return FunctionToolset([_schema_tool(tool) for tool in found])


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------


class JSCodeModeReturnSchemaWarning(UserWarning):
    """A sandboxed tool has no return schema, so its declaration says ``Promise<unknown>``.

    The model then has to guess the shape of its result. A function tool gets a return schema
    from its return annotation; an MCP tool from its server's ``outputSchema``. To silence it:
    ``warnings.filterwarnings("ignore", category=JSCodeModeReturnSchemaWarning)``.
    """


def _guide(max_tool_calls: int, timeout: float | None) -> str:
    limit = (
        f" Each call may run for {timeout:g}s of JavaScript time (time spent waiting on tools "
        "does not count); a call that runs longer is stopped and the sandbox is reset."
        if timeout is not None
        else ""
    )
    return f"""\
Run JavaScript in a sandbox and get back what it returns. The sandbox is V8, not Node or a \
browser.

- Write plain JavaScript (not TypeScript: no type annotations). The code is the body of an \
async function: use `await`, and `return` the result. The returned value (plain data: objects, \
arrays, strings, numbers, booleans, null) is what you get back; without `return` you get \
nothing back. `console.log` output is returned as well, under `output`.
- Call tools as `await {NAMESPACE}.name({{...}})`: each takes ONE object argument with the \
fields declared below and returns a Promise. Run independent calls concurrently with \
`await Promise.all([...])`. At most {max_tool_calls} tool calls per `{TOOL_NAME}` call.
- Top-level `const`, `let`, `var`, `function` and `class` declarations written at the start of a \
line are kept for later calls; to keep anything else, assign it to `globalThis`. Pass \
`restart: true` to start again from a clean sandbox.
- There is no `fetch`, `require`, `import`, `setTimeout`, filesystem, network or environment. \
`Date.now()` is frozen and `Math.random()` is seeded.
- A failing tool throws an Error whose `name` is the Python error class (`ValidationError`, \
`ModelRetry`, `ToolDenied`, `ToolBudgetError`, ...) and whose `message` says what went wrong; \
catch it with try/catch or let it propagate, and an uncaught error is reported back to you.{limit}"""


# ---------------------------------------------------------------------------
# values crossing the boundary
# ---------------------------------------------------------------------------


def _from_js(value: Any, depth: int = 0) -> Any:
    """Guest data -> what a tool's validator gets: `undefined` properties dropped, sets as
    lists. (Bytes and dates stay: pydantic validates them.)"""
    if depth > 200:
        raise ValueError("arguments are nested too deeply")
    if isinstance(value, dict):
        return {
            str(k): _from_js(v, depth + 1)
            for k, v in value.items()
            if not isinstance(v, JsUndefined)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_from_js(v, depth + 1) for v in value]
    if isinstance(value, JsUndefined):
        return None
    return value


def _to_js(value: Any) -> Any:
    """A tool's result -> plain data, in the JSON form its declared return type describes
    (dates as ISO strings, bytes as base64, models as objects)."""
    return to_jsonable_python(
        value, bytes_mode="base64", serialize_unknown=True, inf_nan_mode="constants"
    )


def _to_model(value: Any, depth: int = 0) -> Any:
    """The snippet's result -> something every message serializer accepts."""
    if depth > 200:
        return "<nested too deeply>"
    if isinstance(value, JsUndefined):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return (
            "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        )
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_model(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_model(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_to_model(v, depth + 1) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes_base64": _to_js(bytes(value))}
    return repr(value)


def _console_text(args: list[Any]) -> str:
    parts = []
    for arg in args:
        if isinstance(arg, str):
            parts.append(arg)
        else:
            try:
                parts.append(json.dumps(_to_model(arg), ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(repr(arg))
    return " ".join(parts)


def _preview(value: Any, *, nested: bool = False) -> str:
    """A short rendering for an error message, cut before it is rendered."""
    if isinstance(value, str):
        cut = value[:_PREVIEW_CHARS]
        return repr(cut) + (
            f" ... ({len(value)} chars)" if len(value) > len(cut) else ""
        )
    if isinstance(value, (list, tuple)):
        if nested:
            return f"[{len(value)} items]"
        shown = ", ".join(_preview(v, nested=True) for v in value[:_PREVIEW_ITEMS])
        return f"[{shown}]" + (
            f" ... ({len(value)} items)" if len(value) > _PREVIEW_ITEMS else ""
        )
    if isinstance(value, dict):
        if nested:
            return f"{{{len(value)} items}}"
        items = list(value.items())[:_PREVIEW_ITEMS]
        shown = ", ".join(
            f"{_preview(k, nested=True)}: {_preview(v, nested=True)}" for k, v in items
        )
        return (
            "{"
            + shown
            + "}"
            + (f" ... ({len(value)} items)" if len(value) > _PREVIEW_ITEMS else "")
        )
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return f"<{type(value).__name__}>"


def _js_error(name: str, message: str) -> Exception:
    """An exception the guest sees as an Error with this `name` and `message`."""
    return _error_class(name)(message[:_MAX_ERROR_CHARS])


def _validation_summary(tool: str, exc: ValidationError) -> str:
    fields = []
    for err in exc.errors(include_url=False)[:10]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(arguments)"
        fields.append(f"{loc}: {err.get('msg', 'invalid')}")
    more = exc.error_count() - len(fields)
    tail = f"; and {more} more" if more > 0 else ""
    return f"invalid arguments for {tool}: " + "; ".join(fields) + tail


# ---------------------------------------------------------------------------
# the JavaScript side
# ---------------------------------------------------------------------------

# Installed once per sandbox, before any guest code. Wraps each tool so a run knows which calls
# are in flight and does not end while one is (a worker that finishes a command with a call still
# unanswered breaks when the answer arrives); compiles a snippet to check its syntax without
# running it; and moves newly bound tools onto `tools`. Intrinsics are captured first, so a guest
# that later replaces `Promise` or `Set` methods only breaks its own runs.
_PRELUDE = f"""(() => {{
  "use strict";
  if (Object.prototype.hasOwnProperty.call(globalThis, "{_STATE}")) return;
  const call = Function.prototype.call.bind.bind(Function.prototype.call);
  const apply = Reflect.apply;
  const then = call(Promise.prototype.then);
  const add = call(Set.prototype.add);
  const remove = call(Set.prototype.delete);
  const forEach = call(Set.prototype.forEach);
  const size = call(Object.getOwnPropertyDescriptor(Set.prototype, "size").get);
  const push = call(Array.prototype.push);
  const allSettled = Promise.allSettled.bind(Promise);
  const AsyncFunction = Object.getPrototypeOf(async function () {{}}).constructor;
  const SyntaxErr = SyntaxError;
  const toStr = String;
  const inflight = new Set();
  const track = (raw, name) => ({{ [name](...args) {{
    const p = apply(raw, undefined, args);
    add(inflight, p);
    const done = () => {{ remove(inflight, p); }};
    then(p, done, done);
    return p;
  }} }})[name];
  const state = Object.freeze({{
    settle: async () => {{
      while (size(inflight)) {{
        const pending = [];
        forEach(inflight, (p) => {{ push(pending, p); }});
        await allSettled(pending);
      }}
    }},
    check: (code) => {{
      try {{ new AsyncFunction(code); return null; }}
      catch (e) {{ return e instanceof SyntaxErr ? toStr(e.message) : null; }}
    }},
    install: (added, removed) => {{
      let t = globalThis.{NAMESPACE};
      if (typeof t !== "object" || t === null) {{ t = {{}}; globalThis.{NAMESPACE} = t; }}
      const src = globalThis.{_BIND};
      for (const [js, name] of added) t[js] = track(src[name], js);
      for (const js of removed) delete t[js];
      delete globalThis.{_BIND};
    }},
  }});
  Object.defineProperty(globalThis, "{_STATE}", {{
    value: state, writable: false, enumerable: false, configurable: false,
  }});
  globalThis.{NAMESPACE} = {{}};
}})();"""


def _wrap(code: str) -> str:
    """A snippet as the body of an async function that keeps its top-level declarations and
    does not end while one of its tool calls is in flight (as `AgentSandbox` does). The code
    starts on line 1 of the script."""
    names = dict.fromkeys(
        n for n in _DECLARATION.findall(code) if n not in _JS_RESERVED
    )
    saves = "".join(f"try {{ globalThis.{n} = {n}; }} catch {{}} " for n in names)
    return (
        f"(async () => {{ let {_PERSIST}; try {{ {_PERSIST} = () => {{ {saves}}}; "
        + code
        + f"\n}} finally {{ if ({_PERSIST}) {_PERSIST}(); await globalThis.{_STATE}.settle(); }} }})()"
    )


# ---------------------------------------------------------------------------
# per-run session and per-snippet execution
# ---------------------------------------------------------------------------


class _Gate:
    """Lets ordinary calls overlap and makes a `sequential` tool run alone."""

    def __init__(self, all_sequential: bool) -> None:
        self._cond = asyncio.Condition()
        self._active = 0
        self._exclusive = False
        self._waiting_exclusive = 0
        self._all = all_sequential

    @contextlib.asynccontextmanager
    async def hold(self, exclusive: bool) -> AsyncIterator[None]:
        exclusive = exclusive or self._all
        async with self._cond:
            if exclusive:
                self._waiting_exclusive += 1
                try:
                    await self._cond.wait_for(
                        lambda: not self._exclusive and self._active == 0
                    )
                finally:
                    self._waiting_exclusive -= 1
                self._exclusive = True
            else:
                await self._cond.wait_for(
                    lambda: not self._exclusive and self._waiting_exclusive == 0
                )
            self._active += 1
        try:
            yield
        finally:
            async with self._cond:
                self._active -= 1
                if exclusive:
                    self._exclusive = False
                self._cond.notify_all()


@dataclass
class _Execution:
    """State of one `run_javascript` call."""

    parent_id: str
    ctx: RunContext[Any]
    manager: ToolManager[Any]
    js_to_tool: dict[str, str]
    defs: dict[str, ToolDefinition]
    max_tool_calls: int
    gate: _Gate
    context: contextvars.Context
    count: int = 0
    inflight: int = 0
    budget_exhausted: bool = False
    fatal: BaseException | None = None
    calls: dict[str, ToolCallPart] = field(default_factory=dict)
    returns: dict[str, ToolReturnPart] = field(default_factory=dict)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    console: list[dict[str, str]] = field(default_factory=list)
    console_chars: int = 0
    console_dropped: int = 0

    def log(self, level: str, args: list[Any]) -> None:
        text = _console_text(args)
        if self.console_chars + len(text) > _MAX_CONSOLE_CHARS:
            self.console_dropped += 1
            return
        self.console_chars += len(text)
        self.console.append({"level": level, "text": text})

    def output(self) -> str:
        lines = [
            e["text"]
            if e["level"] in ("log", "info", "debug")
            else f"[{e['level']}] {e['text']}"
            for e in self.console
        ]
        if self.console_dropped:
            lines.append(f"... ({self.console_dropped} more console lines dropped)")
        return "\n".join(lines)

    def started_calls(self) -> str:
        """Which nested calls started, so a retry does not repeat their side effects."""
        if not self.calls:
            return ""
        lines: list[str] = []
        used = 0
        for call_id, call in self.calls.items():
            ret = self.returns.get(call_id)
            if ret is None:
                outcome = "did not finish, so it may have applied a partial change"
            elif ret.outcome == "denied":
                outcome = "was denied and did not run"
            elif ret.outcome == "failed":
                outcome = f"failed: {_preview(ret.content)}"
            else:
                outcome = f"returned {_preview(ret.content)}"
            line = f"- {call.tool_name}({_preview(call.args)}) {outcome}"
            if used + len(line) > _SUMMARY_MAX_CHARS:
                lines.append(f"- ... and {len(self.calls) - len(lines)} more not shown")
                break
            lines.append(line)
            used += len(line)
        n = len(self.calls)
        return (
            f"\n\n{n} tool call{'s' if n != 1 else ''} started before the code stopped:\n"
            + "\n".join(lines)
            + f"\nAccount for all {n} before retrying: repeating a call repeats what it did."
        )


class _Session:
    """One sandbox for one agent run: created on the first `run_javascript` call, kept across
    calls (REPL state), replaced after a reset, closed when the run ends."""

    def __init__(self, toolset: JSCodeModeToolset[Any]) -> None:
        self.runtime: IsolatedRuntime | None = None
        self.bound: dict[
            str, str
        ] = {}  # js name -> tool name, as installed in the guest
        self.execution: _Execution | None = None
        self.calls_made = 0
        self.resets = 0
        self._options = toolset

    def _on_console(self, level: str, args: list[Any]) -> None:
        execution = self.execution
        if execution is not None:
            execution.log(level, args)

    def _start(self) -> IsolatedRuntime:
        ts = self._options
        options = dict(ts.runtime_options)
        options.setdefault("clock", datetime.now(timezone.utc))
        options.setdefault("random_seed", secrets.randbelow(2**31))
        rt = IsolatedRuntime(
            RuntimeConfig(on_console=self._on_console),
            request_timeout=ts.timeout,
            max_memory=ts.max_memory,
            # Every error the guest sees passes through `_dispatch`, which decides what it says.
            redact_host_errors=False,
            **options,
        )
        try:
            rt.eval(_PRELUDE)
        except BaseException:
            rt.close()
            raise
        return rt

    async def runtime_for(self, tools: Mapping[str, str]) -> IsolatedRuntime:
        """The live runtime, started if needed, with exactly `tools` (js name -> tool name)
        installed on the guest's `tools` object."""
        if self.runtime is not None and self.runtime.is_closed():
            await self.reset()
        if self.runtime is None:
            self.runtime = await asyncio.to_thread(self._start)
            self.bound = {}
        rt = self.runtime
        added = [js for js in tools if js not in self.bound]
        removed = [js for js in self.bound if js not in tools]
        if added or removed:
            await asyncio.to_thread(self._install, rt, added, removed)
            self.bound = dict(tools)
        return rt

    def _install(
        self, rt: IsolatedRuntime, added: list[str], removed: list[str]
    ) -> None:
        session = self

        def shim_for(js: str) -> Callable[..., Any]:
            async def shim(*args: Any) -> Any:
                execution = session.execution
                if execution is None:
                    raise _js_error(
                        "RuntimeError", "no run_javascript call is in progress"
                    )
                return await session._options._dispatch(session, execution, js, args)

            shim.__name__ = js
            return shim

        if added:
            ToolBridge({js: shim_for(js) for js in added}, namespace=_BIND).attach(rt)
        rt.eval(
            f"globalThis.{_STATE}.install({json.dumps([[js, js] for js in added])}, "
            f"{json.dumps(removed)})"
        )

    async def reset(self) -> None:
        rt, self.runtime, self.bound = self.runtime, None, {}
        if rt is not None:
            self.resets += 1
            await asyncio.to_thread(rt.close)

    async def close(self) -> None:
        await self.reset()


# ---------------------------------------------------------------------------
# the toolset
# ---------------------------------------------------------------------------


class _RunJavaScriptArgs(TypedDict):
    code: Annotated[
        str,
        Field(
            description="Plain JavaScript (not TypeScript): the body of an async function. "
            "`await` tool calls and `return` the result."
        ),
    ]
    restart: NotRequired[
        Annotated[
            bool,
            Field(
                description="Start from a clean sandbox, discarding declarations and state kept "
                "from earlier calls. Default false."
            ),
        ]
    ]


_ARGS_ADAPTER = TypeAdapter(_RunJavaScriptArgs)
_ARGS_SCHEMA = _ARGS_ADAPTER.json_schema()
_ARGS_VALIDATOR: SchemaValidatorProt = _ARGS_ADAPTER.validator  # type: ignore[assignment]


@dataclass(kw_only=True)
class _RunJavaScriptTool(ToolsetTool[AgentDepsT]):
    """`run_javascript`, carrying what `get_tools` worked out for `call_tool`."""

    js_to_tool: dict[str, str]
    sandboxed: dict[str, ToolsetTool[AgentDepsT]]


def _is_code_tool(td: ToolDefinition) -> bool:
    return bool(td.metadata and "code_arg_name" in td.metadata)


@dataclass
class JSCodeModeToolset(WrapperToolset[AgentDepsT]):
    """Implementation toolset for `JSCodeMode`: exposes ``run_javascript`` plus every tool that
    stays native, and runs snippets in a per-run `IsolatedRuntime`.

    Tools stay native (visible to the model as ordinary tool calls) when they are not selected
    by `tool_selector`, are framework tools (``tool_kind`` set: tool search, capability
    loading), are not available yet (deferred loading), are output tools, have a native
    counterpart (``unless_native``) or are themselves code-execution tools.
    """

    tool_selector: ToolSelector[AgentDepsT] = "all"
    """Which wrapped tools are callable from JavaScript; the rest stay native."""

    max_retries: int = 3
    """Retries for ``run_javascript`` (syntax and runtime errors count)."""

    _: KW_ONLY

    max_tool_calls: int = 100
    """Nested tool calls one snippet may make; further calls throw ``ToolBudgetError``."""

    max_session_tool_calls: int | None = None
    """Nested tool calls over the whole agent run (all snippets); ``None``: unlimited."""

    timeout: float | None = 30.0
    """Seconds of JavaScript execution per snippet, enforced by killing the worker (time spent
    waiting on tools does not count). ``None`` removes it."""

    max_memory: int | None = 256 << 20
    """Resident memory ceiling of the worker process, in bytes. ``None`` removes it."""

    approvals: Literal["inline", "defer"] = "inline"
    """How tools that need approval are handled: ``"inline"`` resolves them during the snippet
    through the agent's ``HandleDeferredToolCalls`` capability. ``"defer"`` is not implemented."""

    dynamic_catalog: bool = False
    """Put the tool declarations in the instructions instead of the tool description, so the
    tool definitions stay byte-stable (prompt cache) when the toolset changes mid-run."""

    runtime_options: Mapping[str, Any] = field(default_factory=dict)
    """Extra keyword arguments for `pydeno.IsolatedRuntime` (``sandbox``, ``max_host_wait``,
    ``max_inflight_host_calls``, ``clock``, ``random_seed``, ``jitless``, ...)."""

    capability: AbstractCapability[AgentDepsT] | None = field(default=None, repr=False)

    _session: _Session | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _entered: int = field(default=0, init=False, repr=False, compare=False)
    _warned: set[str] = field(
        default_factory=set, init=False, repr=False, compare=False
    )
    _last_catalog: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _check_options(self)

    # -- lifecycle -------------------------------------------------------------

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        """A fresh instance per agent run, so concurrent runs never share a sandbox."""
        wrapped = await self.wrapped.for_run(ctx)
        new = replace(self, wrapped=wrapped)
        new._warned = self._warned
        return new

    async def for_run_step(
        self, ctx: RunContext[AgentDepsT]
    ) -> AbstractToolset[AgentDepsT]:
        new_wrapped = await self.wrapped.for_run_step(ctx)
        if new_wrapped is self.wrapped:
            return self
        new = replace(self, wrapped=new_wrapped)
        new._session = self._session
        new._entered = self._entered
        new._warned = self._warned
        new._last_catalog = self._last_catalog
        return new

    async def __aenter__(self) -> Self:
        if self._entered == 0:
            self._session = _Session(self)
        self._entered += 1
        try:
            await self.wrapped.__aenter__()
        except BaseException:
            await self._leave()
            raise
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        try:
            return await self.wrapped.__aexit__(*args)
        finally:
            await self._leave()

    async def _leave(self) -> None:
        self._entered = max(0, self._entered - 1)
        if self._entered == 0 and self._session is not None:
            session, self._session = self._session, None
            await session.close()

    # -- catalog ---------------------------------------------------------------

    async def get_instructions(
        self, ctx: RunContext[AgentDepsT]
    ) -> str | InstructionPart | Sequence[str | InstructionPart] | None:
        upstream = await self.wrapped.get_instructions(ctx)
        if not self._last_catalog:
            return upstream
        part = InstructionPart(content=self._last_catalog, dynamic=True)
        if upstream is None:
            return part
        if isinstance(upstream, (str, InstructionPart)):
            return [upstream, part]
        return [*upstream, part]

    async def get_tools(
        self, ctx: RunContext[AgentDepsT]
    ) -> dict[str, ToolsetTool[AgentDepsT]]:
        wrapped_tools = await self.wrapped.get_tools(ctx)
        if TOOL_NAME in wrapped_tools:
            raise UserError(
                f"Tool name {TOOL_NAME!r} is reserved for JSCodeMode; rename your tool."
            )
        sandboxed: dict[str, ToolsetTool[AgentDepsT]] = {}
        native: dict[str, ToolsetTool[AgentDepsT]] = {}
        for name, tool in wrapped_tools.items():
            td = tool.tool_def
            if (
                td.tool_kind is not None
                or td.kind == "output"
                or not ctx.is_tool_available(td)
                or td.unless_native
                or _is_code_tool(td)
                or not await matches_tool_selector(self.tool_selector, ctx, td)
            ):
                native[name] = tool
            else:
                sandboxed[name] = tool
        tool_to_js = js_tool_names(sandboxed)
        missing = [
            n
            for n, t in sandboxed.items()
            # `{}` is what an unannotated function tool gets: as uninformative as none.
            if not t.tool_def.return_schema and n not in self._warned
        ]
        if missing:
            self._warned.update(missing)
            listed = ", ".join(repr(n) for n in missing)
            warnings.warn(
                f"JSCodeMode: no return schema for {listed}; the model sees `Promise<unknown>`. "
                "Add a return annotation (or an MCP outputSchema), or filter "
                "JSCodeModeReturnSchemaWarning.",
                JSCodeModeReturnSchemaWarning,
                stacklevel=2,
            )
        catalog = (
            "```ts\n"
            + schema_tools_to_dts([t.tool_def for t in sandboxed.values()])
            + "```"
        )
        guide = _guide(self.max_tool_calls, self.timeout)
        if self.dynamic_catalog:
            description = guide + "\n\nThe tools are declared in the instructions."
            self._last_catalog = (
                f"Tools callable from `{TOOL_NAME}` code:\n\n{catalog}"
                if sandboxed
                else ""
            )
        else:
            description = guide + "\n\n" + catalog
            self._last_catalog = ""
        result: dict[str, ToolsetTool[AgentDepsT]] = dict(native)
        result[TOOL_NAME] = _RunJavaScriptTool(
            toolset=self,
            tool_def=ToolDefinition(
                name=TOOL_NAME,
                description=description,
                parameters_json_schema=_ARGS_SCHEMA,
                metadata={"code_arg_name": "code", "code_arg_language": "javascript"},
                sequential=True,
                capability_id=self._capability_id(ctx),
            ),
            max_retries=self.max_retries,
            args_validator=_ARGS_VALIDATOR,
            js_to_tool={js: name for name, js in tool_to_js.items()},
            sandboxed=sandboxed,
        )
        return result

    def _capability_id(self, ctx: RunContext[AgentDepsT]) -> str | None:
        if self.capability is None:
            return None
        return next(
            (rid for rid, cap in ctx.capabilities.items() if cap is self.capability),
            None,
        )

    # -- running ---------------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[AgentDepsT],
        tool: ToolsetTool[AgentDepsT],
    ) -> Any:
        if not isinstance(tool, _RunJavaScriptTool):
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        session = self._session
        if session is None:
            # Not entered (called outside an agent run). Works, but nothing closes the worker
            # except the runtime's own finalizer; enter the toolset to manage it.
            session = self._session = _Session(self)
        if tool_args.get("restart"):
            await session.reset()
        return await self._run(session, str(tool_args["code"]), ctx, tool)

    async def _run(
        self,
        session: _Session,
        code: str,
        ctx: RunContext[Any],
        tool: _RunJavaScriptTool[Any],
    ) -> ToolReturn[Any]:
        parent = ctx.tool_manager
        if parent is None:
            raise UserError(
                "JSCodeMode needs ctx.tool_manager (run it inside an agent run)"
            )
        manager = ToolManager(
            toolset=self.wrapped,
            root_capability=parent.root_capability,
            ctx=ctx,
            tools=dict(tool.sandboxed),
        )
        execution = _Execution(
            parent_id=ctx.tool_call_id or "pyd_ai_js_code_mode",
            ctx=ctx,
            manager=manager,
            js_to_tool=tool.js_to_tool,
            defs={name: t.tool_def for name, t in tool.sandboxed.items()},
            max_tool_calls=self.max_tool_calls,
            gate=_Gate(manager.get_parallel_execution_mode() != "parallel"),
            context=contextvars.copy_context(),
        )
        rt = await session.runtime_for(tool.js_to_tool)
        try:
            problem = await rt.eval_async(
                f"globalThis.{_STATE}.check({json.dumps(code)})"
            )
        except (RuntimeTimeout, WorkerCrashed) as exc:
            await session.reset()
            raise ModelRetry(
                f"The sandbox failed before the code ran ({exc}) and was reset. Try again."
            ) from exc
        if isinstance(problem, str):
            raise ModelRetry(
                f"Syntax error: {problem}\n\nNothing ran. Write plain JavaScript (no TypeScript "
                "annotations), as the body of an async function."
            )

        started = time.monotonic()
        session.execution = execution
        try:
            value = await rt.eval_async(_wrap(code))
        except JavaScriptError as exc:
            await self._settle(execution)
            raise self._runtime_error(execution, exc) from exc
        except (RuntimeTimeout, WorkerCrashed) as exc:
            await self._settle(execution, cancel=True)
            await session.reset()
            if execution.fatal is not None:
                raise execution.fatal from exc
            raise ModelRetry(self._reset_message(execution, exc)) from exc
        except (TypeError, RuntimeError) as exc:
            await self._settle(execution, cancel=True)
            if rt.is_closed():
                await session.reset()
                if execution.fatal is not None:
                    raise execution.fatal from exc
                raise ModelRetry(self._reset_message(execution, exc)) from exc
            if execution.fatal is not None:
                raise execution.fatal from exc
            raise ModelRetry(
                f"The code ran, but its result cannot be returned: {exc}. Return plain data "
                "(objects, arrays, strings, numbers, booleans, null), not functions or symbols."
                + _with_output(execution)
            ) from exc
        except BaseException:
            # Cancelled (the run is being torn down): the worker was killed mid-command.
            await self._settle(execution, cancel=True)
            await session.reset()
            raise
        finally:
            session.execution = None
        await self._settle(execution)
        if execution.fatal is not None:
            raise execution.fatal
        duration_ms = round((time.monotonic() - started) * 1000, 1)
        return self._result(execution, value, duration_ms)

    @staticmethod
    async def _settle(execution: _Execution, *, cancel: bool = False) -> None:
        """Wait for (or cancel) nested calls still running after the snippet ended, so none
        outlives its `run_javascript` call."""
        tasks = [t for t in execution.tasks if not t.done()]
        if not tasks:
            return
        if cancel:
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _runtime_error(
        self, execution: _Execution, exc: JavaScriptError
    ) -> BaseException:
        if execution.fatal is not None:
            return execution.fatal
        text = str(exc).removeprefix("Evaluation failed: ")[:_MAX_ERROR_CHARS]
        hint = ""
        if text.startswith("ToolBudgetError"):
            hint = (
                "\n\nCall fewer tools per run (filter the inputs first), or split the work "
                "across several run_javascript calls."
            )
        return ModelRetry(
            f"Runtime error: {text}{hint}{_with_output(execution)}{execution.started_calls()}"
        )

    def _reset_message(self, execution: _Execution, exc: BaseException) -> str:
        if isinstance(exc, RuntimeTimeout) and "hard deadline" in str(exc):
            why = (
                f"it ran longer than the {self.timeout:g}s limit (time spent in tools does "
                "not count). Make it do less work per call."
            )
        elif isinstance(exc, RuntimeTimeout):
            why = f"{exc}."
        elif "max_memory" in str(exc):
            why = "it used more memory than the sandbox allows. Process less data at once."
        else:
            why = f"the sandbox stopped ({type(exc).__name__}: {exc})."
        return (
            f"The code was stopped: {why}\n\nThe sandbox was reset: declarations and "
            "globalThis state from earlier calls are gone."
            f"{_with_output(execution)}{execution.started_calls()}"
        )

    def _result(
        self, execution: _Execution, value: Any, duration_ms: float
    ) -> ToolReturn[Any]:
        output = execution.output()
        result = _to_model(value)
        if output:
            return_value: Any = (
                {"output": output}
                if isinstance(value, JsUndefined)
                else {"output": output, "result": result}
            )
        elif isinstance(value, JsUndefined):
            return_value = {
                "result": None,
                "note": "The code returned nothing. End it with `return <value>;`.",
            }
        else:
            return_value = result
        return ToolReturn(
            return_value=return_value,
            metadata={
                "code_mode": True,
                "language": "javascript",
                "tool_calls": execution.calls,
                "tool_returns": execution.returns,
                "console": execution.console,
                "duration_ms": duration_ms,
            },
        )

    # -- nested tool calls -------------------------------------------------------

    async def _dispatch(
        self, session: _Session, execution: _Execution, js: str, args: tuple[Any, ...]
    ) -> Any:
        """One tool call from the guest. Runs on the agent's event loop."""
        if session.execution is not execution:
            raise _js_error(
                "RuntimeError", "this run_javascript call has already ended"
            )
        name = execution.js_to_tool.get(js)
        if name is None:
            raise _js_error("ToolUnavailable", f"tools.{js} is not available now")
        if execution.fatal is not None:
            raise _js_error(type(execution.fatal).__name__, str(execution.fatal))
        if execution.count >= execution.max_tool_calls:
            execution.budget_exhausted = True
            raise ToolBudgetError(
                f"this run already made its {execution.max_tool_calls} tool calls; refused "
                f"tools.{js}"
            )
        limit = self.max_session_tool_calls
        if limit is not None and session.calls_made >= limit:
            execution.budget_exhausted = True
            raise ToolBudgetError(
                f"this agent run already made its {limit} tool calls from code; refused tools.{js}"
            )
        ctx = execution.ctx
        limits = ctx.usage_limits
        if limits is not None and limits.tool_calls_limit is not None:
            projected = ctx.usage.tool_calls + execution.inflight + 1
            if projected > limits.tool_calls_limit:
                execution.fatal = UsageLimitExceeded(
                    f"The next tool call(s) would exceed the tool_calls_limit of "
                    f"{limits.tool_calls_limit} (tool_calls={projected})."
                )
                raise _js_error("UsageLimitExceeded", str(execution.fatal))
        execution.count += 1
        session.calls_made += 1
        if len(args) > 1:
            raise _js_error(
                "TypeError",
                f"tools.{js} takes one object argument, like tools.{js}({{field: value}}); "
                f"got {len(args)} arguments",
            )
        raw = args[0] if args else None
        if raw is None or isinstance(raw, JsUndefined):
            raw = {}
        if not isinstance(raw, dict):
            raise _js_error(
                "TypeError",
                f"tools.{js} takes one object argument, like tools.{js}({{field: value}}); "
                f"got {type(raw).__name__}",
            )
        call = ToolCallPart(
            tool_name=name,
            args=_from_js(raw),
            tool_call_id=f"{execution.parent_id}__{execution.count}",
        )
        execution.calls[call.tool_call_id] = call
        td = execution.defs.get(name)
        execution.inflight += 1
        try:
            async with execution.gate.hold(bool(td and td.sequential)):
                loop = asyncio.get_running_loop()
                # In the agent run's context (a copy per call), not the worker thread's: hooks,
                # tracing and contextvars-based state see the run they belong to.
                task = loop.create_task(
                    self._nested(execution, call, td), context=execution.context.copy()
                )
                execution.tasks.add(task)
                try:
                    return await task
                finally:
                    execution.tasks.discard(task)
        finally:
            execution.inflight -= 1

    async def _nested(
        self, execution: _Execution, call: ToolCallPart, td: ToolDefinition | None
    ) -> Any:
        name = call.tool_name

        def failed(error: Exception, shown: str) -> Exception:
            execution.returns[call.tool_call_id] = ToolReturnPart(
                tool_name=name,
                content=shown,
                tool_call_id=call.tool_call_id,
                outcome="failed",
            )
            return error

        try:
            result = await execution.manager.handle_call(
                call, wrap_validation_errors=False
            )
        except UsageLimitExceeded as exc:
            execution.fatal = exc
            raise failed(_js_error("UsageLimitExceeded", str(exc)), str(exc)) from None
        except ValidationError as exc:
            message = _validation_summary(name, exc)
            raise failed(_js_error("ValidationError", message), message) from None
        except ModelRetry as exc:
            raise failed(_js_error("ModelRetry", exc.message), exc.message) from None
        except ToolRetryError as exc:
            message = str(exc)
            raise failed(_js_error("ModelRetry", message), message) from None
        except ToolFailed as exc:
            raise failed(_js_error("ToolFailed", exc.message), exc.message) from None
        except (ApprovalRequired, CallDeferred) as exc:
            kind = (
                "requires approval"
                if isinstance(exc, ApprovalRequired)
                else "is deferred"
            )
            message = (
                f"tool {name!r} {kind}, and no HandleDeferredToolCalls capability on the agent "
                "resolved it inline (JSCodeMode cannot pause a snippet for an external answer)"
            )
            raise failed(_js_error(type(exc).__name__, message), message) from None
        except UserError as exc:
            message = str(exc)
            raise failed(_js_error("UserError", message), message) from None
        except Exception as exc:  # noqa: BLE001 - the guest sees the failure, curated
            expose = bool(td and td.metadata and td.metadata.get("expose_errors"))
            message = (
                str(exc) if expose else f"tool {name!r} failed (details are not shown)"
            )
            raise failed(_js_error(type(exc).__name__, message), message) from None
        if isinstance(result, ToolDenied):
            execution.returns[call.tool_call_id] = ToolReturnPart(
                tool_name=name,
                content=result.message,
                tool_call_id=call.tool_call_id,
                outcome="denied",
            )
            raise _js_error("ToolDenied", f"tool {name!r} was denied: {result.message}")
        metadata: Any = None
        if isinstance(result, ToolReturn):
            metadata = result.metadata
            result = result.return_value
        execution.returns[call.tool_call_id] = ToolReturnPart(
            tool_name=name,
            content=result,
            tool_call_id=call.tool_call_id,
            metadata=metadata,
        )
        return _to_js(result)


def _with_output(execution: _Execution) -> str:
    output = execution.output()
    return f"\n\nConsole output before it stopped:\n{output}" if output else ""


def _check_options(options: Any) -> None:
    if options.approvals == "defer":
        raise NotImplementedError(
            "JSCodeMode(approvals='defer') is not implemented yet: approvals are resolved "
            "inline, through a HandleDeferredToolCalls capability on the agent "
            "(approvals='inline')."
        )
    if options.approvals != "inline":
        raise ValueError("approvals must be 'inline' (or 'defer', not implemented yet)")
    if (
        not isinstance(options.max_tool_calls, int)
        or isinstance(options.max_tool_calls, bool)
        or options.max_tool_calls < 1
    ):
        raise UserError("max_tool_calls must be an int of at least 1")
    session_limit = options.max_session_tool_calls
    if session_limit is not None and (
        not isinstance(session_limit, int)
        or isinstance(session_limit, bool)
        or session_limit < 1
    ):
        raise UserError("max_session_tool_calls must be None or an int of at least 1")
    if options.timeout is not None and not options.timeout > 0:
        raise UserError("timeout must be None or a positive number of seconds")
    owned = _OWNED_RUNTIME_OPTIONS & set(options.runtime_options)
    if owned:
        raise UserError(
            f"runtime_options may not set {sorted(owned)}: JSCodeMode sets them "
            "(use timeout= and max_memory=)"
        )


# ---------------------------------------------------------------------------
# the capability
# ---------------------------------------------------------------------------


@dataclass
class JSCodeMode(AbstractCapability[AgentDepsT]):
    """Let the model call the agent's tools from JavaScript, in one ``run_javascript`` tool.

    ```python
    from pydantic_ai import Agent
    from pydeno.integrations.pydantic_ai import JSCodeMode

    agent = Agent("openai:gpt-5", capabilities=[JSCodeMode()])
    ```

    The model sees one tool, ``run_javascript(code, restart?)``, whose description declares
    the selected tools as TypeScript (``declare namespace tools { ... }``, generated from their
    JSON schemas). Its code calls them as ``await tools.name({...})``. Each agent run gets its
    own sandbox, started on first use and closed when the run ends; declarations persist
    between ``run_javascript`` calls of that run.

    Mirrors pydantic-ai-harness's ``CodeMode`` options where they apply, so switching is a
    one-line change. See ``docs/guides/pydantic-ai.md``.
    """

    tools: ToolSelector[AgentDepsT] = "all"
    """Which tools become callable from JavaScript: ``'all'``, a list of names, a predicate
    ``(ctx, tool_def) -> bool``, or a metadata dict. The others stay native tool calls."""

    max_retries: int = 3
    """Retries for ``run_javascript``: syntax errors, uncaught errors and resets count."""

    _: KW_ONLY

    max_tool_calls: int = 100
    """Nested tool calls per snippet."""

    max_session_tool_calls: int | None = None
    """Nested tool calls per agent run, over all snippets (``None``: unlimited)."""

    timeout: float | None = 30.0
    """Seconds of JavaScript execution per snippet; time waiting on tools does not count."""

    max_memory: int | None = 256 << 20
    """The worker's resident-memory ceiling, in bytes."""

    approvals: Literal["inline", "defer"] = "inline"
    """``"inline"``: resolve approval-required tools during the snippet via the agent's
    ``HandleDeferredToolCalls`` capability. ``"defer"`` raises ``NotImplementedError``."""

    dynamic_catalog: bool = False
    """Declare the tools in the instructions instead of the tool description."""

    runtime_options: Mapping[str, Any] = field(default_factory=dict)
    """Extra keyword arguments for `pydeno.IsolatedRuntime`."""

    schema_tools: Sequence[Any] = ()
    """Extra tools described by JSON Schema (`pydeno.SchemaTool`s or mappings with their keys),
    added to the agent and callable from JavaScript like its other tools; see `schema_toolset`."""

    # Tools already warned about (missing return schema), shared by every toolset this makes.
    _warned: set[str] = field(
        default_factory=set, init=False, repr=False, compare=False
    )
    _schema_toolset: FunctionToolset[Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _check_options(self)
        # Built (and so checked) once, up front: a bad schema tool fails here, not mid-run.
        self._schema_toolset = (
            schema_toolset(list(self.schema_tools)) if self.schema_tools else None
        )

    def get_toolset(self) -> AbstractToolset[AgentDepsT] | None:
        return self._schema_toolset

    def get_ordering(self) -> CapabilityOrdering:
        """Outermost, and around ToolSearch, so ``search_tools`` stays a native tool."""
        return CapabilityOrdering(position="outermost", wraps=[ToolSearch])

    def get_wrapper_toolset(
        self, toolset: AbstractToolset[AgentDepsT]
    ) -> AbstractToolset[AgentDepsT] | None:
        toolset = JSCodeModeToolset(
            wrapped=toolset,
            tool_selector=self.tools,
            max_retries=self.max_retries,
            max_tool_calls=self.max_tool_calls,
            max_session_tool_calls=self.max_session_tool_calls,
            timeout=self.timeout,
            max_memory=self.max_memory,
            approvals=self.approvals,
            dynamic_catalog=self.dynamic_catalog,
            runtime_options=self.runtime_options,
            capability=self,
        )
        toolset._warned = self._warned
        return toolset
