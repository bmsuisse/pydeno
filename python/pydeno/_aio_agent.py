"""`AsyncAgentSandbox`: `AgentSandbox` for an asyncio event loop.

The same session (tools, budget, pause/resume at tool calls, frozen clock, seeded `Math.random`,
signed journal, deterministic replay), on `AsyncIsolatedRuntime` instead of `IsolatedRuntime`:

* no thread per session. `AgentSandbox` owns an event-loop thread per session (its tools' shims
  run there); this class runs its shims, its run task and its caller on the caller's loop, and the
  worker is supervised by the one supervisor task `AsyncIsolatedRuntime` keeps per loop;
* every method is a coroutine, so nothing blocks the loop: synchronous tools run on the shared
  handler thread pool, large journals are signed and parsed on the codec thread;
* cancelling the task that awaits `start`, `resume` or `run` SIGKILLs the worker before the
  `CancelledError` propagates, and closes the session. `close()` (and `async with`) end a session
  that is running or paused at a tool call the same way.

Journals are byte-for-byte the format `AgentSandbox` writes: one written here loads there and the
other way round. Everything the guest sends is untrusted data, exactly as for `AgentSandbox`.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import inspect
import itertools
import json
import secrets
from collections.abc import Callable, Generator, Mapping
from datetime import datetime, timezone
from typing import Any

from . import _aio
from ._agent import (
    _JOURNAL_FORMAT,
    _MAC_LEN,
    _MAGIC,
    _MAX_ABANDONED_CALLS,
    _MISSING,
    _OWNED_OPTIONS,
    _SESSION_IDS,
    DEFAULT_MAX_JOURNAL_BYTES,
    DEFAULT_MAX_PAUSE,
    DEFAULT_TIMEOUT,
    Done,
    Failed,
    JournalError,
    ReplayDivergence,
    Step,
    ToolCall,
    _check_tools,
    _decode,
    _encode,
    _error_class,
    _open,
    _outcome,
    _parse,
    _prelude,
    _Run,
    _seal,
    _wrap,
    describe_tools,
    typescript_stubs,
)
from ._aio import AsyncIsolatedRuntime
from ._isolated import WorkerCrashed
from ._pydeno import RuntimeConfig
from ._snapshot_auth import _engine_version
from ._tools import ToolBridge, ToolBudgetError

__all__ = ["AsyncAgentSandbox"]

# Journals bigger than this are serialised, signed, verified and parsed on the codec thread, not on
# the loop (8 MiB of JSON is tens of milliseconds of CPU).
_OFFLOAD_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# pieces shared by both session classes' logic (kept free of any I/O)
# ---------------------------------------------------------------------------


def _settings(
    tools: Mapping[str, Callable[..., Any]],
    max_tool_calls: int | None,
    namespace: str | None,
    clock: datetime | float | int | None,
    random_seed: int | None,
    max_journal_bytes: int,
    runtime_options: Mapping[str, Any],
    who: str,
) -> tuple[dict[str, Callable[..., Any]], int, int]:
    """`AgentSandbox.__init__`'s validation: (checked tools, clock in ms, random seed)."""
    checked = _check_tools(tools)
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
            f"{who} sets {sorted(owned)} itself (use clock=, random_seed=, timeout=, max_pause=)"
        )
    config = runtime_options.get("config")
    if isinstance(config, RuntimeConfig) and config.timeout is not None:
        raise ValueError(
            f"RuntimeConfig.timeout is not supported by {who}: it would count the time a run is "
            f"paused at a tool call. Use {who}(timeout=...)."
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
    return checked, clock_ms, random_seed


def _answer(
    value: Any, error: BaseException | None, redact: bool
) -> tuple[list[Any], Any, BaseException | None]:
    """A tool answer as (journal record, value to send, error to send): exactly what a replay of
    that record will send, so the live run and its replay cannot differ."""
    if (value is _MISSING) == (error is None):
        raise TypeError("resume() takes exactly one of value= or error=")
    if error is not None:
        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception instance")
        name = type(error).__name__
        message = "host function failed" if redact else str(error)
        return ["ans", "e", name, message], None, _error_class(name)(message)
    encoded = _encode(
        value
    )  # a TypeError here is the caller's to fix; nothing was answered
    return ["ans", "v", encoded], _decode(encoded), None


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


def _open_journal(blob: bytes, key: bytes, associated_data: bytes) -> dict[str, Any]:
    return _parse(_open(blob, key, associated_data))


def _check_journal(
    journal: dict[str, Any],
    tools: Mapping[str, Callable[..., Any]],
    options: Mapping[str, Any],
) -> dict[str, Callable[..., Any]]:
    """`AgentSandbox.load`'s checks, made before any worker starts."""
    config = journal["config"]
    made_by = _engine_version().decode(errors="replace")
    if config["release"] != made_by:
        raise JournalError(
            f"the journal was recorded by pydeno {config['release']!r}, this is {made_by!r}"
        )
    if bool(options.get("redact_host_errors", True)) != config["redact"]:
        raise JournalError(
            "the journal was recorded with a different redact_host_errors setting"
        )
    checked = _check_tools(tools)
    if list(checked) != config["tools"]:
        raise JournalError(
            f"the journal was recorded with tools {config['tools']}, not {list(checked)}"
        )
    for owned in ("clock", "random_seed", "max_tool_calls", "namespace"):
        if owned in options:
            raise TypeError(f"{owned} comes from the journal")
    return checked


def _replay_plan(
    records: list[list[Any]],
) -> Generator[tuple[Any, ...], Step, None]:
    """`AgentSandbox._replay` without the driving: yields each input to apply, either
    ``("run", code)`` or ``("ans", tool_call, value, error)``, is sent the step it produced, and
    raises `ReplayDivergence` the moment an outcome differs from the recorded one. A sync or an
    async session can drive it."""
    step: Step | None = None
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
                step = yield ("run", input_)
            else:
                assert isinstance(step, ToolCall)
                if input_[1] == "v":
                    step = yield ("ans", step, _decode(input_[2]), None)
                else:
                    step = yield (
                        "ans",
                        step,
                        _MISSING,
                        _error_class(input_[2])(input_[3]),
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


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


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
        self, rt: AsyncIsolatedRuntime, session_id: int, max_tool_calls: int | None
    ) -> None:
        self.rt = rt
        self.session_id = session_id
        self.max_tool_calls = max_tool_calls
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

    async def execute(self, run: _AsyncRun, code: str) -> None:
        try:
            final: Step = Done(await self.rt.eval(_wrap(code)))
        except asyncio.CancelledError:
            self.finish(run, Failed(WorkerCrashed("the run was cancelled")))
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is the run's outcome
            final = Failed(exc)
        self.finish(run, final)

    def finish(self, run: _AsyncRun, final: Step) -> None:
        if run.final is not None:
            return
        run.final = final
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


class AsyncAgentSandbox:
    """`AgentSandbox` for asyncio: the same session, driven by coroutines on the caller's loop.

    Takes exactly `AgentSandbox`'s arguments (``runtime_options`` go to `AsyncIsolatedRuntime`,
    which also accepts ``handler_executor``: synchronous tools run there, or on its shared pool).
    Constructing the object validates them and starts nothing; start the worker with
    ``async with AsyncAgentSandbox(...) as sb`` or ``sb = await AsyncAgentSandbox.create(...)``.

    Semantics, limits and errors are `AgentSandbox`'s. The differences are what asyncio implies:

    * cancelling a `start`, `resume` or `run` that is in progress kills the worker and closes the
      session (V8 cannot be interrupted mid-command, and a worker left running would answer the
      next command with the previous one's frames); the `CancelledError` propagates;
    * `close()` on a session that is running or paused at a tool call kills the worker at once
      rather than asking it to exit;
    * a session belongs to the event loop it was started on;
    * calling into a session from one of its own tools raises `RuntimeError` ("busy").
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
        self._tools, self._clock_ms, self._random_seed = _settings(
            tools,
            max_tool_calls,
            namespace,
            clock,
            random_seed,
            max_journal_bytes,
            runtime_options,
            "AsyncAgentSandbox",
        )
        self._namespace = namespace
        self._max_tool_calls = max_tool_calls
        self._max_journal_bytes = max_journal_bytes
        self._redact = bool(runtime_options.get("redact_host_errors", True))
        self._executor = runtime_options.get("handler_executor")
        self._records: list[list[Any]] | None = []
        self._journal_size = 0
        self._dead = False
        self._busy = False
        self._started = False
        self._paused: ToolCall | None = None
        self._run: _AsyncRun | None = None
        rt = AsyncIsolatedRuntime(
            clock=self._clock_ms / 1000,
            random_seed=self._random_seed,
            request_timeout=timeout,
            max_host_wait=max_pause,
            **runtime_options,
        )
        self._core = _Core(rt, next(_SESSION_IDS), max_tool_calls)

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    async def create(
        cls, tools: Mapping[str, Callable[..., Any]], **options: Any
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
            await core.rt.eval(_prelude(list(self._tools), self._namespace))
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
        self._dead = True
        self._paused = None
        self._core.kill(why)

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
            self._dead = True
            return True
        return False

    def describe_tools(self) -> str:
        """See the module-level `describe_tools`."""
        return describe_tools(self._tools, namespace=self._namespace)

    def typescript_stubs(self) -> str:
        """See the module-level `typescript_stubs`."""
        return typescript_stubs(self._tools, namespace=self._namespace)

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
            self._abort("run() was cancelled; the worker was killed")
            raise
        finally:
            self._busy = False
        if isinstance(step, Failed):
            raise step.error
        return step.value

    async def _call_tool(self, call: ToolCall) -> Any:
        tool = self._tools[call.name]
        if inspect.iscoroutinefunction(tool):
            result = await tool(*call.args)
        else:
            # A plain function may block (a database driver, `requests`): never on the loop. It
            # sees the caller's contextvars, as with `asyncio.to_thread`.
            context = contextvars.copy_context()
            result = await asyncio.get_running_loop().run_in_executor(
                self._executor or _aio._pool("handlers"),  # noqa: SLF001
                context.run,
                tool,
                *call.args,
            )
            if inspect.isawaitable(result):
                result = await result
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
        record, sent_value, sent_error = _answer(value, error, self._redact)
        self._record(record)
        run = self._run
        assert run is not None
        self._paused = None
        self._core.answer(run, step.call_id, sent_value, sent_error)
        return self._observe(await self._core.next_step(run))

    def _observe(self, step: Step) -> Step:
        kind, digest = _outcome(step)
        self._record(["obs", kind, digest])
        if isinstance(step, ToolCall):
            self._paused = step
        else:
            self._paused = None
            if self._core.rt.is_closed():
                # The worker is gone (crash, hard timeout, memory kill, `max_pause`).
                self._dead = True
                self._core.kill("the session's worker is gone")
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

    async def dump(self, key: bytes, *, associated_data: bytes = b"") -> bytes:
        """The session's journal, HMAC-SHA256-signed with `key`. See `AgentSandbox.dump`; the
        bytes are interchangeable with it."""
        self._enter()
        try:
            self.is_closed()  # notices a worker that died behind the session's back
            if self._dead:
                raise JournalError(
                    "the session's worker is gone; its last run cannot be replayed (dump after "
                    "each step you want to be able to return to)"
                )
            if self._records is None:
                raise JournalError(
                    f"the journal grew past max_journal_bytes={self._max_journal_bytes}"
                )
            config = {
                "clock_ms": self._clock_ms,
                "random_seed": self._random_seed,
                "max_tool_calls": self._max_tool_calls,
                "namespace": self._namespace,
                "tools": list(self._tools),
                "release": _engine_version().decode(errors="replace"),
                "redact": self._redact,
            }
            records = list(self._records)
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
        tools: Mapping[str, Callable[..., Any]],
        *,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        associated_data: bytes = b"",
        **options: Any,
    ) -> AsyncAgentSandbox:
        """Rebuild a session from a journal (`dump()` output of this class or of `AgentSandbox`)
        by replaying it on a fresh worker. See `AgentSandbox.load`."""
        if not isinstance(blob, (bytes, bytearray)):
            raise TypeError("blob must be bytes")
        if len(blob) > max_journal_bytes * 2 + len(_MAGIC) + _MAC_LEN + 4096:
            raise JournalError("journal is larger than max_journal_bytes allows")
        blob = bytes(blob)
        if len(blob) > _OFFLOAD_BYTES:
            journal = await asyncio.get_running_loop().run_in_executor(
                _aio._pool("codec"),  # noqa: SLF001
                _open_journal,
                blob,
                key,
                associated_data,
            )
        else:
            journal = _open_journal(blob, key, associated_data)
        tools = _check_journal(journal, tools, options)
        config = journal["config"]
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
            await session._open()
            await session._replay(journal["records"])
        except JournalError:
            await session.close()
            raise
        except (ValueError, TypeError, OverflowError) as exc:
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
        try:
            request = next(plan)
            while True:
                if request[0] == "run":
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
