"""`AsyncPydeno`: the front door for asyncio (Monty's `AsyncMonty`).

```python
async with AsyncPydeno() as pool:
    async with pool.checkout() as session:
        await session.feed_run("1 + 1")      # -> 2
```

The same contract as `Pydeno` (see `_front`), on `AsyncSandboxPool` and `AsyncAgentSandbox`:
nothing blocks the loop, external functions may be coroutine functions (plain ones run on a thread
pool), and cancelling a feed kills its worker before the `CancelledError` propagates (the session
is then over). A pool, and every session from it, belongs to the event loop it was started on.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ._agent import (
    _MISSING,
    _SESSION_IDS,
    _TOOL_OF,
    Done,
    Failed,
    ToolCall,
    _log,
    _UNBOUNDED_LIMIT,
    _ThreadBudget,
    _ThreadsExhausted,
    _ToolThread,
    _unavailable,
)
from ._aio import AsyncIsolatedRuntime
from ._aio_agent import AsyncAgentSandbox, apreinstall
from ._front import (
    _CONFIG,
    _EXTERNAL,
    PydenoComplete,
    PydenoCrashedError,
    PydenoError,
    DEFAULT_MAX_TOOL_THREADS,
    PydenoLimits,
    ToolThreadLimitError,
    _check_lookup,
    _close_pool,
    _open_pool,
    _tool_budget,
    _check_pool_arguments,
    _compile_check,
    _ended,
    _external_placeholder,
    _checked_answer,
    _failure,
    _fresh_seed,
    _is_js_syntax,
    _journal_seed,
    _wrong_load,
    _Limits,
    _load_failure,
    _not_available,
    _output,
    _prepare,
    _Prepared,
    _Printer,
    _REFILL_DELAY,
    _printer_for,
    _resolve_limits,
    _start_failure,
    _unpack,
)
from ._isolated import WorkerCrashed
from ._sandbox_pool import AsyncSandboxPool

__all__ = ["AsyncPydeno", "AsyncPydenoSession", "AsyncPydenoSnapshot"]

_CONSOLE_THREADS = _ThreadBudget(_UNBOUNDED_LIMIT, "console sinks")


class _Pool(AsyncSandboxPool):
    """`AsyncSandboxPool`, except that every worker gets its own random seed and has run its
    first command (see `_front._Core`)."""

    def _build(self, session: dict[str, Any] | None = None) -> AsyncIsolatedRuntime:
        return AsyncIsolatedRuntime(
            self._config,
            prewarm=False,
            random_seed=_fresh_seed(),
            **self._spawn,
            **(self._session if session is None else session),
        )

    async def _start_runtime(
        self, rt: AsyncIsolatedRuntime, session: dict[str, Any] | None
    ) -> None:
        if session is None and self._ready:
            await asyncio.sleep(_REFILL_DELAY)
        await rt._start()
        await apreinstall(rt, [_EXTERNAL])


class AsyncPydeno:
    """`Pydeno` for asyncio: a pool of pre-started, OS-sandboxed workers (Monty's `AsyncMonty`).

    Takes `Pydeno`'s arguments. Constructing it validates them and starts nothing; the workers
    start on ``async with AsyncPydeno() as pool:`` (or ``await pool.start()``), on the loop that
    will use them: the first one before ``async with`` returns, so a platform that cannot sandbox
    fails there with a `PydenoCrashedError`, the rest in the background."""

    def __init__(
        self,
        *,
        min_processes: int = 2,
        max_workers: int | None = None,
        checkout_timeout: float = 30.0,
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
        self._pool = _Pool(
            _CONFIG,
            size=min_processes,
            max_workers=max_workers,
            checkout_timeout=checkout_timeout,
            **self._spawn,
        )

    async def start(self) -> AsyncPydeno:
        """Start the first worker (errors surface here) and the background refill."""
        _open_pool(
            self._budget
        )  # may warn (or raise, under -W error): before anything starts
        try:
            await self._pool.start()
        except WorkerCrashed as exc:
            _close_pool(self._budget)
            raise _start_failure(exc, self._sandbox) from exc
        except BaseException:
            _close_pool(self._budget)
            raise
        weakref.finalize(self, _close_pool, self._budget)
        return self

    async def __aenter__(self) -> AsyncPydeno:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Kill the workers still waiting in the pool. Idempotent."""
        await self._pool.close()
        _close_pool(self._budget)

    def checkout(
        self, *, script_name: str = "main.js", limits: PydenoLimits | None = None
    ) -> AsyncPydenoSession:
        """A session on one dedicated worker, checked out by ``async with`` (see
        `Pydeno.checkout`)."""
        return AsyncPydenoSession(
            self,
            script_name,
            self._limits
            if limits is None
            else _resolve_limits(self._limits_in, limits),
        )

    def stats(self) -> dict[str, Any]:
        """As `Pydeno.stats()`."""
        return self._pool.stats()

    @staticmethod
    def sandbox_status() -> Any:
        """`pydeno.sandbox_status()`."""
        from ._status import sandbox_status  # noqa: PLC0415

        return sandbox_status()

    # -- for sessions --------------------------------------------------------

    async def _runtime(
        self, limits: _Limits, seed: int | None = None
    ) -> AsyncIsolatedRuntime:
        try:
            if seed is None and limits.max_memory == self._limits.max_memory:
                return await self._pool.checkout()
            options = {**self._spawn, "max_memory": limits.max_memory}
            return await self._pool._new(  # noqa: SLF001
                factory=lambda: AsyncIsolatedRuntime(
                    _CONFIG,
                    # A spare is an extra process: only without a cap, as before caps existed.
                    prewarm=self._pool._capacity.maximum is None,  # noqa: SLF001
                    random_seed=_fresh_seed() if seed is None else seed,
                    **options,
                )
            )
        except WorkerCrashed as exc:
            raise _start_failure(exc, self._sandbox) from exc

    async def _agent(self, limits: _Limits, printer: _Printer) -> AsyncAgentSandbox:
        rt = await self._runtime(limits)
        try:
            agent = AsyncAgentSandbox(
                {_EXTERNAL: _external_placeholder},
                runtime=rt,
                max_tool_calls=limits.max_tool_calls,
                timeout=limits.timeout,
                max_pause=limits.max_pause,
            )
        except BaseException:
            await rt.close()
            raise
        agent._core.console.user = printer  # noqa: SLF001
        await agent.__aenter__()
        return agent

    async def _load(
        self,
        state: bytes,
        limits: _Limits,
        associated_data: bytes = b"",
        *,
        suspended: bool | None = None,
        release: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncAgentSandbox:
        seed = _journal_seed(
            state, self._key, self._spawn["strict_eval"], associated_data, suspended
        )
        if release is not None and self._pool._capacity.maximum is not None:  # noqa: SLF001
            await release()  # see `Pydeno._load`
        rt = await self._runtime(limits, seed)
        try:
            return await AsyncAgentSandbox.load(
                state,
                self._key,
                {_EXTERNAL: _external_placeholder},
                associated_data=associated_data,
                runtime=rt,
                timeout=limits.timeout,
                max_pause=limits.max_pause,
            )
        except asyncio.CancelledError:
            await rt.close()
            raise
        except BaseException as exc:
            await rt.close()
            raise _load_failure(exc) from exc


class AsyncPydenoSnapshot:
    """`PydenoSnapshot` for asyncio: `resume` and `resume_auto` are coroutines; `resume_auto`
    awaits a coroutine external function (Monty's `AsyncFunctionSnapshot`)."""

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
        session: AsyncPydenoSession,
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

    async def resume(
        self,
        result: Any = _MISSING,
        /,
        *,
        value: Any = _MISSING,
        error: BaseException | None = None,
    ) -> AsyncPydenoSnapshot | PydenoComplete:
        """See `PydenoSnapshot.resume`."""
        value, error = _checked_answer(result, value, error)
        self._take()
        return await self._session._answer(self, value, error)  # noqa: SLF001

    async def resume_auto(self) -> AsyncPydenoSnapshot | PydenoComplete:
        """Answer the call from the feed's `external_lookup` (awaiting a coroutine function;
        running a plain one on the handler thread pool)."""
        self._take()
        session = self._session
        agent = session._live()  # noqa: SLF001
        try:
            value, error = await agent._until_run_ends(  # noqa: SLF001
                session._call_external(  # noqa: SLF001
                    self._lookup.get(self.function_name), self.function_name, self.args
                )
            )
        except RuntimeError as exc:
            value, error = _MISSING, exc
        return await session._answer(self, value, error)  # noqa: SLF001

    async def dump(self, *, associated_data: bytes = b"") -> bytes:
        """The suspended session, signed (see `AsyncPydenoSession.dump`)."""
        return await self._session.dump(associated_data=associated_data)

    def __repr__(self) -> str:
        return (
            f"AsyncPydenoSnapshot(function_name={self.function_name!r}, args={self.args!r}, "
            f"call_id={self.call_id})"
        )


def _exclusive_async(method: Callable[..., Any]) -> Callable[..., Any]:
    """`_front._exclusive` for a coroutine method: one feed at a time per session."""

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self._busy:
            raise PydenoError(
                "the session is busy (another task is feeding it); use one session per task"
            )
        self._busy = True
        try:
            return await method(self, *args, **kwargs)
        finally:
            self._busy = False

    return wrapper


class AsyncPydenoSession:
    """`PydenoSession` for asyncio (Monty's `AsyncMontySession`): every method is a coroutine,
    external functions may be coroutine functions, and cancelling a feed kills the worker (the
    session is then over)."""

    def __init__(self, pool: AsyncPydeno, script_name: str, limits: _Limits) -> None:
        self._pool = pool
        self.script_name = script_name
        self._limits = limits
        self._agent: AsyncAgentSandbox | None = None
        self._printer = _Printer()
        self._entered = False
        self._busy = False
        # One id namespace with the agent sessions' (the self-close guard compares them).
        self._sid = next(_SESSION_IDS)
        self._tools = _ToolThread(f"pydeno-front-tool-{self._sid}", pool._budget)  # noqa: SLF001
        self._refused: BaseException | None = None
        # A session dropped without close() must not keep its tool thread (or its budget).
        self._refusals = 0
        # The session's console sink runs here, not on the process-wide handler pool: one
        # tenant's slow `print_callback` holds up only its own session. Bounded by construction
        # (one per session), so it does not count against the tool-thread budget.
        self._console = _ToolThread(
            f"pydeno-front-console-{self._sid}", _CONSOLE_THREADS
        )
        weakref.finalize(self, self._tools.close)
        weakref.finalize(self, self._console.close)

    async def __aenter__(self) -> AsyncPydenoSession:
        if self._entered:
            raise RuntimeError(
                "an AsyncPydenoSession is entered once: each checkout is a fresh, single-use worker"
            )
        self._entered = True
        self._agent = await self._pool._agent(self._limits, self._printer)  # noqa: SLF001
        self._agent._core.rt._handler_executor = self._console  # noqa: SLF001
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Kill the session's worker (it is never reused). Idempotent."""
        if _TOOL_OF.get() == self._sid:
            raise RuntimeError(
                "an external function cannot close the session that called it"
            )
        self._tools.close()
        self._console.close()
        if self._refusals > 1:
            _log.warning(
                "pydeno: session %d: %d tool calls were refused in all",
                self._sid,
                self._refusals,
            )
        agent, self._agent = self._agent, None
        if agent is not None:
            # SIGKILL at once: a worker that will never run again need not exit cleanly.
            if not agent._core.closed:  # noqa: SLF001
                agent._core.kill("the session ended")  # noqa: SLF001
            await agent.close()

    def _live(self) -> AsyncAgentSandbox:
        agent = self._agent
        if agent is None:
            raise RuntimeError(
                "the session is not checked out: use `async with pool.checkout() as session:`"
                if not self._entered
                else "the session is closed"
            )
        if agent.is_closed():
            raise PydenoCrashedError(
                "the session's worker is gone (crashed, killed, timed out or cancelled); check "
                "out a new session"
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
        return agent.worker_pid

    @property
    def session_id(self) -> bytes | None:
        """Always None: as in Monty for local workers (a server that stores sessions would
        give one). Session state travels as `dump()` bytes instead."""
        return None

    # -- feeding -------------------------------------------------------------

    @_exclusive_async
    async def feed_run(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        external_lookup: dict[str, Any] | None = None,
        print_callback: Callable[[Literal["stdout", "stderr"], str], Any] | None = None,
    ) -> Any:
        """See `PydenoSession.feed_run`; external functions may also be coroutine functions."""
        agent = self._live()
        calls, names = _check_lookup(external_lookup, sync=False)
        prepared = _prepare(code, inputs, external_lookup, names)
        self._printer.callback = _printer_for(print_callback)
        self._refused = None
        try:
            step = await self._start(agent, prepared)
            while isinstance(step, ToolCall):
                unpacked = _unpack(step)
                if unpacked is None:
                    value, error = _MISSING, _not_available(None)
                else:
                    try:
                        # Bounded by the run: a limit that kills the worker while the
                        # external runs ends the feed now, not when the external returns.
                        value, error = await agent._until_run_ends(  # noqa: SLF001
                            self._call_external(calls.get(unpacked[0]), *unpacked)
                        )
                    except RuntimeError as exc:
                        value, error = _MISSING, exc
                step = await self._resume(agent, step, value, error)
            return self._finish(step)
        except asyncio.CancelledError:
            if not agent.is_closed():
                agent._abort("the feed was cancelled; the worker was killed")  # noqa: SLF001
            raise
        finally:
            self._printer.callback = None

    @_exclusive_async
    async def feed_start(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        external_lookup: dict[str, Any] | None = None,
        print_callback: Callable[[Literal["stdout", "stderr"], str], Any] | None = None,
    ) -> AsyncPydenoSnapshot | PydenoComplete:
        """See `PydenoSession.feed_start`."""
        agent = self._live()
        calls, names = _check_lookup(external_lookup, sync=False)
        prepared = _prepare(code, inputs, external_lookup, names)
        self._printer.callback = _printer_for(print_callback)
        try:
            return await self._step(
                await self._start(agent, prepared), calls, frozenset(calls)
            )
        except BaseException:
            self._printer.callback = None
            raise

    # -- durability ----------------------------------------------------------

    @_exclusive_async
    async def dump(self, *, associated_data: bytes = b"") -> bytes:
        """See `PydenoSession.dump` (and its `associated_data`)."""
        if not isinstance(associated_data, (bytes, bytearray)):
            raise TypeError("associated_data must be bytes")
        try:
            return await self._dumpable().dump(
                self._pool._key,  # noqa: SLF001
                associated_data=associated_data,
            )
        except PydenoError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PydenoError(f"cannot dump this session: {exc}", exc) from exc

    @_exclusive_async
    async def load_session(self, state: bytes, *, associated_data: bytes = b"") -> None:
        """See `PydenoSession.load_session`."""
        await self._replace(state, suspended=False, associated_data=associated_data)

    @_exclusive_async
    async def load_snapshot(
        self,
        state: bytes,
        *,
        print_callback: Callable[[Literal["stdout", "stderr"], str], Any] | None = None,
        external_lookup: dict[str, Any] | None = None,
        associated_data: bytes = b"",
    ) -> AsyncPydenoSnapshot:
        """See `PydenoSession.load_snapshot`."""
        calls, _ = _check_lookup(external_lookup, sync=False)
        agent = await self._replace(
            state, suspended=True, associated_data=associated_data
        )
        self._printer.callback = _printer_for(print_callback)
        step = agent.pending
        assert step is not None
        snapshot = await self._step(
            step,
            calls,
            None if external_lookup is None else frozenset(calls),
            pending=True,
        )
        assert isinstance(snapshot, AsyncPydenoSnapshot)
        return snapshot

    async def _replace(
        self, state: bytes, *, suspended: bool, associated_data: bytes = b""
    ) -> AsyncAgentSandbox:
        old = self._agent
        if old is None:
            self._live()
        self._printer.callback = None
        assert old is not None

        async def release() -> None:
            # See `PydenoSession._replace`: only with a worker cap.
            if not old._core.closed:  # noqa: SLF001
                old._core.kill("the session loaded new state")  # noqa: SLF001
            await old.close()

        new = await self._pool._load(  # noqa: SLF001
            state,
            self._limits,
            associated_data,
            suspended=suspended,
            release=release,
        )
        if (new.pending is not None) != suspended:
            await new.close()
            raise _wrong_load(suspended)
        new._core.console.user = self._printer  # noqa: SLF001
        new._core.rt._handler_executor = self._console  # noqa: SLF001
        self._agent = new
        await old.close()
        return new

    # -- internals -----------------------------------------------------------

    async def _start(self, agent: AsyncAgentSandbox, prepared: _Prepared) -> Any:
        step = await agent.start(prepared.source)
        if not (isinstance(step, Failed) and _is_js_syntax(step.error)):
            return step
        if await self._compiles(agent, prepared.source):
            return step
        if prepared.fallback is not None:
            step = await agent.start(prepared.fallback)
            if not (isinstance(step, Failed) and _is_js_syntax(step.error)):
                return step
            if await self._compiles(agent, prepared.fallback):
                return step
        raise _failure(step.error, compile_time=True) from None

    @staticmethod
    async def _compiles(agent: AsyncAgentSandbox, source: str) -> bool:
        try:
            return await agent._core.rt.eval(_compile_check(source)) is not False  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    async def _resume(
        agent: AsyncAgentSandbox,
        step: ToolCall,
        value: Any,
        error: BaseException | None,
    ) -> Any:
        if error is None:
            try:
                return await agent.resume(step, value)
            except TypeError as exc:
                return await agent.resume(step, error=exc)
        return await agent.resume(step, error=error)

    async def _call_external(
        self, fn: Any, name: str, args: tuple[Any, ...]
    ) -> tuple[Any, BaseException | None]:
        if fn is None:
            return _MISSING, _not_available(name)
        try:
            # Each call in a fresh copy of the caller's context, marked as this session's tool.
            context = contextvars.copy_context()
            context.run(_TOOL_OF.set, self._sid)
            if inspect.iscoroutinefunction(fn):
                result = await context.run(
                    asyncio.get_running_loop().create_task, fn(*args)
                )
            else:
                # A plain function may block: never on the loop, and never on a thread another
                # session uses (its thread-locals, or a wedged call, stay this session's).
                try:
                    submitted = self._tools.submit(context.run, fn, *args)
                except _ThreadsExhausted as exc:
                    self._refused = exc
                    self._refusals += 1
                    if self._refusals == 1:  # once per session; the rest are counted
                        _log.warning(
                            "pydeno: session %d: a tool call was refused: %s",
                            self._sid,
                            exc,
                        )
                    return _MISSING, _unavailable()
                result = await asyncio.wrap_future(submitted)
                if inspect.isawaitable(result):
                    result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the guest sees the failure
            return _MISSING, exc
        return result, None

    def _finish(self, step: Any) -> Any:
        if isinstance(step, Done):
            return _output(step.value)
        assert isinstance(step, Failed)
        error = _ended(step.error, self._agent)
        if self._refused is not None and not isinstance(error, PydenoCrashedError):
            raise ToolThreadLimitError(str(self._refused), self._refused) from error
        raise error from None

    async def _step(
        self,
        step: Any,
        calls: dict[str, Any],
        declared: frozenset[str] | None,
        *,
        pending: bool = False,
    ) -> AsyncPydenoSnapshot | PydenoComplete:
        agent = self._agent
        assert agent is not None
        while isinstance(step, ToolCall):
            unpacked = _unpack(step)
            if unpacked is not None:
                if pending or declared is None or unpacked[0] in declared:
                    return AsyncPydenoSnapshot(
                        self, step, unpacked[0], unpacked[1], calls, declared
                    )
                # Not a function this feed declared (a stub left by an earlier feed, or a
                # name passed straight to the dispatcher): refused, as `feed_run` does.
                step = await agent.resume(step, error=_not_available(unpacked[0]))
                continue
            step = await agent.resume(step, error=_not_available(None))
        self._printer.callback = None
        return PydenoComplete(self._finish(step))

    @_exclusive_async
    async def _answer(
        self, snapshot: AsyncPydenoSnapshot, value: Any, error: BaseException | None
    ) -> AsyncPydenoSnapshot | PydenoComplete:
        agent = self._live()
        try:
            step = await self._resume(agent, snapshot._call, value, error)  # noqa: SLF001
        except BaseException:
            self._printer.callback = None
            raise
        return await self._step(step, snapshot._lookup, snapshot._declared)  # noqa: SLF001

    def __repr__(self) -> str:
        state = (
            "not checked out"
            if not self._entered
            else "closed"
            if self._agent is None or self._agent.is_closed()
            else "open"
        )
        return f"AsyncPydenoSession({self.script_name!r}, {state})"
