"""`Pydeno`: the front door. A secure, fast sandbox for AI-generated JavaScript, shaped like Monty.

```python
with Pydeno() as pool:                       # pre-started, OS-sandboxed workers
    with pool.checkout() as session:         # one single-use worker, checked out by `with`
        session.feed_run("const x = 20")
        session.feed_run("x + 1")            # -> 21: state persists between feeds
```

A user of pydantic-monty should recognise every name: `Pydeno` / `AsyncPydeno` are `Monty` /
`AsyncMonty`, `checkout(limits=...)` hands out a `PydenoSession` (`feed_run`, `feed_start`,
`dump`, `load_session`, `load_snapshot`, `worker_pid`), `feed_start` suspends at every external
call with a `PydenoSnapshot` (`function_name`, `args`, `call_id`, `resume`, `resume_auto`,
`dump`), `PydenoLimits` is `ResourceLimits`, and the errors are `PydenoError` and friends.

Nothing here is new machinery. A `Pydeno` owns a `SandboxPool` (workers started ahead of time, each
used once) and every session is an `AgentSandbox` running on a checked-out `IsolatedRuntime`
(`AgentSandbox(runtime=...)`), so tools, budgets, console capture, the frozen clock, the signed
journal and deterministic replay are exactly the agent sandbox's. This module only translates:

* `external_lookup` -> one hidden dispatcher tool, plus a stub per name installed before the feed
  runs (JavaScript cannot intercept undefined names, so a callable must be named up front);
* `inputs` -> globals assigned from JSON before the feed runs (plain data only);
* the feed's trailing expression -> its result, by rewriting that one statement to ``return``;
* `PydenoLimits` -> the session's options; errors -> `PydenoError` subclasses that wrap the
  original exception (`classify_error` sees through them).

Secure by default: ``sandbox="require"`` (no silent downgrade), ``--jitless``, host errors redacted,
every limit set, and a worker never serves a second session. Relaxing any of that is an explicit,
documented argument.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import functools
import inspect
import json
import math
import os
import queue
import re
import secrets
import signal
import sys
import threading
import warnings
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from . import _sandbox_pool
from ._agent import (
    _COMPILES,
    _MISSING,
    DEFAULT_MAX_JOURNAL_BYTES,
    AgentSandbox,
    Done,
    Failed,
    JournalError,
    ToolCall,
    _error_class,
    _open_journal,
    _public,
    _PROCESS_THREADS,
    _ThreadBudget,
    _ThreadsExhausted,
    _TOOL_OF,
    _unavailable,
    preinstall,
)
from ._isolated import _CONTROL, IsolatedRuntime, WorkerCrashed
from ._limits import limit_int, limit_seconds
from ._pydeno import JavaScriptError, JsUndefined, RuntimeConfig, RuntimeTimeout
from ._result import _STDOUT_LEVELS, ResultTooLarge, format_console_arg
from ._schema import _JS_RESERVED
from ._tools import ToolBridge

__all__ = [
    "Pydeno",
    "PydenoComplete",
    "PydenoCrashedError",
    "PydenoError",
    "PydenoLimits",
    "PydenoRuntimeError",
    "PydenoSession",
    "PydenoSnapshot",
    "PydenoSyntaxError",
    "PydenoTimeoutError",
    "ToolThreadLimitError",
]

DEFAULT_MIN_PROCESSES = 2
#: Session tool threads one pool may hold at once (see `Pydeno(max_tool_threads=...)`).
DEFAULT_MAX_TOOL_THREADS = 128
_MIB = 1024 * 1024
#: The limits every session gets unless told otherwise (see `PydenoLimits`).
DEFAULT_LIMITS: dict[str, Any] = {
    "max_feed_duration_secs": 30.0,
    "max_memory": 512 * _MIB,
    "max_suspensions": 1000,
    "max_host_wait_secs": 600.0,
}
_EXTERNAL = "__pydeno_external"
_RESERVED_PREFIX = "__pydeno"
_MAX_INPUT_DEPTH = 64
_MAX_SAFE_INT = 2**53 - 1


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------


class PydenoLimits(TypedDict, total=False):
    """Resource limits for a session: Monty's `ResourceLimits`, mapped onto pydeno's.

    Omit a key to keep its default. ``None`` removes a limit where that is allowed (it is an
    explicit choice, never the default). The defaults: 30 s per feed, 512 MiB of worker memory,
    1000 external calls per session, 600 s of waiting on the host per feed.
    """

    max_feed_duration_secs: float | None
    """Hard limit on one feed's guest running time (time suspended at an external call does not
    count). Exceeding it kills the worker: `PydenoTimeoutError`, and the session is over (Monty
    raises inside the sandbox and keeps the session). The worker's CPU is capped at twice this."""

    max_turn_duration_secs: float | None
    """Monty's per-turn limit. One V8 command runs the whole feed, so pydeno enforces it as a cap
    on the feed's guest time (``min`` with ``max_feed_duration_secs``): never weaker than Monty's."""

    max_memory: int | None
    """The worker's resident memory, in bytes (it is killed past it; `PydenoCrashedError`). Also
    caps `ArrayBuffer` storage at a quarter of it, which the guest sees as a catchable
    `RangeError`. Fixed when a worker starts: a session asking for a value other than its pool's
    gets a freshly started worker (a cold start)."""

    max_recursion_depth: int | None
    """Not supported: V8 bounds recursion by stack size (a catchable `RangeError: Maximum call
    stack size exceeded`), and raising that limit safely is not something a flag can promise.
    Passing a value raises `ValueError`."""

    max_suspensions: int | None
    """External calls the guest may make over the session's life (default 1000; ``None`` keeps
    it, as in Monty). The call over budget throws a catchable ``ToolBudgetError`` in the guest."""

    gc_interval: int | None
    """Not supported (V8 decides when to collect). Passing a value raises `ValueError`."""

    max_total_sleep_secs: float | None
    """Accepted and always satisfied: a guest cannot sleep (its timers run on virtual time)."""

    max_host_wait_secs: float | None
    """pydeno only: most time one feed may spend suspended, waiting on external calls in total
    (default 600 s). Enforced while the external call runs: past it the worker is killed and the
    feed raises `PydenoTimeoutError` within about 0.1 s, even if the external function never
    returns (it is left running on its own thread; its answer is discarded). The same holds for
    the CPU cap while the guest computes during an external call."""


_LIMIT_KEYS = frozenset(PydenoLimits.__annotations__)


@dataclass(frozen=True)
class _Limits:
    timeout: float | None
    max_pause: float | None
    max_memory: int | None
    max_tool_calls: int


def _positive(name: str, value: Any, kind: type = float) -> Any:
    """A positive limit or None: `TypeError` for a wrong type, `ValueError` for a bad value."""
    if kind is float:
        return limit_seconds(name, value)
    return limit_int(name, value, minimum=1)


def _resolve_limits(*layers: Mapping[str, Any] | None) -> _Limits:
    merged = dict(DEFAULT_LIMITS)
    for layer in layers:
        if layer is None:
            continue
        if not isinstance(layer, Mapping):
            raise TypeError("limits must be a PydenoLimits mapping")
        unknown = sorted(set(layer) - _LIMIT_KEYS)
        if unknown:
            raise TypeError(f"unknown limits {unknown} (known: {sorted(_LIMIT_KEYS)})")
        merged.update(layer)
    for unsupported in ("max_recursion_depth", "gc_interval"):
        if merged.get(unsupported) is not None:
            raise ValueError(
                f"{unsupported} is not supported by pydeno (see PydenoLimits); leave it out"
            )
    feed = _positive("max_feed_duration_secs", merged.get("max_feed_duration_secs"))
    turn = _positive("max_turn_duration_secs", merged.get("max_turn_duration_secs"))
    caps = [v for v in (feed, turn) if v is not None]
    suspensions = merged.get("max_suspensions")
    if suspensions is None:
        suspensions = DEFAULT_LIMITS["max_suspensions"]
    suspensions = limit_int("max_suspensions", suspensions, minimum=0)
    _positive("max_total_sleep_secs", merged.get("max_total_sleep_secs"))
    return _Limits(
        timeout=float(min(caps)) if caps else None,
        max_pause=_positive("max_host_wait_secs", merged.get("max_host_wait_secs")),
        max_memory=_positive("max_memory", merged.get("max_memory"), int),
        max_tool_calls=suspensions,
    )


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class PydenoError(Exception):
    """Base class of every error a `Pydeno` session raises for a feed (Monty's `MontyError`).

    `exception()` returns the pydeno exception it wraps (a `JavaScriptError`, `WorkerCrashed`,
    `RuntimeTimeout`, ...); `classify_error` classifies that one."""

    def __init__(self, message: str, inner: BaseException | None = None) -> None:
        super().__init__(message)
        self._pydeno_inner = inner

    def exception(self) -> BaseException:
        """The exception pydeno raised underneath (this one if there is none)."""
        return self._pydeno_inner if self._pydeno_inner is not None else self


