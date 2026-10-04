"""`AsyncAgentSandbox`: `AgentSandbox` for an asyncio event loop.

The same session (tools, schema tools, the lazy catalog, budget, pause/resume at tool calls,
results with console capture, frozen clock, seeded `Math.random`, signed journal with crash
recovery, deterministic replay), on `AsyncIsolatedRuntime` instead of `IsolatedRuntime`. All of
that logic is `_agent._SessionBase`, shared with `AgentSandbox`; this module only drives it:

* no thread per session. `AgentSandbox` owns an event-loop thread per session (its tools' shims
  run there); this class runs its shims, its run task and its caller on the caller's loop, and the
  worker is supervised by the one supervisor task `AsyncIsolatedRuntime` keeps per loop;
* every method is a coroutine, so nothing blocks the loop: synchronous tools run on the shared
  handler thread pool, large journals are signed and parsed on the codec thread;
* cancelling the task that awaits `start`, `resume`, `run` or `execute` SIGKILLs the worker before
  the `CancelledError` propagates, and closes the session. `close()` (and `async with`) end a
  session that is running or paused at a tool call the same way.

Journals are byte-for-byte the format `AgentSandbox` writes: one written here loads there and the
other way round. Everything the guest sends is untrusted data, exactly as for `AgentSandbox`.
"""

from __future__ import annotations

import asyncio
import collections
import collections.abc
import contextvars
import dataclasses
import inspect
import itertools
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from . import _aio
from ._agent import (
    _CATALOG_CALL,
    _MAX_ABANDONED_CALLS,
    _MISSING,
    _SESSION_IDS,
    DEFAULT_MAX_JOURNAL_BYTES,
    DEFAULT_MAX_PAUSE,
    DEFAULT_TIMEOUT,
    Done,
    Failed,
    JournalError,
    Step,
    ToolCall,
    ToolNotDiscoveredError,
    _ConsoleSink,
    _open_journal,
    _prelude,
    _public,
    _replay_plan,
    _Run,
    _seal_journal,
    _SessionBase,
    _wrap,
)
from ._aio import AsyncIsolatedRuntime
from ._isolated import WorkerCrashed
from ._result import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    ExecutionResult,
    OutputCapture,
    bounded_result,
)
from ._tools import ToolBudgetError

__all__ = ["AsyncAgentSandbox"]

# Journals bigger than this are serialised, signed, verified and parsed on the codec thread, not on
# the loop (8 MiB of JSON is tens of milliseconds of CPU).
_OFFLOAD_BYTES = 1024 * 1024


class _AsyncRun(_Run):
    __slots__ = ("wake",)

    def __init__(self) -> None:
        super().__init__()
        self.wake = asyncio.Event()


