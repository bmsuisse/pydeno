"""`ExecutionResult`: one bounded, JSON-able shape for the outcome of running guest code.

``{status, stdout, stderr, result, error, error_type, truncated}``, the shape a host that runs
JavaScript next to another engine wants for both. Built by `AgentSandbox.execute`,
`IsolatedRuntime.execute`/`execute_async`, and from any `Done`/`Failed` step (`to_result()`).

Bounds, because everything here is sized by the guest:

- ``console.log``/``info``/``debug`` go to `stdout`, ``console.warn``/``error``/``trace`` to
  `stderr`, each in call order, each capped at ``max_output_bytes`` (UTF-8). Past the cap the
  stream ends with a line ``[truncated]`` and `truncated` is set; later calls are dropped without
  being formatted.
- `result` is the run's value as plain JSON data, capped at ``max_result_bytes`` of compact JSON.
  A larger one makes the run ``Failed`` with ``error_type="ResultTooLarge"``; the session goes on.

`error_type` is the JavaScript error's ``name`` for an error the guest threw (``TypeError``,
``ReferenceError``, a tool's ``ToolBudgetError``, or a name the guest chose itself), and the
pydeno exception class for a failure on the host's side (``RuntimeTimeout``, ``WorkerCrashed``,
``ResultTooLarge``, ...). A guest cannot claim one of the host-side names: a thrown error named
like one is reported as ``Error``.
"""

from __future__ import annotations

import base64
import json
import math
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Literal

from ._pydeno import JavaScriptError, JsUndefined

__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_RESULT_BYTES",
    "ExecutionResult",
    "ResultTooLarge",
]

DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
TRUNCATED_MARKER = "[truncated]"

_STDOUT_LEVELS = frozenset({"log", "info", "debug"})
# What guest text must not carry into captured output, an exception message, a log or a terminal:
# C0 and C1 controls except tab and newline (escape sequences, carriage returns), bidirectional
# overrides, embeddings and isolates (text that displays in another order than it reads), and the
# invisible zero-width space, word joiner and byte-order mark. Each is replaced by "?". ZWJ/ZWNJ
# stay: scripts and emoji need them.
UNSAFE_TEXT = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u180e\u200b\u200e\u200f\u202a-\u202e"
    "\u2060\u2066-\u2069\ufeff]"
)
_MAX_DEPTH = 200
_GUEST_ERROR = re.compile(r"^(?:Uncaught )?([A-Za-z_$][A-Za-z0-9_$]{0,63})(?::|$)")
_EVAL_PREFIX = "Evaluation failed: "
# Failures only the host can report. A guest that throws an error with one of these names gets
# `Error` instead, so `error_type` never says "the worker timed out" because the guest said so.
_HOST_ONLY = frozenset(
    {
        "ResultTooLarge",
        "RuntimeTimeout",
        "RuntimeTerminated",
        "RuntimeForceKilled",
        "WorkerCrashed",
        "JournalError",
        "ReplayDivergence",
    }
)


class ResultTooLarge(RuntimeError):
    """A run's result is larger than ``max_result_bytes`` once serialised as JSON."""

    error_type = "ResultTooLarge"


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of running guest code, bounded and JSON-able (`to_dict()`).

    Attributes:
        status: ``"Succeeded"`` or ``"Failed"``.
        stdout: ``console.log``/``info``/``debug`` output, one line per call, in call order.
        stderr: ``console.warn``/``error``/``trace`` output, likewise.
        result: The value the code returned, as plain JSON data (``undefined`` is ``None``,
            bytes are base64 text, dates ISO 8601 text, sets lists, non-finite numbers
            ``None``). ``None`` when the run failed.
        error: What went wrong (``"TypeError: x is not a function"``), or ``None``.
        error_type: A stable name for the failure (see the module docs), or ``None``.
        truncated: Some console output was cut at ``max_output_bytes``.
    """

    status: Literal["Succeeded", "Failed"]
    stdout: str = ""
    stderr: str = ""
    result: Any = None
    error: str | None = None
    error_type: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "Succeeded"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_limit(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive int")
    return value


# ---------------------------------------------------------------------------
# console capture
# ---------------------------------------------------------------------------


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value)) if value or math.copysign(1, value) > 0 else "-0"
    return repr(value)


def format_console_arg(arg: Any) -> str:
    """One `console.*` argument as text, close to what a JavaScript console prints."""
    if isinstance(arg, str):
        return arg
    if isinstance(arg, JsUndefined):
        return "undefined"
    if arg is None:
        return "null"
    if isinstance(arg, bool):
        return "true" if arg else "false"
    if isinstance(arg, int):
        return str(arg)
    if isinstance(arg, float):
        return _number(arg)
    try:
        return json.dumps(to_jsonable(arg), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return f"<{type(arg).__name__}>"


def _cut_utf8(text: str, limit: int) -> str:
    """The longest prefix of `text` that is at most `limit` bytes of UTF-8."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