class _GuestError(PydenoError):
    """An error the guest's JavaScript threw: `name`, `message` and `stack` (when known)."""

    def __init__(
        self,
        name: str,
        message: str,
        inner: BaseException | None = None,
        stack: str | None = None,
    ) -> None:
        super().__init__(f"{name}: {message}", inner)
        self.name = name
        self.message = message
        self.stack = stack

    def traceback(self) -> list[str]:
        """The JavaScript stack's frame lines (empty when the worker reported none)."""
        if not self.stack:
            return []
        return [
            ln.strip() for ln in self.stack.splitlines() if ln.strip().startswith("at ")
        ]

    def display(
        self, format: Literal["traceback", "type-msg", "msg"] = "traceback"
    ) -> str:  # noqa: A002 - Monty's name
        """``'traceback'``: the stack (or ``'type-msg'`` without one); ``'type-msg'``:
        ``Name: message``; ``'msg'``: the message alone."""
        if format == "msg":
            return self.message
        if format == "type-msg" or not self.stack:
            return f"{self.name}: {self.message}"
        return self.stack


class PydenoRuntimeError(_GuestError):
    """The feed's JavaScript threw (or its result was too large). The session survives."""


class PydenoSyntaxError(_GuestError):
    """The feed is not valid JavaScript; nothing of it ran. The session survives."""


class PydenoCrashedError(PydenoError):
    """The worker is gone, and the session with it: killed over `max_memory`, a crash, a
    protocol violation, or a worker that could not start (on a machine without the OS sandbox,
    see `pydeno.sandbox_status()`). Check out a new session; the pool is unaffected."""

    timed_out = False

    @property
    def exit_status(self) -> int | None:
        """Always None: the worker's exit status is not reported through the session."""
        return None


class PydenoTimeoutError(PydenoCrashedError, TimeoutError):
    """A deadline killed the worker (`max_feed_duration_secs`, the CPU cap, or
    `max_host_wait_secs`). A `TimeoutError`; the session is over."""

    timed_out = True


class ToolThreadLimitError(PydenoError):
    """An external call was refused because a thread budget is spent: the pool's
    ``max_tool_threads`` (or the process-wide ceiling), typically held by tools of killed runs
    that have not returned yet. Raised by the feed whose call was refused if that feed then
    failed; always logged (logger ``pydeno``). The guest only saw its call fail like any other
    host error (``RuntimeError: host function failed``), nothing about the host. The session
    survives; other pools and sessions are unaffected."""


_JS_MESSAGE = re.compile(
    r"(?:Evaluation failed: )?(?:Uncaught )?([A-Za-z_$][\w$]{0,63}): ([\s\S]*)\Z"
)


def _js_parts(exc: BaseException) -> tuple[str, str, str | None]:
    text = str(exc)
    stack = getattr(exc, "stack", None)
    found = _JS_MESSAGE.match(text)
    if found:
        message = found.group(2)
        first, _, rest = message.partition("\n    at ")
        if rest and stack is None:
            stack = f"{found.group(1)}: {message}"
            message = first
        return found.group(1), message, stack if isinstance(stack, str) else None
    return "Error", text.removeprefix("Evaluation failed: "), None


def _is_js_syntax(exc: BaseException) -> bool:
    return isinstance(exc, JavaScriptError) and _js_parts(exc)[0] == "SyntaxError"


def _failure(exc: BaseException, *, compile_time: bool = False) -> PydenoError:
    """The `PydenoError` for what a feed failed with."""
    if isinstance(exc, PydenoError):
        return exc
    if isinstance(exc, JavaScriptError):
        name, message, stack = _js_parts(exc)
        cls = (
            PydenoSyntaxError
            if compile_time and name == "SyntaxError"
            else PydenoRuntimeError
        )
        return cls(name, message, exc, stack)
    if isinstance(exc, ResultTooLarge):
        return PydenoRuntimeError("ResultTooLarge", str(exc), exc)
    if isinstance(exc, RuntimeTimeout):
        return PydenoTimeoutError(str(exc), exc)
    if isinstance(exc, WorkerCrashed):
        return PydenoCrashedError(str(exc), exc)
    # Anything else the run failed with on the host side (a result that cannot cross the
    # boundary, say): the feed failed, the session goes on (`_ended` makes it a crash otherwise).
    return PydenoRuntimeError(type(exc).__name__, str(exc), exc)


def _ended(exc: BaseException, agent: Any) -> PydenoError:
    """`_failure`, except that whatever ended with the worker gone is a crash at least."""
    error = _failure(exc)
    if (
        not isinstance(error, PydenoCrashedError)
        and agent is not None
        and agent.is_closed()
    ):
        return PydenoCrashedError(str(error), exc)
    return error


def _start_failure(exc: BaseException, sandbox: str) -> PydenoCrashedError:
    hint = (
        " Run pydeno.sandbox_status() to see which OS sandbox layers this machine lacks. "
        "Pydeno(sandbox='auto') runs with whatever the platform offers, which is weaker "
        "containment for untrusted code (a documented risk, never the default)."
        if sandbox == "require" and "termination authority" not in str(exc)
        else ""
    )
    return PydenoCrashedError(f"could not start a sandboxed worker: {exc}.{hint}", exc)


# ---------------------------------------------------------------------------
# preparing a feed
# ---------------------------------------------------------------------------

_PUNCTUATORS = sorted(
    (
        ">>>= ... === !== **= <<= >>= >>> &&= ||= ??= => == != <= >= && || ?? ?. ++ -- += -= "
        "*= /= %= &= |= ^= ** << >> { } ( ) [ ] ; , < > + - * / % & | ^ ! ~ ? : = . @"
    ).split(),
    key=len,
    reverse=True,
)
_TOKEN = re.compile(
    r"(?P<nl>[\n\r  ])"
    r"|(?P<ws>[ \t\v\f﻿ ]+)"
    r"|(?P<lc>//[^\n\r  ]*)"
    r"|(?P<bc>/\*[\s\S]*?\*/)"
    r"|(?P<str>\"(?:[^\"\\\n\r]|\\[\s\S])*\"|'(?:[^'\\\n\r]|\\[\s\S])*')"
    r"|(?P<tpl>`)"
    r"|(?P<num>(?:0[xXoObB][0-9a-fA-F_]+n?"
    r"|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?n?))"
    r"|(?P<id>#?[A-Za-z_$\u0080-￿][\w$\u0080-￿]*)"
    r"|(?P<p>" + "|".join(re.escape(p) for p in _PUNCTUATORS) + ")"
)
_REGEX = re.compile(
    r"/(?:[^/\\\[\n\r]|\\[^\n\r]|\[(?:[^\]\\\n\r]|\\[^\n\r])*\])+/[A-Za-z]*"
)
_TEMPLATE = re.compile(r"(?:[^`\\$]|\\[\s\S]|\$(?!\{))*(`|\$\{)")
# After these, a `/` starts a regular expression rather than dividing.
_BEFORE_REGEX_WORDS = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split()
)
# Keywords that leave an expression (or a statement) unfinished at the end of a line.
_CONTINUING_WORDS = frozenset(
    "typeof new await void delete in instanceof of extends yield case else do".split()
)
# Keywords that cannot start a statement, only continue the one before.
_CONTINUATION_WORDS = frozenset({"in", "instanceof", "else", "catch", "finally"})
# A punctuator at the start of a line that begins a new statement (any other continues the last).
_STATEMENT_PUNCTUATORS = frozenset({"{", "!", "~", "++", "--", ";"})
_CONTROL_WORDS = frozenset({"if", "for", "while", "with"})
_ASSIGNMENTS = frozenset(
    "= += -= *= /= %= **= <<= >>= >>>= &= |= ^= &&= ||= ??=".split()
)
_DECLARATIONS = frozenset({"var", "let", "const", "function", "class", "async"})
_NOT_EXPRESSION = frozenset(
    "var let const function class if for while do switch try throw return break continue "
    "export debugger with else case default catch finally yield enum".split()
)
_EXPRESSION_PUNCTUATORS = frozenset({"(", "[", "!", "~", "+", "-", "++", "--"})


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str
    text: str
    start: int
    end: int
    nl: bool  # a line break separates it from the top-level token before it