class _Core:
    """What the bound tool shims touch. It never refers to the `AsyncAgentSandbox`, so a session
    dropped without `close()` can be collected (and `AsyncIsolatedRuntime`'s finalizer then kills
    its worker)."""

    def __init__(
        self,
        rt: AsyncIsolatedRuntime,
        session_id: int,
        max_tool_calls: int | None,
        *,
        console: _ConsoleSink,
        max_output_bytes: int,
        max_result_bytes: int,
        catalog: frozenset[str],
    ) -> None:
        self.rt = rt
        self.session_id = session_id
        self.max_tool_calls = max_tool_calls
        self.console = console
        self.max_output_bytes = max_output_bytes
        self.max_result_bytes = max_result_bytes
        self.catalog = catalog
        self.discovered: set[str] = set()
        self.calls_made = 0
        self.ids = itertools.count(1)
        self.run: _AsyncRun | None = None
        self.abandoned: list[asyncio.Future[Any]] = []
        self.closed = False
        self.task: asyncio.Task[None] | None = None

    async def on_tool_call(self, name: str, args: list[Any]) -> Any:
        """A bound tool, as the guest sees it (see `_agent._Core.on_tool_call`)."""
        if self.max_tool_calls is not None and self.calls_made >= self.max_tool_calls:
            raise ToolBudgetError(
                f"tool call budget exhausted ({self.max_tool_calls} calls); refused {name!r}"
            )
        self.calls_made += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        run = self.run
        if run is None or run.final is not None or self.closed:
            if len(self.abandoned) >= _MAX_ABANDONED_CALLS:
                raise RuntimeError("too many abandoned tool calls in this session")
            self.abandoned.append(future)
        else:
            call = ToolCall(name, tuple(args), next(self.ids), self.session_id)
            run.pending[call.call_id] = future
            run.events.append(call)
            run.wake.set()
        return await future

    async def on_catalog_call(self, name: Any, args: list[Any]) -> Any:
        """See `_agent._Core.on_catalog_call`."""
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

    async def execute(self, run: _AsyncRun, code: str) -> None:
        capture = OutputCapture(self.max_output_bytes)
        self.console.capture = capture
        cancelled = False
        try:
            value = await self.rt.eval(_wrap(code))
            # Over the cap, the run fails but the session goes on (the value is dropped here).
            bounded_result(value, self.max_result_bytes)
            final: Step = Done(value)
        except asyncio.CancelledError:
            cancelled = True
            final = Failed(WorkerCrashed("the run was cancelled"))
        except Exception as exc:  # noqa: BLE001 - every failure is the run's outcome
            final = Failed(exc)
        finally:
            # Console calls are synchronous host calls: every one was answered before the
            # command's result arrived.
            self.console.capture = None
        self.finish(
            run,
            dataclasses.replace(
                final,
                stdout=capture.stdout,
                stderr=capture.stderr,
                truncated=capture.truncated,
            ),
        )
        if cancelled:
            raise asyncio.CancelledError

    def finish(self, run: _AsyncRun, final: Step) -> None:
        if run.final is not None:
            return
        run.final = final
        # Calls still unanswered now belong to nobody (see `_agent._Core.execute`).
        run.events = collections.deque(
            e for e in run.events if not isinstance(e, ToolCall)
        )
        self.abandoned.extend(f for f in run.pending.values() if not f.done())
        run.pending.clear()
        run.events.append(final)
        run.wake.set()

    def answer(
        self, run: _AsyncRun, call_id: int, value: Any, error: BaseException | None
    ) -> bool:
        future = run.pending.pop(call_id, None)
        if future is None or future.done() or run.final is not None:
            return False
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)
        return True

    async def next_step(self, run: _AsyncRun) -> Step:
        while True:
            if run.events:
                return run.events.popleft()
            if self.closed:
                return Failed(RuntimeError("the session was closed"))
            run.wake.clear()
            await run.wake.wait()

    def in_use(self) -> bool:
        """A run is in progress or paused at a tool call: the worker is mid-command."""
        return self.run is not None and self.run.final is None

    def release_calls(self) -> None:
        """Cancel every unanswered tool call, so no shim task is left waiting forever."""
        futures = list(self.abandoned)
        self.abandoned.clear()
        if self.run is not None:
            futures.extend(self.run.pending.values())
            self.run.pending.clear()
        for future in futures:
            if not future.done():
                future.cancel()

    def kill(self, why: str) -> None:
        """End the session now, without awaiting: SIGKILL the worker (the loop's supervisor reaps
        it), wake whoever waits on the run, cancel the shims' pending calls."""
        self.closed = True
        self.rt._kill((WorkerCrashed, why))  # noqa: SLF001
        self.rt._close_stderr()  # noqa: SLF001
        self.release_calls()
        if self.run is not None:
            self.run.wake.set()

    def shutdown(self) -> None:
        """`_SessionBase._observe`'s hook: the worker died during a run."""
        self.kill("the session's worker is gone")