class _Stream:
    __slots__ = ("cut", "parts", "size")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.cut = False


class OutputCapture:
    """Collects one run's console output into two capped streams. Thread-safe: console calls
    arrive on whatever thread serves the worker."""

    def __init__(self, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> None:
        self.max_bytes = check_limit("max_output_bytes", max_output_bytes)
        self._out = _Stream()
        self._err = _Stream()
        self._lock = threading.Lock()

    def __call__(self, level: Any, args: Any) -> None:
        stream = self._out if level in _STDOUT_LEVELS else self._err
        if stream.cut:
            return  # full: do not even format it
        if not isinstance(args, (list, tuple)):
            args = [args]
        line = (
            UNSAFE_TEXT.sub("?", " ".join(format_console_arg(a) for a in args)) + "\n"
        )
        with self._lock:
            if stream.cut:
                return
            room = self.max_bytes - stream.size
            size = len(line.encode("utf-8"))
            if size <= room:
                stream.parts.append(line)
                stream.size += size
                return
            head = _cut_utf8(line, room)
            if head:
                stream.parts.append(head)
                stream.size += len(head.encode("utf-8"))
            stream.cut = True

    @staticmethod
    def _text(stream: _Stream) -> str:
        text = "".join(stream.parts)
        if stream.cut:
            if text and not text.endswith("\n"):
                text += "\n"
            text += TRUNCATED_MARKER + "\n"
        return text

    @property
    def stdout(self) -> str:
        with self._lock:
            return self._text(self._out)

    @property
    def stderr(self) -> str:
        with self._lock:
            return self._text(self._err)

    @property
    def truncated(self) -> bool:
        return self._out.cut or self._err.cut


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def to_jsonable(value: Any, depth: int = 0) -> Any:
    """A value that crossed the worker boundary, as plain JSON data (see `ExecutionResult`)."""
    if depth > _MAX_DEPTH:
        raise ValueError("result is nested too deeply")
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, JsUndefined):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            k if isinstance(k, str) else format_console_arg(k): to_jsonable(
                v, depth + 1
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        members = [to_jsonable(v, depth + 1) for v in value]
        # A set's iteration order depends on the process's string hash seed.
        members.sort(key=lambda m: json.dumps(m, sort_keys=True))
        return members
    return f"<{type(value).__name__}>"


def json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def bounded_result(value: Any, max_result_bytes: int) -> Any:
    """`value` as JSON data, or `ResultTooLarge` if its compact JSON is over the cap."""
    try:
        data = to_jsonable(value)
        size = json_size(data)
    except (ValueError, RecursionError) as exc:
        raise ResultTooLarge(f"the result cannot be returned: {exc}") from None
    if size > max_result_bytes:
        raise ResultTooLarge(
            f"the result is {size} bytes of JSON, over max_result_bytes={max_result_bytes}; "
            "return less (a summary, a page, or a count)"
        )
    return data


def error_type(exc: BaseException) -> str:
    """A stable name for a failure: the guest's error name, or the host's exception class."""
    if isinstance(exc, JavaScriptError):
        name = getattr(exc, "name", None)
        if not isinstance(name, str) or not name:
            match = _GUEST_ERROR.match(str(exc).removeprefix(_EVAL_PREFIX))
            name = match.group(1) if match else "Error"
        return "Error" if name in _HOST_ONLY else name
    declared = getattr(type(exc), "error_type", None)
    if isinstance(declared, str):
        return declared
    return type(exc).__name__


def error_message(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, JavaScriptError):
        text = text.removeprefix(_EVAL_PREFIX)
    return text or type(exc).__name__


def failed_result(
    exc: BaseException,
    *,
    stdout: str = "",
    stderr: str = "",
    truncated: bool = False,
    max_error_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ExecutionResult:
    message = error_message(exc)
    cut = len(message.encode("utf-8")) > max_error_bytes
    if cut:
        message = _cut_utf8(message, max_error_bytes) + " " + TRUNCATED_MARKER
    return ExecutionResult(
        status="Failed",
        stdout=stdout,
        stderr=stderr,
        error=message,
        error_type=error_type(exc),
        truncated=cut or truncated,
    )


def capture_result(
    capture: OutputCapture,
    *,
    value: Any = None,
    error: BaseException | None = None,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> ExecutionResult:
    """An `ExecutionResult` from a capture and either a value or the error the run raised."""
    if error is None:
        try:
            data = bounded_result(value, max_result_bytes)
        except ResultTooLarge as exc:
            error = exc
        else:
            return ExecutionResult(
                status="Succeeded",
                stdout=capture.stdout,
                stderr=capture.stderr,
                result=data,
                truncated=capture.truncated,
            )
    return failed_result(
        error,
        stdout=capture.stdout,
        stderr=capture.stderr,
        truncated=capture.truncated,
        max_error_bytes=capture.max_bytes,
    )
