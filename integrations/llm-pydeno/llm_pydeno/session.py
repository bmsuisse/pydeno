"""A sandboxed JavaScript session for a model, with no dependency on `llm`.

`JavaScriptSession.run(code)` runs ``code`` in a `pydeno.AgentSandbox` (an `IsolatedRuntime`
worker process under the OS sandbox) and returns the `pydeno.ExecutionResult` fields as a dict:
``status``, ``stdout``, ``stderr``, ``result``, ``error``, ``error_type``, ``truncated``.

The session keeps its state between calls: ``const``/``let``/``var``/``function``/``class``
declarations that start a line, and assignments to ``globalThis``, survive into the next run (an
indented or destructured declaration does not; see pydeno's agent-sessions guide). Code is the body of an
async function, so ``return`` gives the result and ``await`` works at the top level.

The worker is started on the first `run`. A run that stops the worker (a timeout, the memory cap)
closes that session; the next `run` starts a fresh one, and the failed run's ``error`` says that
earlier state is gone.
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = [
    "DEFAULT_MAX_MEMORY_MB",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_RESULT_BYTES",
    "DEFAULT_TIMEOUT",
    "JavaScriptSession",
]

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_MEMORY_MB = 256
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024
DEFAULT_MAX_RESULT_BYTES = 64 * 1024

RESET_NOTE = " (the JavaScript session was restarted; state from earlier calls is gone)"


class JavaScriptSession:
    """One sandboxed JavaScript session, created lazily and restarted after a fatal failure.

    Args:
        timeout: Seconds of guest running time per call. Exceeding it stops the worker.
        max_memory_mb: Resident memory cap of the worker, in MiB.
        max_output_bytes: Cap on each of ``stdout`` and ``stderr`` per call (UTF-8 bytes); past it
            the stream ends with ``[truncated]`` and ``truncated`` is true.
        max_result_bytes: Cap on the result as compact JSON; a larger one is a failed call with
            ``error_type="ResultTooLarge"`` (the session goes on).
        sandbox: ``"require"`` (default) refuses to run without the complete OS sandbox;
            ``"auto"`` applies what the platform offers.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        sandbox: str = "require",
    ) -> None:
        if not (isinstance(timeout, (int, float)) and timeout > 0):
            raise ValueError("timeout must be a positive number of seconds")
        for name, value in (
            ("max_memory_mb", max_memory_mb),
            ("max_output_bytes", max_output_bytes),
            ("max_result_bytes", max_result_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if sandbox not in ("require", "auto"):
            raise ValueError("sandbox must be 'require' or 'auto'")
        self.timeout = float(timeout)
        self.max_memory = max_memory_mb * 1024 * 1024
        self.max_output_bytes = max_output_bytes
        self.max_result_bytes = max_result_bytes
        self.sandbox = sandbox
        self._sandbox: Any = None
        self._lock = threading.Lock()

    def _open(self) -> Any:
        from pydeno import AgentSandbox

        return AgentSandbox(
            {},
            timeout=self.timeout,
            max_output_bytes=self.max_output_bytes,
            max_result_bytes=self.max_result_bytes,
            max_memory=self.max_memory,
            sandbox=self.sandbox,
        )

    def run(self, code: str) -> dict[str, Any]:
        """Run ``code`` and return the `ExecutionResult` fields. Never raises for a failed run;
        a worker that cannot start (no OS sandbox, say) is a failed result too."""
        with self._lock:
            if self._sandbox is None or self._sandbox.is_closed():
                try:
                    self._sandbox = self._open()
                except Exception as exc:  # noqa: BLE001 - reported to the model
                    return _failed(exc)
            try:
                outcome = self._sandbox.execute(code).to_dict()
            except Exception as exc:  # noqa: BLE001 - a closed session raises here
                outcome = _failed(exc)
            if self._sandbox.is_closed():
                self._sandbox = None
                if outcome.get("error"):
                    outcome["error"] += RESET_NOTE
            return outcome

    def reset(self) -> None:
        """Stop the worker; the next `run` starts with empty state."""
        with self._lock:
            sandbox, self._sandbox = self._sandbox, None
        if sandbox is not None:
            sandbox.close()

    close = reset


def _failed(exc: BaseException) -> dict[str, Any]:
    return {
        "status": "Failed",
        "stdout": "",
        "stderr": "",
        "result": None,
        "error": f"{type(exc).__name__}: {exc}",
        "error_type": type(exc).__name__,
        "truncated": False,
    }