class AsyncAgentSandbox(_SessionBase):
    """`AgentSandbox` for asyncio: the same session, driven by coroutines on the caller's loop.

    Takes exactly `AgentSandbox`'s arguments (``runtime_options`` go to `AsyncIsolatedRuntime`,
    which also accepts ``handler_executor``: synchronous tools and the console capture run there,
    or on its shared pool). Constructing the object validates them and starts nothing; start the
    worker with ``async with AsyncAgentSandbox(...) as sb`` or
    ``sb = await AsyncAgentSandbox.create(...)``.

    Semantics, limits, results, journals and errors are `AgentSandbox`'s. The differences are
    what asyncio implies:

    * cancelling a `start`, `resume`, `run` or `execute` that is in progress kills the worker and
      closes the session (V8 cannot be interrupted mid-command, and a worker left running would
      answer the next command with the previous one's frames); the `CancelledError` propagates,
      and `dump()` then returns the journal as of the last good run plus a ``lost`` record, as
      after a crash;
    * `close()` on a session that is running or paused at a tool call kills the worker at once
      rather than asking it to exit;
    * a session belongs to the event loop it was started on;
    * calling into a session from one of its own tools raises `RuntimeError` ("busy").
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
        self._executor = runtime_options.get("handler_executor")
        self._busy = False
        self._started = False
        rt = AsyncIsolatedRuntime(
            rt_config,
            clock=self._clock_ms / 1000,
            random_seed=self._random_seed,
            request_timeout=timeout,
            max_host_wait=max_pause,
            **runtime_options,
        )
        self._core = _Core(
            rt,
            next(_SESSION_IDS),
            max_tool_calls,
            console=sink,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
            catalog=frozenset(self._catalog),
        )

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    async def create(
        cls, tools: Mapping[str, Any] | collections.abc.Sequence[Any], **options: Any
    ) -> AsyncAgentSandbox:
        """Construct a session and start its worker, without blocking the event loop."""
        session = cls(tools, **options)
        await session._open()
        return session

    async def __aenter__(self) -> AsyncAgentSandbox:
        if not self._started:
            await self._open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _open(self) -> None:
        if self._started or self._core.closed:
            raise RuntimeError("this AsyncAgentSandbox has already been started")
        self._started = True
        core = self._core
        try:
            await core.rt.__aenter__()

            def shim_for(name: str) -> Callable[..., Any]:
                async def shim(*args: Any) -> Any:
                    return await core.on_tool_call(name, list(args))

                shim.__name__ = name
                return shim

            shims = {name: shim_for(name) for name in self._tools}
            if self._namespace is None:
                for name, shim in shims.items():
                    await core.rt.bind_function(name, shim)
            elif shims:
                await core.rt.bind_object(self._namespace, shims)
            if self._catalog:

                async def catalog_call(name: Any = None, *args: Any) -> Any:
                    return await core.on_catalog_call(name, list(args))

                await core.rt.bind_function(_CATALOG_CALL, catalog_call)
            await core.rt.eval(
                _prelude(
                    list(self._tools),
                    self._namespace,
                    self._catalog_ns if self._catalog else None,
                )
            )
        except BaseException:
            self._dead = True
            core.kill("the session failed to start")
            raise

    async def close(self) -> None:
        """Stop the worker. Idempotent; safe while running or paused (the worker is killed)."""
        core = self._core
        self._paused = None
        if core.in_use() and not core.closed:
            core.kill("the session was closed while a run was in progress")
        else:
            core.closed = True
            core.release_calls()
        await core.rt.close()

    def _abort(self, why: str) -> None:
        """A cancellation: the run in progress is lost, with what it spent (see `dump`)."""
        self._mark_dead("WorkerCrashed")
        self._core.kill(why)

    # -- introspection -------------------------------------------------------

    @property
    def worker_pid(self) -> int | None:
        """The worker's process id (None before the session is started)."""
        proc = self._core.rt._proc  # noqa: SLF001
        return None if proc is None else proc.pid

    def is_closed(self) -> bool:
        """True once the session can run nothing more: closed, or its worker is gone (crashed,
        killed, timed out, or exited behind the session's back)."""
        if self._core.closed or self._dead:
            return True
        proc = self._core.rt._proc  # noqa: SLF001
        if proc is not None and proc.poll() is not None:
            # Died while idle (nothing to lose), or with a run begun since the last checkpoint
            # (paused at a tool call, or ended by the death but not yet observed): that run is lost.
            begun = self._core.calls_made > self._checkpoint_calls or (
                self._records is not None and len(self._records) > self._checkpoint
            )
            self._mark_dead("WorkerCrashed" if begun else None)
            return True
        return False

    # -- running -------------------------------------------------------------

    def _enter(self) -> None:
        if not self._started:
            raise RuntimeError(
                "AsyncAgentSandbox is not started: use `async with` or "
                "`await AsyncAgentSandbox.create(...)`"
            )
        if asyncio.get_running_loop() is not self._core.rt._loop:  # noqa: SLF001
            raise RuntimeError(
                "an AsyncAgentSandbox belongs to the event loop it was started on"
            )
        if self._busy:
            raise RuntimeError(
                "AsyncAgentSandbox is busy (another task is using it, or a tool called back "
                "into its own session)"
            )
        self._busy = True

    async def start(self, code: str) -> Step:
        """Run `code` until it calls a tool (`ToolCall`), finishes (`Done`) or fails (`Failed`).
        See `AgentSandbox.start`."""
        self._enter()
        try:
            return await self._start(code)
        except asyncio.CancelledError:
            self._abort("start() was cancelled; the worker was killed")
            raise
        finally:
            self._busy = False

    async def resume(
        self,
        step: ToolCall,
        value: Any = _MISSING,
        *,
        error: BaseException | None = None,
    ) -> Step:
        """Answer the tool call the session is paused at. See `AgentSandbox.resume`."""
        self._enter()
        try:
            return await self._resume(step, value, error)
        except asyncio.CancelledError:
            self._abort("resume() was cancelled; the worker was killed")
            raise
        finally:
            self._busy = False

    async def run(self, code: str) -> Any:
        """`start` the code and answer every tool call with the real tool (awaited if it is a
        coroutine function, on the handler thread pool if it is a plain one); return the result
        or raise what the run failed with. See `AgentSandbox.run`."""
        step = await self._drive(code)
        if isinstance(step, Failed):
            raise step.error
        return step.value

    async def execute(self, code: str) -> ExecutionResult:
        """`run` the code, but return an `ExecutionResult` instead of raising. See
        `AgentSandbox.execute`."""
        step = await self._drive(code)
        return step.to_result(max_error_bytes=self._max_output_bytes)

    async def _drive(self, code: str) -> Done | Failed:
        self._enter()
        try:
            step = await self._start(code)
            while isinstance(step, ToolCall):
                try:
                    result = await self._call_tool(step)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the guest sees the failure
                    step = await self._resume(step, _MISSING, exc)
                else:
                    step = await self._resume(step, result, None)
        except asyncio.CancelledError:
            self._abort("the run was cancelled; the worker was killed")
            raise
        finally:
            self._busy = False
        return step

    async def call(self, step: ToolCall) -> Any:
        """Run the real tool for a `ToolCall` and return its result (see `AgentSandbox.call`)."""
        self._check_call(step)
        return await self._call_tool(step)

    async def _call_tool(self, call: ToolCall) -> Any:
        fn, args = self._check_call(call)
        if inspect.iscoroutinefunction(fn):
            result = await fn(*args)
        else:
            # A plain function may block (a database driver, `requests`): never on the loop. It
            # sees the caller's contextvars, as with `asyncio.to_thread`.
            context = contextvars.copy_context()
            result = await asyncio.get_running_loop().run_in_executor(
                self._executor or _aio._pool("handlers"),  # noqa: SLF001
                context.run,
                fn,
                *args,
            )
            if inspect.isawaitable(result):
                result = await result
        return self._check_result(call, result)

    async def _start(self, code: str) -> Step:
        if not isinstance(code, str):
            raise TypeError("code must be a string")
        self._check_usable()
        if self._paused is not None:
            raise RuntimeError("the session is paused at a tool call; resume it first")
        run = _AsyncRun()
        core = self._core
        self._run = run
        core.run = run
        core.task = asyncio.get_running_loop().create_task(core.execute(run, code))
        self._record(["run", code])
        return self._observe(await core.next_step(run))

    async def _resume(
        self, step: ToolCall, value: Any, error: BaseException | None
    ) -> Step:
        sent_value, sent = self._answer(step, value, error)
        run = self._run
        assert isinstance(run, _AsyncRun)
        self._core.answer(run, step.call_id, sent_value, sent)
        return self._observe(await self._core.next_step(run))

    # -- durability ----------------------------------------------------------

    async def dump(self, key: bytes, *, associated_data: bytes = b"") -> bytes:
        """The session's journal, HMAC-SHA256-signed with `key`. See `AgentSandbox.dump`; the
        bytes are interchangeable with it. After the worker died (a crash, a timeout, a kill, a
        cancellation) it is the journal as of the last good run plus a ``lost`` record."""
        self._enter()
        try:
            self.is_closed()  # notices a worker that died behind the session's back
            config, records = self._journal()
            if self._journal_size > _OFFLOAD_BYTES:
                return await asyncio.get_running_loop().run_in_executor(
                    _aio._pool("codec"),  # noqa: SLF001
                    _seal_journal,
                    config,
                    records,
                    key,
                    associated_data,
                )
            return _seal_journal(config, records, key, associated_data)
        finally:
            self._busy = False

    @classmethod
    async def load(
        cls,
        blob: bytes,
        key: bytes,
        tools: Mapping[str, Any] | collections.abc.Sequence[Any],
        *,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        associated_data: bytes = b"",
        tools_catalog: Mapping[str, Any] | collections.abc.Sequence[Any] | None = None,
        **options: Any,
    ) -> AsyncAgentSandbox:
        """Rebuild a session from a journal (`dump()` output of this class or of `AgentSandbox`)
        by replaying it on a fresh worker. See `AgentSandbox.load`."""
        if isinstance(blob, (bytes, bytearray)) and len(blob) > _OFFLOAD_BYTES:
            journal = await asyncio.get_running_loop().run_in_executor(
                _aio._pool("codec"),  # noqa: SLF001
                _open_journal,
                bytes(blob),
                key,
                associated_data,
                max_journal_bytes,
            )
        else:
            journal = _open_journal(blob, key, associated_data, max_journal_bytes)
        entries, arguments = cls._load_arguments(
            journal, tools, tools_catalog, max_journal_bytes, options
        )
        session = cls(entries, **arguments, **options)
        try:
            await session._open()
            await session._replay(journal["records"])
        except (ValueError, TypeError, OverflowError) as exc:  # as AgentSandbox.load
            await session.close()
            raise JournalError(f"malformed journal: {type(exc).__name__}") from None
        except asyncio.CancelledError:
            session._abort("load() was cancelled; the worker was killed")
            raise
        except BaseException:
            await session.close()
            raise
        return session

    async def _replay(self, records: list[list[Any]]) -> None:
        plan = _replay_plan(records)
        step: Step | None = None
        try:
            request = next(plan)
            while True:
                if request[0] == "lost":
                    self._replay_lost(request[1])
                    step = None
                elif request[0] == "run":
                    step = await self._start(request[1])
                else:
                    step = await self._resume(request[1], request[2], request[3])
                request = plan.send(step)
        except StopIteration:
            return

    def __repr__(self) -> str:
        state = (
            "closed"
            if self.is_closed()
            else "paused"
            if self._paused
            else "idle"
            if self._started
            else "not started"
        )
        return f"AsyncAgentSandbox(tools={list(self._tools)!r}, {state}, calls={self.calls_made})"
