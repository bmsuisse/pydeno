"""The `llm` plugin module (the ``llm`` entry point): a sandboxed JavaScript interpreter for models, backed by pydeno.

Registers the ``PyDeno`` toolbox (``llm -T PyDeno ...``). Its one tool, ``PyDeno_run_javascript``,
runs code in a `pydeno.AgentSandbox` session that keeps state between calls, and returns the
`pydeno.ExecutionResult` fields (``stdout``, ``stderr``, ``result``, ``error``, ...).

The toolbox is synchronous on purpose: `llm` runs an ``async def`` tool from a synchronous chain
with a fresh ``asyncio.run`` per call, and an `AsyncAgentSandbox` belongs to the event loop it was
started on, so its state would not survive from one call to the next.
"""

from __future__ import annotations

from typing import Any

import llm

from .session import (
    DEFAULT_MAX_MEMORY_MB,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_TIMEOUT,
    JavaScriptSession,
)

__all__ = ["JavaScriptSession", "PyDeno", "register_tools"]


class PyDeno(llm.Toolbox):
    """A sandboxed JavaScript session (V8 in a worker process under the OS sandbox).

    One instance is one JavaScript global scope for its whole life: use one instance per
    conversation and user, never one shared between users, or pass
    ``fresh_session_per_call=True``. A dropped instance stops its worker when it is collected
    (`llm.Toolbox` has no close hook).
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        sandbox: str = "require",
        fresh_session_per_call: bool = False,
    ) -> None:
        self._session = JavaScriptSession(
            timeout=timeout,
            max_memory_mb=max_memory_mb,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
            sandbox=sandbox,
            fresh_session_per_call=fresh_session_per_call,
        )

    def run_javascript(self, code: str) -> dict[str, Any]:
        """Run JavaScript in a sandboxed V8 session and return what it printed and returned.

        The code is the body of an async function: use `return` for the result, and `await`
        works at the top level. const/let/var/function/class declarations that start a
        line, and globalThis properties, are kept for later calls (an indented or
        destructured declaration is not: assign it to globalThis instead). console.log/info/debug go to stdout,
        console.warn/error to stderr. There is no filesystem, network, process, require or
        import of packages: only the JavaScript language and its built-ins.

        Returns a JSON object: status ("Succeeded" or "Failed"), stdout, stderr, result (the
        returned value as JSON), error and error_type (when it failed), truncated (output was
        cut at the size cap).
        """
        return self._session.run(code)

    def _close(self) -> None:
        self._session.close()


@llm.hookimpl
def register_tools(register: Any) -> None:
    register(PyDeno)
