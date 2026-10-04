"""A stable error taxonomy: `classify_error(exc)` maps any error pydeno can raise to a small,
fixed vocabulary a service can branch on without matching exception text itself.

The `kind` strings are a public contract: lowercase, only ever added to, and listed in
`docs/reference/error-kinds.md`.

**Retry rule (one rule for every kind).** `retryable` is true only when the failure was
environmental or transient, so running the *same work* again on a *fresh* runtime could plausibly
succeed: the worker process died or failed to start. A deadline, a memory overrun, a CPU cap or a
spent budget is a property of the work under the limits it was given; the same code fails the same
way again, so those are `retryable=False`. Where a larger limit would let the work finish,
`retry_with_larger_limits` says so (`timeout=`, `max_memory=`, `max_calls=`, ...): that is a
decision for the caller, who may be unwilling to grant it.

**Trust.** Some messages contain text the *worker* (and so the guest) chose: the last line of the
worker's stderr after a crash, the message of an error it reports. A classifier that searched
messages for phrases could be steered by a guest that prints `max_memory` and dies. So every
message-based rule here matches the whole host-authored message, or only its host-authored
prefix, with an anchored pattern; the worker's text only ever appears after it. What a guest can
still do is make an error look like a *less* retryable kind, never a more retryable one: the
default for a dead worker is `worker_crashed` (the only retryable crash kind), and the kinds a
guest can choose by naming a JavaScript error (`tool_*`) are all non-retryable. Facts the host
established itself (exception *types* and the host-authored messages above) are what pick every
other kind.

This module never imports the heavy parts of pydeno: a class from a module that has not been
imported yet cannot be the type of an exception you are holding.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["ErrorInfo", "KINDS", "classify_error"]


@dataclass(frozen=True)
class ErrorInfo:
    """What an error means to a caller."""

    kind: str
    #: A retry of the same work on a fresh runtime could plausibly succeed (environmental fault).
    retryable: bool
    summary: str
    #: The work failed against a limit; a larger limit could let it finish (caller's decision).
    retry_with_larger_limits: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "retryable": self.retryable,
            "retry_with_larger_limits": self.retry_with_larger_limits,
            "summary": self.summary,
        }


#: kind -> (retryable, retry_with_larger_limits, summary): the source of docs/reference/error-kinds.md.
KINDS: dict[str, tuple[bool, bool, str]] = {
    "js_error": (False, False, "The guest code threw or failed to compile."),
    "timeout": (False, True, "A deadline passed before the work finished."),
    "cpu_limit": (
        False,
        True,
        "The worker used more CPU in one command than its cap allows.",
    ),
    "memory_limit": (False, True, "The worker went over max_memory and was stopped."),
    "thread_limit": (
        False,
        False,
        "The worker started more threads than a worker may.",
    ),
    "worker_crashed": (
        True,
        False,
        "The worker process died, hung or failed to start.",
    ),
    "terminated": (False, False, "The runtime was terminated on request."),
    "force_killed": (
        False,
        False,
        "A termination was never acknowledged; the runtime was abandoned.",
    ),
    "host_wait": (
        False,
        True,
        "Host callbacks kept the guest waiting longer than max_host_wait.",
    ),
    "max_pause": (
        False,
        True,
        "Tool answers kept an agent run paused longer than max_pause.",
    ),
    "host_call_budget": (
        False,
        True,
        "The guest made more host calls than max_host_calls.",
    ),
    "inflight_limit": (False, True, "Too many host calls were outstanding at once."),
    "tool_budget": (
        False,
        True,
        "The tool-call budget (max_calls / max_tool_calls) is spent.",
    ),
    "tool_not_found": (
        False,
        False,
        "A tool reported that what it was asked for is missing.",
    ),
    "tool_failed": (False, False, "A host tool raised an error."),
    "protocol_violation": (
        False,
        False,
        "The worker sent something the host refuses; it was discarded.",
    ),
    "sandbox_unavailable": (
        False,
        False,
        "The worker refused to start without a complete OS sandbox.",
    ),
    "limits_unmeasurable": (
        False,
        False,
        "sandbox='require' but the worker's resource usage cannot be read here.",
    ),
    "closed": (
        False,
        False,
        "The runtime, function, stream or session is already closed.",
    ),
    "journal_invalid": (
        False,
        False,
        "An agent journal is malformed, too large or not authentic.",
    ),
    "replay_divergence": (
        False,
        False,
        "Replaying a journal produced a different outcome.",
    ),
    "snapshot_invalid": (False, False, "A signed snapshot failed authentication."),
    "invalid_input": (
        False,
        False,
        "A value or argument was refused (wire or API misuse).",
    ),
    "cancelled": (False, False, "The surrounding asyncio task was cancelled."),
    "unknown": (False, False, "An error pydeno does not classify."),
}


def _info(kind: str) -> ErrorInfo:
    retryable, larger, summary = KINDS[kind]
    return ErrorInfo(kind, retryable, summary, larger)


# ------------------------------------------------------------------ type tests (no imports)


def _cls(module: str, name: str) -> type | None:
    mod = sys.modules.get(module)
    return getattr(mod, name, None) if mod is not None else None


def _is(exc: BaseException, module: str, name: str) -> bool:
    cls = _cls(module, name)
    return cls is not None and isinstance(exc, cls)


def _timeout_types() -> tuple[type, ...]:
    # asyncio.TimeoutError is the builtin from 3.11; on 3.10 it is a separate class.
    asyncio = sys.modules.get("asyncio")
    return (TimeoutError,) + ((asyncio.TimeoutError,) if asyncio else ())


def _cancelled(exc: BaseException) -> bool:
    asyncio = sys.modules.get("asyncio")
    return asyncio is not None and isinstance(exc, asyncio.CancelledError)


# ------------------------------------------------------------------ host-authored messages
# Every pattern is anchored to the START of the message (`match`) and, where the host's text is
# the whole message, to its END (`fullmatch`). Text after a host prefix is the worker's.

_NUM = r"[0-9.e+-]+"
_DEATH_PREFIX = r"(?:worker exited during startup|worker is gone|worker process died|lost the worker)"

_MEMORY = re.compile(
    rf"(?:worker used \d+ bytes, over max_memory=\d+; killed"
    rf"|{_DEATH_PREFIX}: worker went over max_memory=\d+ and exited)"
)
_THREADS = re.compile(r"worker started \d+ threads \(limit \d+\); killed")
_HOST_CALLS = re.compile(r"guest made more than max_host_calls=\d+ host calls")
_UNMEASURABLE = re.compile(
    r"(?:max_memory|the CPU cap|the thread cap)(?: and (?:the CPU cap|the thread cap))*"
    r" cannot be enforced on this system \(the worker's resource usage cannot be read\)"
    r" and sandbox='require' demands every protection"
)
_PROTOCOL = re.compile(
    r"(?:worker broke protocol: "
    r"|worker sent a malformed frame \("
    r"|worker reused a capability token$"
    r"|worker returned a malformed capability tokens?$)"
)
_CLOSED = re.compile(
    r"(?:runtime is closed|Runtime has been closed|Function has been closed"
    r"|Stream has been closed|the session is closed|the session was closed)"
)
# The worker reports why it would not start (its text, but only ever a refusal: no retry helps).
_SANDBOX_REFUSED = re.compile(
    r"worker failed to start: (?:an OS sandbox is required but|sandbox self-test failed)"
)
_CPU = re.compile(
    rf"worker used more than {_NUM}s of CPU in one command and was killed"
)
_HOST_WAIT = re.compile(
    rf"host callbacks kept the guest waiting for more than {_NUM}s in one command "
    r"\(max_host_wait\); worker killed"
)
_INFLIGHT = re.compile(r"more than \d+ host calls in flight")
_ABANDONED = re.compile(r"too many abandoned tool calls in this session")
# A host tool's exception reaches the caller as a JavaScriptError. In-process it has `.name`; from
# IsolatedRuntime the class is rebuilt from the message only, which reads
# "Evaluation failed: ToolBudgetError: ...". The guest can write such a message itself, which is
# harmless: all three kinds are non-retryable, like `js_error`.
_JS_TOOL = re.compile(
    r"(?:[A-Za-z ]+ failed: )?(ToolBudgetError|ToolNotFoundError|ToolError):"
)
_TOOL_KIND = {
    "ToolBudgetError": "tool_budget",
    "ToolNotFoundError": "tool_not_found",
    "ToolError": "tool_failed",
}


def _js_tool_kind(exc: BaseException, text: str) -> str | None:
    name = getattr(exc, "name", None)
    if not isinstance(name, str) or name not in _TOOL_KIND:
        found = _JS_TOOL.match(text)
        name = found.group(1) if found else None
    return _TOOL_KIND.get(name) if name else None


def _crash(exc: BaseException, text: str, pattern: re.Pattern[str]) -> bool:
    return _is(exc, "pydeno._isolated", "WorkerCrashed") and bool(pattern.match(text))


Row = tuple[str, Callable[[BaseException, str, bool], bool]]

# First match wins; most specific first.
_ROWS: list[Row] = [
    ("cancelled", lambda e, t, a: _cancelled(e)),
    ("force_killed", lambda e, t, a: _is(e, "pydeno._pydeno", "RuntimeForceKilled")),
    ("terminated", lambda e, t, a: _is(e, "pydeno._pydeno", "RuntimeTerminated")),
    (
        "cpu_limit",
        lambda e, t, a: (
            _is(e, "pydeno._pydeno", "RuntimeTimeout") and bool(_CPU.fullmatch(t))
        ),
    ),
    (
        "max_pause",
        lambda e, t, a: (
            a
            and _is(e, "pydeno._pydeno", "RuntimeTimeout")
            and bool(_HOST_WAIT.fullmatch(t))
        ),
    ),
    (
        "host_wait",
        lambda e, t, a: (
            _is(e, "pydeno._pydeno", "RuntimeTimeout") and bool(_HOST_WAIT.fullmatch(t))
        ),
    ),
    ("timeout", lambda e, t, a: _is(e, "pydeno._pydeno", "RuntimeTimeout")),
    ("tool_budget", lambda e, t, a: _is(e, "pydeno._tools", "ToolBudgetError")),
    ("tool_not_found", lambda e, t, a: _is(e, "pydeno._tools", "ToolNotFoundError")),
    ("tool_failed", lambda e, t, a: _is(e, "pydeno._tools", "ToolError")),
    (
        "inflight_limit",
        lambda e, t, a: (
            _is(e, "pydeno._pydeno", "JavaScriptError") and bool(_INFLIGHT.search(t))
        ),
    ),
    (
        "_js_tool",
        lambda e, t, a: _is(e, "pydeno._pydeno", "JavaScriptError"),
    ),  # see below
    ("js_error", lambda e, t, a: _is(e, "pydeno._pydeno", "JavaScriptError")),
    ("limits_unmeasurable", lambda e, t, a: _crash(e, t, _UNMEASURABLE)),
    ("memory_limit", lambda e, t, a: _crash(e, t, _MEMORY)),
    ("thread_limit", lambda e, t, a: _crash(e, t, _THREADS)),
    ("host_call_budget", lambda e, t, a: _crash(e, t, _HOST_CALLS)),
    ("protocol_violation", lambda e, t, a: _crash(e, t, _PROTOCOL)),
    ("sandbox_unavailable", lambda e, t, a: _crash(e, t, _SANDBOX_REFUSED)),
    (
        "closed",
        lambda e, t, a: isinstance(e, RuntimeError) and bool(_CLOSED.fullmatch(t)),
    ),
    # Anything else a WorkerCrashed says is the worker dying, hanging or not starting.
    ("worker_crashed", lambda e, t, a: _is(e, "pydeno._isolated", "WorkerCrashed")),
    ("replay_divergence", lambda e, t, a: _is(e, "pydeno._agent", "ReplayDivergence")),
    ("journal_invalid", lambda e, t, a: _is(e, "pydeno._agent", "JournalError")),
    (
        "snapshot_invalid",
        lambda e, t, a: _is(e, "pydeno._snapshot_auth", "SnapshotAuthenticationError"),
    ),
    ("timeout", lambda e, t, a: isinstance(e, _timeout_types())),
    (
        "inflight_limit",
        lambda e, t, a: (
            isinstance(e, RuntimeError)
            and bool(_INFLIGHT.fullmatch(t) or _ABANDONED.fullmatch(t))
        ),
    ),
    ("invalid_input", lambda e, t, a: isinstance(e, (TypeError, ValueError))),
]


def classify_error(exc: BaseException, *, via_agent: bool = False) -> ErrorInfo:
    """Classify any exception pydeno can raise. Never raises; unrecognised errors are `unknown`.

    `via_agent=True` says the error came out of an `AgentSandbox` run: its `max_pause` is the
    worker's `max_host_wait`, so that timeout is reported as `max_pause` instead of `host_wait`.
    """
    try:
        text = str(exc)
        for kind, test in _ROWS:
            if not test(exc, text, via_agent):
                continue
            if kind == "_js_tool":
                tool = _js_tool_kind(exc, text)
                if tool is None:
                    continue
                kind = tool
            return _info(kind)
    except Exception:  # noqa: BLE001 - a classifier that raises is worse than "unknown"
        pass
    return _info("unknown")