def _top_level_tokens(code: str) -> list[_Tok] | None:
    """The tokens of `code` outside any bracket, or None for code this scanner does not fully
    understand (the feed then runs unchanged)."""
    out: list[_Tok] = []
    depth = 0
    # Each open `${`: the depth outside it, where its template began, and that template's `nl`.
    templates: list[tuple[int, int, bool]] = []
    prev: tuple[str, str] | None = None  # the last significant token, any depth
    nl = False
    pos = 0
    n = len(code)
    if code.startswith("#!"):
        pos = code.find("\n")
        pos = n if pos < 0 else pos
    while pos < n:
        ch = code[pos]
        if ch == "/" and (
            prev is None
            or (prev[0] == "p" and prev[1] not in (")", "]", "}", "++", "--"))
            or (prev[0] == "id" and prev[1] in _BEFORE_REGEX_WORDS)
        ):
            if code.startswith("//", pos) or code.startswith("/*", pos):
                m = _TOKEN.match(code, pos)
            else:
                m = _REGEX.match(code, pos)
                if m is None:
                    return None
                if depth == 0:
                    out.append(_Tok("regex", m.group(), pos, m.end(), nl))
                    nl = False
                prev = ("regex", "")
                pos = m.end()
                continue
        else:
            m = _TOKEN.match(code, pos)
        if m is None:
            return None
        kind = m.lastgroup
        text = m.group()
        end = m.end()
        if kind == "nl":
            nl = True
        elif kind == "bc":
            nl = nl or any(c in text for c in "\n\r  ")
        elif kind in ("ws", "lc"):
            pass
        elif kind == "tpl" or (
            kind == "p" and text == "}" and templates and templates[-1][0] == depth - 1
        ):
            # A template literal starts, or the `}` closing one of its `${...}` parts.
            if kind == "tpl":
                outer, start, start_nl = depth, pos, nl
            else:
                outer, start, start_nl = templates.pop()
                depth = outer
            t = _TEMPLATE.match(code, end)
            if t is None:
                return None
            end = t.end()
            if t.group(1) == "${":
                templates.append((outer, start, start_nl))
                depth = outer + 1
                prev = ("p", "${")
            else:
                if outer == 0:
                    out.append(_Tok("tpl", code[start:end], start, end, start_nl))
                    nl = False
                prev = ("tpl", "")
        else:
            if kind == "p" and text in ("(", "[", "{"):
                if depth == 0:
                    out.append(_Tok(kind, text, pos, end, nl))
                    nl = False
                depth += 1
            elif kind == "p" and text in (")", "]", "}"):
                depth -= 1
                if depth < 0:
                    return None
                if depth == 0:
                    out.append(_Tok(kind, text, pos, end, False))
                    nl = False
            elif depth == 0:
                out.append(_Tok(kind, text, pos, end, nl))
                nl = False
            prev = (kind, text)
        pos = end
    if depth != 0 or templates:
        return None
    return out


def _continues(before: _Tok, after: _Tok) -> bool:
    """Does `after` continue the statement `before` ended (no automatic semicolon between)?"""
    if before.kind == "p" and before.text not in (")", "]", "}", "++", "--"):
        return True
    if before.kind == "id" and before.text in _CONTINUING_WORDS:
        return True
    if after.kind == "p":
        return after.text not in _STATEMENT_PUNCTUATORS
    if after.kind == "tpl":
        return True
    return after.kind == "id" and after.text in _CONTINUATION_WORDS


def _completion(code: str) -> str:
    """`code` with its last statement turned into ``return (...)`` when that statement is an
    expression, so the feed's result is its trailing expression, as in a REPL (and in Monty).
    Code this does not fully understand is returned unchanged."""
    toks = _top_level_tokens(code)
    if not toks:
        return code
    while toks and toks[-1].kind == "p" and toks[-1].text == ";":
        toks.pop()
    if not toks:
        return code
    start = 0
    # Top-level declarations that follow a `;` on the same line: moved to a line of their own,
    # where the session finds and keeps them (it looks for declarations at the start of a line).
    breaks: list[int] = []
    for i in range(1, len(toks)):
        before, after = toks[i - 1], toks[i]
        if before.kind == "p" and before.text == ";":
            start = i
            if not after.nl and after.kind == "id" and after.text in _DECLARATIONS:
                breaks.append(after.start)
        elif (
            after.nl or (before.kind == "p" and before.text == "}")
        ) and not _continues(before, after):
            if before.text == ")" and before.kind == "p" and i >= 3:
                # `if (...)` / `for (...)` / `while (...)` + a line break: the statement after it
                # is the body, not a statement of its own (`for (...)\n f()` must loop).
                word = toks[i - 3]
                if word.kind == "id" and (
                    word.text in _CONTROL_WORDS
                    or (word.text == "await" and i >= 4 and toks[i - 4].text == "for")
                ):
                    continue
            start = i
    inserts = [(pos, "\n") for pos in breaks]
    if _is_expression(toks, start):
        inserts += [(toks[start].start, "return ("), (toks[-1].end, ");")]
    if not inserts:
        return code
    parts, last = [], 0
    for pos, text in sorted(inserts):
        parts += [code[last:pos], text]
        last = pos
    parts.append(code[last:])
    return "".join(parts)


def _is_expression(toks: list[_Tok], start: int) -> bool:
    """Is the statement starting at ``toks[start]`` an expression statement whose value is the
    feed's result? Not a top-level assignment: as in Monty, ``x = 1`` has no result."""
    if any(t.kind == "p" and t.text in _ASSIGNMENTS for t in toks[start:]):
        return False
    first = toks[start]
    nxt = toks[start + 1].text if start + 1 < len(toks) else None
    if first.kind == "id":
        if first.text in _NOT_EXPRESSION or first.text.startswith("#"):
            return False
        if first.text == "import" and nxt not in ("(", "."):
            return False
        if first.text == "async" and nxt == "function":
            return False
        return nxt != ":"  # a label
    return first.kind != "p" or first.text in _EXPRESSION_PUNCTUATORS


def _check_name(name: Any, what: str) -> str:
    ToolBridge._check_name(name, what=what)  # noqa: SLF001
    if name in _JS_RESERVED or name.startswith(_RESERVED_PREFIX):
        raise ValueError(f"{what} {name!r} is reserved")
    return name


def _plain(value: Any, depth: int = 0) -> Any:
    """`value` as JSON data, or TypeError: `inputs` cross into the sandbox as plain data only."""
    if depth > _MAX_INPUT_DEPTH:
        raise TypeError("an input is nested too deeply")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise TypeError(
                "an input integer is outside JavaScript's safe range (2**53 - 1)"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("an input float must be finite")
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("input dict keys must be strings")
            out[key] = _plain(item, depth + 1)
        return out
    raise TypeError(
        f"inputs must be plain data (None, bool, int, float, str, list, dict), "
        f"not {type(value).__name__}"
    )


def _setup(inputs: Mapping[str, Any] | None, names: tuple[str, ...]) -> str:
    """One line of JavaScript, run before the feed's code: the stubs for `external_lookup`'s
    callables and the globals for `inputs` (which win over a stub of the same name)."""
    parts = []
    if inputs:
        if not isinstance(inputs, Mapping):
            raise TypeError("inputs must be a mapping of name -> value")
        for name, value in inputs.items():
            _check_name(name, "input name")
            text = json.dumps(_plain(value), allow_nan=False, separators=(",", ":"))
            parts.append(f"globalThis.{name}=JSON.parse({json.dumps(text)});")
    stubs = [n for n in names if not inputs or n not in inputs]
    if stubs:
        parts.append(
            f"{{const d=globalThis.{_EXTERNAL};"
            + "".join(
                f"globalThis.{n}=function {n}(...a){{return d({json.dumps(n)},...a)}};"
                for n in stubs
            )
            + "}"
        )
    return "".join(parts) + "\n" if parts else ""


def _check_lookup(
    external_lookup: Mapping[str, Any] | None, *, sync: bool
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """(callables by name, every name to stub); non-callable values become `inputs`."""
    if external_lookup is None:
        return {}, ()
    if not isinstance(external_lookup, Mapping):
        raise TypeError(
            "external_lookup must be a mapping of name -> callable or value"
        )
    calls = {}
    for name, value in external_lookup.items():
        _check_name(name, "external_lookup name")
        if callable(value):
            if sync and inspect.iscoroutinefunction(value):
                raise RuntimeError(
                    f"external function {name!r} is async; a PydenoSession cannot await it. "
                    "Use AsyncPydeno for async external functions."
                )
            calls[name] = value
    return calls, tuple(calls)


def _values(external_lookup: Mapping[str, Any] | None) -> dict[str, Any]:
    if not external_lookup:
        return {}
    return {k: v for k, v in external_lookup.items() if not callable(v)}


@dataclass(frozen=True, slots=True)
class _Prepared:
    source: str
    fallback: str | None  # the unrewritten feed, if `source` rewrote its last statement


def _prepare(
    code: str,
    inputs: Mapping[str, Any] | None,
    external_lookup: Mapping[str, Any] | None,
    names: tuple[str, ...],
) -> _Prepared:
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    values = _values(external_lookup)
    if values:
        inputs = {**values, **(inputs or {})}
    prefix = _setup(inputs, names)
    body = _completion(code)
    if body is code:
        return _Prepared(prefix + code, None)
    return _Prepared(prefix + body, prefix + code)


def _compile_check(source: str) -> str:
    """JavaScript answering whether `source` compiles as a feed (true), or is a SyntaxError.

    It calls the session prelude's checker, which uses only intrinsics captured before any guest
    code ran (a bare identifier: the global is non-configurable, so nothing can shadow it). Built
    from `Object.getPrototypeOf` and `instanceof` at call time, the check ran guest code: a guest
    could make a feed that ran look like one that never parsed, or change its own state outside
    the journal."""
    return f"{_COMPILES}({json.dumps(source)})"


def _unpack(step: ToolCall) -> tuple[str, tuple[Any, ...]] | None:
    if not step.args or not isinstance(step.args[0], str) or len(step.args[0]) > 64:
        return None
    return step.args[0], step.args[1:]


def _not_available(name: str | None) -> Exception:
    shown = name if name is not None else "?"
    return _public(
        ReferenceError(
            f"{shown} is not defined (it is not in this feed's external_lookup)"
        )
    )


def _output(value: Any) -> Any:
    return None if isinstance(value, JsUndefined) else value


def _external_result(
    result: Any, value: Any, error: BaseException | None
) -> tuple[Any, BaseException | None]:
    """Monty's `resume(result)` (``{'return_value': v}``, ``{'exception': e}``, ``{'exc_type':
    name, 'message': m}``) or pydeno's ``resume(value=...)`` / ``resume(error=...)``."""
    if result is not _MISSING:
        if value is not _MISSING or error is not None:
            raise TypeError("resume() takes a result dict or value=/error=, not both")
        if not isinstance(result, Mapping) or len(result) == 0:
            raise TypeError(
                "resume(result) takes {'return_value': v}, {'exception': e} or "
                "{'exc_type': name, 'message': m}; or use resume(value=...) / resume(error=...)"
            )
        if "return_value" in result:
            return result["return_value"], None
        if "exception" in result:
            return _MISSING, result["exception"]
        if "exc_type" in result:
            name = str(result["exc_type"]).rsplit(".", 1)[-1]
            return _MISSING, _error_class(name)(str(result.get("message", "")))
        if "future" in result:
            raise ValueError(
                "pending futures are not supported; answer with a value or error"
            )
        raise TypeError(f"unknown resume result keys {sorted(result)}")
    if (value is _MISSING) == (error is None):
        raise TypeError("resume() takes exactly one of value= or error=")
    return value, error


def _checked_answer(
    result: Any, value: Any, error: BaseException | None
) -> tuple[Any, BaseException | None]:
    """`_external_result`, refusing an answer the session would refuse, before the snapshot is
    used up by it (a refused answer must leave the snapshot resumable)."""
    value, error = _external_result(result, value, error)
    if error is not None and not isinstance(error, Exception):
        raise TypeError("the error to resume with must be an Exception instance")
    return value, error


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------


def _default_print(stream: str, text: str) -> None:
    """Monty's default (the host's stdout/stderr), minus control characters, bidirectional
    overrides and invisible characters: guest text must not drive (or disguise itself on) the
    host's terminal."""
    target = sys.stdout if stream == "stdout" else sys.stderr
    target.write(_CONTROL.sub("?", text))


class _Printer:
    """The session's console sink: the feed's ``print_callback(stream, text)``, or nothing
    (between feeds, and while a loaded session replays)."""

    __slots__ = ("callback",)

    def __init__(self) -> None:
        self.callback: Callable[[str, str], Any] | None = None

    def __call__(self, level: str, args: list[Any]) -> None:
        callback = self.callback
        if callback is None:
            return
        text = " ".join(format_console_arg(a) for a in args) + "\n"
        callback("stdout" if level in _STDOUT_LEVELS else "stderr", text)


#: What the default printer writes to the host's stdout/stderr per feed before it stops.
DEFAULT_PRINT_LIMIT_BYTES = 1024 * 1024


class _CappedDefaultPrint:
    """The default printer for one feed: `_default_print` until `DEFAULT_PRINT_LIMIT_BYTES` of
    UTF-8 have been written, then one ``[truncated]`` line and nothing more. A guest could
    otherwise write without limit to the host's stdout (measured: ~150 MB in 2 s), which is often a
    log pipeline. An explicit ``print_callback`` gets everything and is not capped."""

    __slots__ = ("_left", "_done")

    def __init__(self) -> None:
        self._left = DEFAULT_PRINT_LIMIT_BYTES
        self._done = False

    def __call__(self, stream: str, text: str) -> None:
        if self._done:
            return
        size = len(text.encode("utf-8", "replace"))
        if size <= self._left:
            self._left -= size
            _default_print(stream, text)
            return
        # Leave room for the newline that ends the cut line, so the total stays within the cap.
        cut = max(0, self._left - 1)
        head = text.encode("utf-8", "replace")[:cut].decode("utf-8", "ignore")
        self._done = True
        _default_print(
            stream, head + ("\n" if head and not head.endswith("\n") else "")
        )
        _default_print(stream, "[truncated]\n")


def _printer_for(
    print_callback: Callable[[str, str], Any] | None,
) -> Callable[[str, str], Any]:
    if print_callback is None:
        return _CappedDefaultPrint()
    if not callable(print_callback):
        raise TypeError("print_callback must be a callable (stream, text)")
    return print_callback


def _drop_console(level: str, args: list[Any]) -> None:
    """Pooled workers route console output to the parent; a session installs its own sink."""


def _external_placeholder(name: str, *args: Any) -> Any:
    raise RuntimeError("external functions are answered by the Pydeno session")


# The front door's dispatcher is the one tool allowed a reserved `__pydeno` name.
_external_placeholder._pydeno_internal = True  # type: ignore[attr-defined]


# The one runtime config every front-door worker is built with.
_CONFIG = RuntimeConfig(on_console=_drop_console)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


class PydenoComplete:
    """A feed started with `feed_start` ran to completion (Monty's `MontyComplete`)."""

    __slots__ = ("output",)

    def __init__(self, output: Any) -> None:
        #: The feed's result (its trailing expression, or what it returned; None for none).
        self.output = output

    def __repr__(self) -> str:
        return f"PydenoComplete(output={self.output!r})"


class PydenoSnapshot:
    """A feed suspended at a call to an external function (Monty's `FunctionSnapshot`).

    `function_name` and `args` are what the guest passed: untrusted data, validate before acting
    on them. Answer exactly once, with `resume(...)` or `resume_auto()`; either returns the next
    `PydenoSnapshot` or a `PydenoComplete`, or raises the feed's `PydenoError`."""

    __slots__ = (
        "_call",
        "_declared",
        "_lookup",
        "_session",
        "_used",
        "args",
        "call_id",
        "function_name",
    )

    def __init__(
        self,
        session: PydenoSession,
        call: ToolCall,
        name: str,
        args: tuple[Any, ...],
        lookup: dict[str, Any],
        declared: frozenset[str] | None = None,
    ) -> None:
        self._session = session
        self._call = call
        self._lookup = lookup
        # The functions the feed declared (None: not known, after `load_snapshot` without
        # `external_lookup`); calls to any other name are refused, never surfaced.
        self._declared = declared
        self._used = False
        self.function_name = name
        self.args = args
        self.call_id = call.call_id

    @property
    def kwargs(self) -> dict[str, Any]:
        """Always empty: JavaScript calls are positional."""
        return {}

    def _take(self) -> None:
        if self._used:
            raise RuntimeError("this snapshot has already been resumed")
        self._used = True

    def resume(
        self,
        result: Any = _MISSING,
        /,
        *,
        value: Any = _MISSING,
        error: BaseException | None = None,
    ) -> PydenoSnapshot | PydenoComplete:
        """Answer the call: ``resume(value=v)`` / ``resume(error=exc)``, or Monty's
        ``resume({'return_value': v})`` / ``resume({'exception': exc})``. An error reaches the
        guest as an Error named after its class, its message redacted."""
        value, error = _checked_answer(result, value, error)
        self._take()
        return self._session._answer(self, value, error)  # noqa: SLF001

    def resume_auto(self) -> PydenoSnapshot | PydenoComplete:
        """Answer the call from the feed's `external_lookup`. A name not in it throws a
        `ReferenceError` in the guest; an async function raises `RuntimeError` (use
        `AsyncPydeno`)."""
        fn = self._lookup.get(self.function_name)
        if fn is not None and inspect.iscoroutinefunction(fn):
            raise RuntimeError(
                f"external function {self.function_name!r} is async; use AsyncPydeno"
            )
        self._take()
        session = self._session
        value, error = session._bounded_external(fn, self.function_name, self.args)  # noqa: SLF001
        return session._answer(self, value, error)  # noqa: SLF001

    def dump(self, *, associated_data: bytes = b"") -> bytes:
        """The suspended session, signed (see `PydenoSession.dump`); restore it with
        `PydenoSession.load_snapshot`."""
        return self._session.dump(associated_data=associated_data)

    def __repr__(self) -> str:
        return (
            f"PydenoSnapshot(function_name={self.function_name!r}, args={self.args!r}, "
            f"call_id={self.call_id})"
        )


# ---------------------------------------------------------------------------
# the pool
# ---------------------------------------------------------------------------


# How often a caller waiting on an external checks that the worker is still alive.
_POLL = 0.05
# How long a filler waits before replacing a checked-out worker while another is still ready.
_REFILL_DELAY = 0.05


def _fresh_seed() -> int:
    return secrets.randbelow(2**31)


class _Core(_sandbox_pool._Core):  # noqa: SLF001
    """`SandboxPool`'s core, except that every worker gets its own random seed (a session adopts
    it; `Math.random` must not repeat across sessions) and arrives with the session's
    dispatcher and prelude installed (`preinstall`), so a checkout does no round trip."""

    def new(self, session: dict[str, Any] | None = None) -> IsolatedRuntime:
        if session is None and self.ready:
            # A filler replacing a worker just checked out. Starting a process forks this one
            # and holds the GIL for about a millisecond, right when the new session runs its
            # first feed; a pool that still has a worker ready can wait a moment.
            with self.cond:
                self.cond.wait_for(lambda: self.closed, timeout=_REFILL_DELAY)
            if self.closed:
                raise RuntimeError("the pool is closed")
        rt = IsolatedRuntime(
            self.config,
            prewarm=False,
            random_seed=_fresh_seed(),
            **self.spawn,
            **(self.session if session is None else session),
        )
        try:
            # Everything a session needs that does not depend on it: the dispatcher binding and
            # the session prelude (also the worker's first command, which is the slow one).
            preinstall(rt, [_EXTERNAL])
        except BaseException:
            rt.close()
            raise
        return rt


class _Pool(_sandbox_pool.SandboxPool):
    _core_type = _Core


class _Reaper:
    """Waits for the workers of ended sessions to exit, on one thread per pool, so that a
    session's exit costs a SIGKILL and not the kernel's teardown of the process (milliseconds).
    The worker is killed before the session's exit returns; only the bookkeeping waits."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[IsolatedRuntime | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def stop(self) -> None:
        """Let the thread finish what is queued and exit. Workers of sessions that end later
        are killed and reaped on their own thread. Idempotent."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self._thread is not None and self._thread.is_alive():
                self._queue.put(None)

    def kill(self, rt: IsolatedRuntime) -> None:
        if rt.is_closed():
            return
        if os.getpid() != rt._owner_pid or self._stopped:  # noqa: SLF001
            # A fork()ed child (not ours to signal), or a closed pool: no reaper thread.
            if os.getpid() == rt._owner_pid:  # noqa: SLF001
                rt._kill()  # noqa: SLF001
            else:
                rt.close()
            return
        proc = rt._proc  # noqa: SLF001
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass
        rt._closed = True  # noqa: SLF001 - nothing may be sent to it any more
        with self._lock:
            if self._stopped:  # closed meanwhile: reap here
                rt._reap()  # noqa: SLF001
                return
            self._queue.put(rt)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="pydeno-front-reaper", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            rt = self._queue.get()
            if rt is None:
                return
            try:
                rt._reap()  # noqa: SLF001
            except Exception:  # noqa: BLE001, S110 - a reaper must not die of one worker
                pass


def _tool_budget(max_tool_threads: int) -> _ThreadBudget:
    max_tool_threads = limit_int("max_tool_threads", max_tool_threads, minimum=1)
    ceiling = _PROCESS_THREADS.limit
    if max_tool_threads > ceiling:
        warnings.warn(
            f"max_tool_threads={max_tool_threads} is above the process ceiling ({ceiling}); "
            f"clamped to {ceiling}",
            RuntimeWarning,
            stacklevel=3,
        )
        max_tool_threads = ceiling
    return _ThreadBudget(max_tool_threads, "this pool", parent=_PROCESS_THREADS)


# The open pools' budgets (to warn when their caps add up to more than the process ceiling).
_OPEN_POOLS: set[_ThreadBudget] = set()
_OPEN_POOLS_LOCK = threading.Lock()


def _open_pool(budget: _ThreadBudget) -> None:
    """Warn if this pool's cap takes the open pools past the ceiling, then count it as open.
    Called before the pool starts anything: if the warning is raised as an error (``-W error``),
    nothing has been registered or started."""
    ceiling = _PROCESS_THREADS.limit
    with _OPEN_POOLS_LOCK:
        total = sum(b.limit for b in _OPEN_POOLS) + budget.limit
    if total > ceiling:
        warnings.warn(
            f"the open pools' max_tool_threads add up to {total}, above the process ceiling "
            f"({ceiling}): a pool's cap is an upper bound, not a reservation, so a pool may be "
            "refused before reaching its own cap while others hold the threads",
            RuntimeWarning,
            stacklevel=4,
        )
    with _OPEN_POOLS_LOCK:
        _OPEN_POOLS.add(budget)


def _close_pool(budget: _ThreadBudget) -> None:
    with _OPEN_POOLS_LOCK:
        _OPEN_POOLS.discard(budget)


def _check_pool_arguments(
    min_processes: int,
    sandbox: str,
    jitless: bool,
    dump_key: bytes | None,
    strict_eval: bool = False,
) -> bytes:
    # A session freezes the guest's clock with the worker's own script (`_SessionBase._install`):
    # import it now, not inside the first checkout.
    from . import _worker  # noqa: F401, PLC0415

    if sandbox not in ("require", "auto", "off"):
        raise ValueError("sandbox must be 'require' (the default), 'auto' or 'off'")
    if not isinstance(jitless, bool):
        raise TypeError("jitless must be a bool")
    if not isinstance(strict_eval, bool):
        raise TypeError("strict_eval must be a bool")
    if min_processes is None:
        raise TypeError("min_processes must be a positive int")
    limit_int("min_processes", min_processes, minimum=1)
    if dump_key is None:
        return secrets.token_bytes(32)
    if not isinstance(dump_key, (bytes, bytearray)) or len(dump_key) < 16:
        raise ValueError("dump_key must be at least 16 bytes")
    return bytes(dump_key)


class Pydeno:
    """A pool of pre-started, OS-sandboxed JavaScript workers (Monty's `Monty`).

    ```python
    with Pydeno() as pool:
        with pool.checkout() as session:
            assert session.feed_run("1 + 1") == 2
    ```

    Args:
        min_processes: Workers kept started and ready (default 2: enough that back-to-back
            checkouts find one ready while the next starts in the background, at ~30-40 MB of
            memory each). A checkout that finds none ready starts one on the spot (a cold start,
            tens of milliseconds): exhaustion is never an error and never waits for a return.
            Workers are single-use, so there is no ``max_processes``: a session's worker dies
            with the session.
        limits: Default `PydenoLimits` for every session (each key overrides the built-in
            default; ``checkout(limits=...)`` overrides these).
        sandbox: ``"require"`` (default): refuse to start unless every OS sandbox layer the
            platform has is in force (macOS Seatbelt; Linux Landlock + seccomp), raising
            `PydenoCrashedError` that points at `pydeno.sandbox_status()`. ``"auto"`` runs with
            whatever the platform offers and ``"off"`` with none: weaker containment for untrusted
            code, a risk you take explicitly.
        jitless: Run V8 without its JIT compiler or WebAssembly (default True), which removes the
            largest class of V8 exploits. ``False`` is faster on heavy compute and is a risk you
            take explicitly.
        strict_eval: Forbid code generation from strings in the guest (default False): ``eval``
            and ``new Function`` throw ``EvalError``. Every worker of the pool gets it, and
            `dump()` records it: a dump made with it loads only into a pool with it, and the
            other way round. See `IsolatedRuntime(strict_eval=...)`.
        dump_key: The key `dump()` signs session state with (HMAC-SHA256, at least 16 bytes) and
            `load_session` / `load_snapshot` check. Default: a random key per `Pydeno`, so state
            loads only into the pool that dumped it; pass your own (from a secret store) to load
            it in another process.
        max_tool_threads: Most session tool threads this pool's sessions may hold at once
            (default 128). A session gets its own threads at its first external call: two for
            a `PydenoSession` (its loop and its tool thread), one for an `AsyncPydenoSession`;
            they end with the session, except that a tool that never returns keeps its thread
            after its run is killed. Past the cap, an external call that needs a thread is
            refused (journaled and charged like a failed tool call; a generic failure for the
            guest; `ToolThreadLimitError` for the host). Clamped to the process ceiling
            (`pydeno._agent.MAX_TOOL_THREADS`, 512), which every pool draws from: a cap is an
            upper bound, not a reservation (a warning says so when open pools' caps exceed it).

    The first worker starts in the constructor (a platform that cannot sandbox fails here, not
    at the first checkout) and the rest in the background, so the first checkout is fast. Use
    `with` or call `close()`; sessions already checked out are not affected by either.
    """

    def __init__(
        self,
        *,
        min_processes: int = DEFAULT_MIN_PROCESSES,
        limits: PydenoLimits | None = None,
        sandbox: Literal["require", "auto", "off"] = "require",
        jitless: bool = True,
        strict_eval: bool = False,
        dump_key: bytes | None = None,
        max_tool_threads: int = DEFAULT_MAX_TOOL_THREADS,
    ) -> None:
        self._key = _check_pool_arguments(
            min_processes, sandbox, jitless, dump_key, strict_eval
        )
        self._budget = _tool_budget(max_tool_threads)
        self._limits_in = limits
        self._limits = _resolve_limits(limits)
        self._sandbox = sandbox
        self._spawn = {
            "sandbox": sandbox,
            "jitless": jitless,
            "strict_eval": strict_eval,
            "max_memory": self._limits.max_memory,
        }
        _open_pool(
            self._budget
        )  # may warn (or raise, under -W error): before anything starts
        try:
            self._reaper = _Reaper()
            self._pool = _Pool(_CONFIG, size=min_processes, **self._spawn)
        except WorkerCrashed as exc:
            _close_pool(self._budget)
            raise _start_failure(exc, sandbox) from exc
        except BaseException:
            _close_pool(self._budget)
            raise
        weakref.finalize(self, _close_pool, self._budget)
        weakref.finalize(self, self._reaper.stop)  # a pool dropped without close()

    def __enter__(self) -> Pydeno:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Kill the workers still waiting in the pool, and stop its threads. Idempotent."""
        self._pool.close()
        self._reaper.stop()
        _close_pool(self._budget)

    def checkout(
        self, *, script_name: str = "main.js", limits: PydenoLimits | None = None
    ) -> PydenoSession:
        """A session served by one dedicated worker, checked out by ``with`` on the returned
        session and killed when the ``with`` block exits (a worker never serves twice).

        Args:
            script_name: A name for the session's code (kept as `PydenoSession.script_name`).
            limits: `PydenoLimits` for this session, over the pool's.
        """
        resolved = (
            self._limits if limits is None else _resolve_limits(self._limits_in, limits)
        )
        return PydenoSession(self, script_name, resolved)

    def stats(self) -> dict[str, Any]:
        """The pool's `SandboxPool.stats()`: ``size``, ``ready``, ``starting``, ``checkouts``,
        ``cold_starts`` and ``last_error``."""
        return self._pool.stats()

    @staticmethod
    def sandbox_status() -> Any:
        """`pydeno.sandbox_status()`: what this machine's OS sandbox can apply, and why a
        ``sandbox="require"`` pool would or would not start."""
        from ._status import sandbox_status  # noqa: PLC0415

        return sandbox_status()

    # -- for sessions --------------------------------------------------------

    def _runtime(self, limits: _Limits, seed: int | None = None) -> IsolatedRuntime:
        try:
            if seed is None and limits.max_memory == self._limits.max_memory:
                return self._pool.checkout()
            options = {**self._spawn, "max_memory": limits.max_memory}
            return IsolatedRuntime(
                _CONFIG,
                random_seed=_fresh_seed() if seed is None else seed,
                **options,
            )
        except WorkerCrashed as exc:
            raise _start_failure(exc, self._sandbox) from exc

    def _agent(self, limits: _Limits, printer: _Printer) -> AgentSandbox:
        rt = self._runtime(limits)
        try:
            agent = AgentSandbox(
                {_EXTERNAL: _external_placeholder},
                runtime=rt,
                max_tool_calls=limits.max_tool_calls,
                timeout=limits.timeout,
                max_pause=limits.max_pause,
            )
        except BaseException:
            rt.close()
            raise
        agent._core.console.user = printer  # noqa: SLF001
        agent._core.tools.budget = self._budget  # noqa: SLF001
        return agent

    def _load(
        self, state: bytes, limits: _Limits, associated_data: bytes = b""
    ) -> AgentSandbox:
        seed = _journal_seed(
            state, self._key, self._spawn["strict_eval"], associated_data
        )
        rt = self._runtime(limits, seed)
        try:
            agent = AgentSandbox.load(
                state,
                self._key,
                {_EXTERNAL: _external_placeholder},
                associated_data=associated_data,
                runtime=rt,
                timeout=limits.timeout,
                max_pause=limits.max_pause,
            )
        except BaseException as exc:
            rt.close()
            raise _load_failure(exc) from exc
        agent._core.tools.budget = self._budget  # noqa: SLF001
        return agent


def _journal_seed(
    state: bytes, key: bytes, strict_eval: bool, associated_data: bytes = b""
) -> int:
    """The state's random seed, checked before a worker is started for it (also its
    ``strict_eval``, which `AgentSandbox.load` checks again on the worker it gets)."""
    try:
        journal = _open_journal(state, key, associated_data, DEFAULT_MAX_JOURNAL_BYTES)
    except Exception as exc:  # noqa: BLE001
        raise _load_failure(exc) from exc
    recorded = journal["config"].get("strict_eval", False)
    if recorded != strict_eval:
        raise _load_failure(
            JournalError(
                f"the journal was recorded with strict_eval={recorded}; load it into a "
                f"session with strict_eval={recorded}, not {strict_eval}"
            )
        )
    seed = journal["config"].get("random_seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
        raise PydenoError("cannot load this state: it has no valid random seed")
    return seed


def _load_failure(exc: BaseException) -> BaseException:
    if isinstance(exc, (PydenoError, KeyboardInterrupt, SystemExit)) or not isinstance(
        exc, Exception
    ):
        return exc
    if isinstance(exc, WorkerCrashed):
        return PydenoCrashedError(str(exc), exc)
    return PydenoError(f"cannot load this state: {type(exc).__name__}: {exc}", exc)


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


def _exclusive(method: Callable[..., Any]) -> Callable[..., Any]:
    """One feed (or dump, or load) at a time per session. A second caller gets a `PydenoError`
    and touches nothing the first one is using (its printer, its run)."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not self._busy.acquire(blocking=False):
            raise PydenoError(
                "the session is busy (another thread is feeding it); use one session per thread"
            )
        try:
            return method(self, *args, **kwargs)
        finally:
            self._busy.release()

    return wrapper


class PydenoSession:
    """A REPL session on one dedicated, single-use worker (Monty's `MontySession`).

    Obtained from `Pydeno.checkout()` and used as a context manager. Globals and functions persist
    across feeds: a top-level ``const``/``let``/``var``/``function``/``class`` (at the start of a
    line or after a ``;``) is kept for later feeds, as is anything put on ``globalThis``. A feed may
    ``await``.

    Use it from one thread at a time. External functions and ``print_callback`` are called in this
    process (``print_callback`` on the session's own thread)."""

    def __init__(self, pool: Pydeno, script_name: str, limits: _Limits) -> None:
        self._pool = pool
        self.script_name = script_name
        self._limits = limits
        self._agent: AgentSandbox | None = None
        self._printer = _Printer()
        self._entered = False
        self._busy = threading.Lock()

    def __enter__(self) -> PydenoSession:
        if self._entered:
            raise RuntimeError(
                "a PydenoSession is entered once: each checkout is a fresh, single-use worker"
            )
        self._entered = True
        self._agent = self._pool._agent(self._limits, self._printer)  # noqa: SLF001
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Kill the session's worker (it is never reused). Idempotent."""
        agent = self._agent
        if agent is not None and _TOOL_OF.get() == agent._core.session_id:  # noqa: SLF001
            raise RuntimeError(
                "an external function cannot close the session that called it"
            )
        self._agent = None
        if agent is not None:
            # SIGKILL at once instead of asking a worker that will never run again to exit
            # cleanly (which costs its interpreter's teardown, waited for by the caller).
            self._pool._reaper.kill(agent._core.rt)  # noqa: SLF001
            agent.close()

    def _live(self) -> AgentSandbox:
        agent = self._agent
        if agent is None:
            raise RuntimeError(
                "the session is not checked out: use `with pool.checkout() as session:`"
                if not self._entered
                else "the session is closed"
            )
        if agent.is_closed():
            raise PydenoCrashedError(
                "the session's worker is gone (crashed, killed or timed out); check out a new "
                "session"
            )
        return agent

    def _dumpable(self) -> Any:
        """The agent, even with its worker gone: its journal as of the last good feed is what
        a crashed, timed-out or cancelled session is recovered from."""
        if self._agent is None:
            self._live()  # raises
        return self._agent

    @property
    def worker_pid(self) -> int | None:
        """The worker's process id (None when no worker is attached)."""
        agent = self._agent
        if agent is None or agent.is_closed():
            return None
        return agent._core.rt._proc.pid  # noqa: SLF001

    # -- feeding -------------------------------------------------------------

    @_exclusive
    def feed_run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        external_lookup: dict[str, Any] | None = None,
        print_callback: Callable[[Literal["stdout", "stderr"], str], Any] | None = None,
    ) -> Any:
        """Run one feed to completion and return its result: the value of its trailing
        expression (or what it ``return``s; None when there is none).

        Args:
            code: JavaScript. It may ``await``; tools return promises, so ``await fetch(1)``.
            inputs: Globals assigned before the feed runs, from plain data (None, bool, int, float,
                str, list, dict). They persist, like any global.
            external_lookup: ``name -> callable`` the feed may call (as ``await name(...)``):
                each call is answered by calling it here with the guest's arguments (untrusted:
                validate them). A non-callable value is assigned like an input. Async callables
                are refused (`RuntimeError`): use `AsyncPydeno`. An exception reaches the guest
                as an Error named after its class, its message redacted.
            print_callback: Gets the feed's console output as ``(stream, text)``, ``stream``
                ``'stdout'`` (``log``/``info``/``debug``) or ``'stderr'``. Default: this
                process's stdout/stderr, with control characters replaced, at most 1 MiB per
                feed (then one ``[truncated]`` line). A callback you pass is not capped.

        Raises:
            PydenoRuntimeError: the code threw (the session survives).
            PydenoSyntaxError: the code does not parse (the session survives).
            PydenoTimeoutError: a deadline killed the worker (the session is over).
            PydenoCrashedError: the worker is gone (the session is over).
        """
        agent = self._live()
        calls, names = _check_lookup(external_lookup, sync=True)
        prepared = _prepare(code, inputs, external_lookup, names)
        self._printer.callback = _printer_for(print_callback)
        agent._core.refused = None  # noqa: SLF001

        def answer(call: ToolCall) -> Any:
            unpacked = _unpack(call)
            if unpacked is None:
                raise _not_available(None)
            value, error = self._call_external(calls.get(unpacked[0]), *unpacked)
            if error is not None:
                raise error
            return value

        try:
            # Driven on this thread: each external call is answered as it arrives.
            return self._finish(
                self._start(agent, prepared, lambda src: agent._drive(src, answer))  # noqa: SLF001
            )
        finally:
            self._printer.callback = None

    @_exclusive
    def feed_start(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        external_lookup: dict[str, Any] | None = None,
        print_callback: Callable[[Literal["stdout", "stderr"], str], Any] | None = None,
    ) -> PydenoSnapshot | PydenoComplete:
        """Start a feed and return a `PydenoSnapshot` at every external call (or a
        `PydenoComplete` when it ends), instead of answering them.

        `external_lookup` names the functions the feed may call (their stubs are installed
        before it runs); their values are only used by `PydenoSnapshot.resume_auto`. Other
        arguments as in `feed_run`; ``print_callback`` stays in force until the feed ends."""
        agent = self._live()
        calls, names = _check_lookup(external_lookup, sync=False)
        prepared = _prepare(code, inputs, external_lookup, names)
        self._printer.callback = _printer_for(print_callback)
        agent._core.refused = None  # noqa: SLF001
        try:
            return self._step(self._start(agent, prepared), calls, frozenset(calls))
        except BaseException:
            self._printer.callback = None
            raise

    # -- durability ----------------------------------------------------------

    @_exclusive
    def dump(self, *, associated_data: bytes = b"") -> bytes:
        """The session's state, idle or suspended mid-feed, as signed bytes (the agent sandbox's
        journal: every feed's code and every external answer, HMAC-signed with the pool's
        ``dump_key``). The session stays usable. Restore with `load_session` (idle) or
        `load_snapshot` (suspended), on a fresh worker, by deterministic replay. After the worker
        died (a crash, a timeout, a memory kill, a cancellation) it still works: it returns the
        state as of the last feed that ended with the worker alive.

        `associated_data` (a tenant or conversation id, plus a counter of your own if rollback
        matters) is folded into the signature, not stored: the state then loads only when
        `load_session` / `load_snapshot` are given the same bytes. Without it, any state this
        pool dumped loads into any of its sessions, including an older dump of the same session
        (which restores the external-call budget it had then)."""
        if not isinstance(associated_data, (bytes, bytearray)):
            raise TypeError("associated_data must be bytes")
        try:
            return self._dumpable().dump(
                self._pool._key,  # noqa: SLF001
                associated_data=associated_data,
            )
        except PydenoError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PydenoError(f"cannot dump this session: {exc}", exc) from exc

    @_exclusive
    def load_session(self, state: bytes, *, associated_data: bytes = b"") -> None:
        """Replace this session's state with `state` (a `dump()` taken between feeds), replayed
        on a fresh worker; the current worker is killed. Only state signed with this pool's
        ``dump_key`` (and dumped with the same `associated_data`) loads. Raises `PydenoError` for
        state that is not authentic or that was dumped mid-feed (use `load_snapshot`)."""
        self._replace(state, suspended=False, associated_data=associated_data)

    @_exclusive
    def load_snapshot(
        self,
        state: bytes,
        *,
        print_callback: Callable[[Literal["stdout", "stderr"], str], Any] | None = None,
        external_lookup: dict[str, Any] | None = None,
        associated_data: bytes = b"",
    ) -> PydenoSnapshot:
        """Restore a `dump()` taken mid-feed and return the snapshot it was suspended at.
        `external_lookup` is for `resume_auto`; ``print_callback`` for the rest of the feed;
        `associated_data` must be what the state was dumped with."""
        calls, _ = _check_lookup(external_lookup, sync=False)
        agent = self._replace(state, suspended=True, associated_data=associated_data)
        self._printer.callback = _printer_for(print_callback)
        step = agent.pending
        assert step is not None
        snapshot = self._step(
            step,
            calls,
            None if external_lookup is None else frozenset(calls),
            pending=True,
        )
        assert isinstance(snapshot, PydenoSnapshot)
        return snapshot

    def _replace(
        self, state: bytes, *, suspended: bool, associated_data: bytes = b""
    ) -> AgentSandbox:
        old = self._agent  # its worker may be gone: loading is how a session recovers
        if old is None:
            self._live()  # raises: not checked out, or closed
        self._printer.callback = (
            None  # the replay's console output was printed long ago
        )
        new = self._pool._load(state, self._limits, associated_data)  # noqa: SLF001
        if (new.pending is not None) != suspended:
            new.close()
            raise PydenoError(
                "this state was dumped mid-feed; use load_snapshot"
                if not suspended
                else "this state was dumped between feeds; use load_session"
            )
        new._core.console.user = self._printer  # noqa: SLF001
        self._agent = new
        assert old is not None
        old.close()
        return new

    # -- internals -----------------------------------------------------------

    def _start(
        self,
        agent: AgentSandbox,
        prepared: _Prepared,
        run: Callable[[str], Any] | None = None,
    ) -> Any:
        run = run or agent.start
        step = run(prepared.source)
        if not (isinstance(step, Failed) and _is_js_syntax(step.error)):
            return step
        # Was it the code that does not parse (nothing ran), or a SyntaxError it threw?
        if self._compiles(agent, prepared.source):
            return step
        if prepared.fallback is not None:
            # The rewrite of the last statement did not parse: run the feed as written.
            step = run(prepared.fallback)
            if not (isinstance(step, Failed) and _is_js_syntax(step.error)):
                return step
            if self._compiles(agent, prepared.fallback):
                return step
        raise _failure(step.error, compile_time=True) from None

    @staticmethod
    def _compiles(agent: AgentSandbox, source: str) -> bool:
        try:
            return agent._core.rt.eval(_compile_check(source)) is not False  # noqa: SLF001
        except Exception:  # noqa: BLE001 - the worker died: not a syntax question any more
            return True

    def _resume(
        self,
        agent: AgentSandbox,
        step: ToolCall,
        value: Any,
        error: BaseException | None,
    ) -> Any:
        if error is None:
            try:
                return agent.resume(step, value)
            except (
                TypeError
            ) as exc:  # a value the sandbox cannot hold: the guest sees why
                return agent.resume(step, error=exc)
        return agent.resume(step, error=error)

    def _bounded_external(
        self, fn: Any, name: str, args: tuple[Any, ...]
    ) -> tuple[Any, BaseException | None]:
        """`_call_external` on a tool thread, given up on as soon as the worker is gone (a limit
        enforced while the external runs killed it): the caller is released at once; the
        external is left to finish on its thread and its answer is discarded."""
        agent = self._live()
        core = agent._core  # noqa: SLF001
        context = contextvars.copy_context()
        context.run(_TOOL_OF.set, core.session_id)
        # The session's own tool thread: never shared with another session.
        try:
            future = core.tools.submit(context.run, self._call_external, fn, name, args)
        except _ThreadsExhausted as exc:
            core.note_refusal(exc)  # answered with the generic error, as feed_run does
            return _MISSING, _unavailable()
        rt = core.rt
        while True:
            try:
                return future.result(_POLL)
            except concurrent.futures.TimeoutError:
                if rt.is_closed():
                    return _MISSING, RuntimeError(
                        "the run ended while the external ran"
                    )

    @staticmethod
    def _call_external(
        fn: Any, name: str, args: tuple[Any, ...]
    ) -> tuple[Any, BaseException | None]:
        if fn is None:
            return _MISSING, _not_available(name)
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001 - the guest sees the failure
            return _MISSING, exc
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if close is not None:
                close()
            return _MISSING, _public(
                TypeError(
                    f"{name} returned an awaitable; async externals need AsyncPydeno"
                )
            )
        return result, None

    def _finish(self, step: Any) -> Any:
        if isinstance(step, Done):
            return _output(step.value)
        assert isinstance(step, Failed)
        error = _ended(step.error, self._agent)
        refused = self._agent._core.refused if self._agent is not None else None  # noqa: SLF001
        if refused is not None and not isinstance(error, PydenoCrashedError):
            raise ToolThreadLimitError(str(refused), refused) from error
        raise error from None

    def _step(
        self,
        step: Any,
        calls: dict[str, Any],
        declared: frozenset[str] | None,
        *,
        pending: bool = False,
    ) -> PydenoSnapshot | PydenoComplete:
        agent = self._agent
        assert agent is not None
        while isinstance(step, ToolCall):
            unpacked = _unpack(step)
            if unpacked is not None:
                if pending or declared is None or unpacked[0] in declared:
                    return PydenoSnapshot(
                        self, step, unpacked[0], unpacked[1], calls, declared
                    )
                # Not a function this feed declared (a stub left by an earlier feed, or a
                # name passed straight to the dispatcher): refused, as `feed_run` does.
                step = agent.resume(step, error=_not_available(unpacked[0]))
                continue
            step = agent.resume(step, error=_not_available(None))
        self._printer.callback = None
        return PydenoComplete(self._finish(step))

    @_exclusive
    def _answer(
        self, snapshot: PydenoSnapshot, value: Any, error: BaseException | None
    ) -> PydenoSnapshot | PydenoComplete:
        agent = self._live()
        try:
            step = self._resume(agent, snapshot._call, value, error)  # noqa: SLF001
        except BaseException:
            self._printer.callback = None
            raise
        return self._step(step, snapshot._lookup, snapshot._declared)  # noqa: SLF001

    def __repr__(self) -> str:
        state = (
            "not checked out"
            if not self._entered
            else "closed"
            if self._agent is None or self._agent.is_closed()
            else "open"
        )
        return f"PydenoSession({self.script_name!r}, {state})"
