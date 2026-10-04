"""`SandboxPool` / `AsyncSandboxPool`: isolated runtimes started ahead of time, each used once.

Starting an `IsolatedRuntime` costs tens of milliseconds, nearly all of it the worker process
starting Python, importing, creating V8, applying the OS sandbox and testing it. A pool does that
work before anyone asks: `checkout()` hands over a runtime whose worker has already passed its
handshake and sandbox self-test, which takes microseconds.

The rules that keep a pool as safe as a fresh runtime:

* **Single use.** A checked-out runtime belongs to the caller and is never returned to the pool;
  closing it kills its worker. No worker ever serves two sessions, so nothing one guest leaves
  behind (globals, a corrupted heap, a compromised process) can reach the next.
* **Same construction.** Each pooled runtime is an ordinary `IsolatedRuntime` /
  `AsyncIsolatedRuntime`, built with the pool's options: the same handshake, `sandbox="require"`
  check, self-test, limits and caps, only earlier.
* **Exhaustion is a cold start, never an error.** If every pooled runtime is taken, `checkout()`
  starts one on the spot, exactly as `IsolatedRuntime(...)` would. Replacements are started in the
  background as soon as a runtime is handed out.

Options split in two. Whatever the worker receives at start-up (the `RuntimeConfig`, `sandbox`,
`jitless`, `v8_flags`, `clock`, `random_seed`, `max_memory`, console routing, ...) is fixed per
pool: use one pool per such configuration. The options that only the parent enforces
(`SESSION_OPTIONS`: deadlines, host-call budgets, error redaction, ...) can be set per checkout.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import os
import threading
import weakref
from collections.abc import Generator
from typing import Any

from ._aio import AsyncIsolatedRuntime
from ._isolated import SESSION_OPTIONS, IsolatedRuntime, _session_options
from ._limits import limit_int
from ._pydeno import RuntimeConfig

__all__ = ["AsyncSandboxPool", "SandboxPool", "SESSION_OPTIONS"]

DEFAULT_POOL_SIZE = 4
DEFAULT_MAX_CONCURRENT_STARTS = 2
# A start that fails in the background (out of processes, a worker that could not apply its
# sandbox this once) is retried after this long, doubling up to the cap, so a persistent failure
# costs one process every `_MAX_BACKOFF` seconds rather than a spawn loop. Checkouts meanwhile fall
# back to cold starts, which report the error to the caller.
_FIRST_BACKOFF = 0.1
_MAX_BACKOFF = 30.0
# How long `close()` waits for a start already in progress (a handshake gives up after 30 s).
_JOIN_SECONDS = 35.0

_ASYNC_SESSION_OPTIONS = (*SESSION_OPTIONS, "handler_executor")


def _split(
    options: dict[str, Any], session_names: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if "prewarm" in options:
        raise TypeError("a pool always starts its workers ahead of time; drop prewarm=")
    spawn = {k: v for k, v in options.items() if k not in session_names}
    session = {k: v for k, v in options.items() if k in session_names}
    return spawn, session


def _check_sizes(size: int, max_concurrent_starts: int) -> tuple[int, int]:
    """Both as plain ints: `TypeError` for a wrong type (a bool, a float, None), `ValueError` below 1."""
    checked = []
    for name, value in (
        ("size", size),
        ("max_concurrent_starts", max_concurrent_starts),
    ):
        if value is None:
            raise TypeError(f"{name} must be a positive int")
        checked.append(limit_int(name, value, minimum=1))
    return checked[0], checked[1]  # type: ignore[return-value]


def _session_for(
    defaults: dict[str, Any], overrides: dict[str, Any], names: tuple[str, ...]
) -> dict[str, Any]:
    unknown = sorted(set(overrides) - set(names))
    if unknown:
        raise TypeError(
            f"{', '.join(unknown)} cannot be set per checkout: the worker receives it when it "
            f"starts, so it is fixed for the whole pool (per checkout: {', '.join(names)})"
        )
    return {**defaults, **overrides}


def _parent_side(session: dict[str, Any]) -> dict[str, Any]:
    """`_session_options` for everything but `handler_executor` (async only, set as is)."""
    options = _session_options(
        **{k: v for k, v in session.items() if k != "handler_executor"}
    )
    if "handler_executor" in session:
        options["_handler_executor"] = session["handler_executor"]
    return options


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


class _Core:
    """The pool's state, shared with its filler threads. The threads hold this, not the
    `SandboxPool`, so a pool that is dropped without `close()` is still collected, and its
    finalizer drains this."""

    def __init__(
        self,
        config: RuntimeConfig | None,
        spawn: dict[str, Any],
        session: dict[str, Any],
        size: int,
        max_concurrent_starts: int,
    ) -> None:
        self.config = config
        self.spawn = spawn
        self.session = session
        self.size = size
        self.fillers = max_concurrent_starts
        self.cond = threading.Condition()
        self.ready: collections.deque[IsolatedRuntime] = collections.deque()
        self.starting = 0
        self.closed = False
        self.checkouts = 0
        self.cold_starts = 0
        self.last_error: str | None = None
        self.threads: list[threading.Thread] = []
        # Set in a fork()ed child, whose copy of the pool has no workers and no filler threads:
        # the next checkout starts fresh fillers there.
        self.forked = False

    def new(self, session: dict[str, Any] | None = None) -> IsolatedRuntime:
        return IsolatedRuntime(
            self.config,
            prewarm=False,
            **self.spawn,
            **(self.session if session is None else session),
        )

    def start_fillers(self) -> None:
        for i in range(self.fillers):
            thread = threading.Thread(
                target=self.fill, name=f"pydeno-sandbox-pool-{i}", daemon=True
            )
            self.threads.append(thread)
            thread.start()

    def fill(self) -> None:
        backoff = 0.0
        while True:
            with self.cond:
                if backoff:
                    self.cond.wait_for(lambda: self.closed, timeout=backoff)
                self.cond.wait_for(
                    lambda: self.closed or len(self.ready) + self.starting < self.size
                )
                if self.closed:
                    return
                self.starting += 1
            rt: IsolatedRuntime | None = None
            error: BaseException | None = None
            try:
                rt = self.new()
            except Exception as exc:  # noqa: BLE001 - a filler must not die of one failed start
                error = exc
            with self.cond:
                self.starting -= 1
                if rt is not None and not self.closed:
                    self.ready.append(rt)
                    rt = None
                    backoff = 0.0
                    self.cond.notify_all()
                elif error is not None:
                    self.last_error = f"{type(error).__name__}: {error}"
                    backoff = min(max(_FIRST_BACKOFF, backoff * 2), _MAX_BACKOFF)
                    self.cond.notify_all()
            if rt is not None:  # the pool closed while this one was starting
                rt.close()

    def take(self) -> IsolatedRuntime | None:
        dead: list[IsolatedRuntime] = []
        rt = None
        with self.cond:
            if self.closed:
                raise RuntimeError("this SandboxPool is closed")
            self.refill_after_fork()
            while self.ready:
                candidate = self.ready.popleft()
                # A pooled worker can die while it waits (the idle watchdog, an OOM killer, a
                # signal): such a one is discarded, never handed out.
                if not candidate.is_closed() and candidate._proc.poll() is None:  # noqa: SLF001
                    rt = candidate
                    break
                dead.append(candidate)
            self.checkouts += 1
            if rt is None:
                self.cold_starts += 1
            self.cond.notify_all()  # a filler starts the replacement
        for candidate in dead:
            candidate.close()
        return rt

    def refill_after_fork(self) -> None:
        """Called with `cond` held."""
        if self.forked and not self.closed:
            self.forked = False
            self.start_fillers()

    def forget_parents(self) -> None:
        """In a fork()ed child, where no other thread exists: the pooled workers and the filler
        threads are the parent's. Let go of the pipes without signalling the workers (in a child
        `close()` only drops the inherited descriptors) and refill lazily, at the next checkout,
        rather than start threads inside a fork handler."""
        # A parent thread may have held the lock at the instant of the fork; that thread does not
        # exist here, so the lock could never be released. A fresh one cannot deadlock the child.
        self.cond = threading.Condition()
        for rt in self.ready:
            rt.close()
        self.ready.clear()
        self.starting = 0
        self.threads = []
        self.forked = True

    def close(self, *, wait: bool = True, graceful: bool = True) -> None:
        with self.cond:
            self.closed = True
            items = list(self.ready)
            self.ready.clear()
            self.cond.notify_all()
        for rt in items:
            if not graceful:
                rt._kill()  # noqa: SLF001 - at exit: no second's grace per worker
            rt.close()
        if wait:
            current = threading.current_thread()
            for thread in self.threads:
                if thread is not current:
                    thread.join(_JOIN_SECONDS)

    def stats(self) -> dict[str, Any]:
        with self.cond:
            return {
                "size": self.size,
                "ready": len(self.ready),
                "starting": self.starting,
                "checkouts": self.checkouts,
                "cold_starts": self.cold_starts,
                "last_error": self.last_error,
            }


_CORES: weakref.WeakSet[_Core] = weakref.WeakSet()


class SandboxPool:
    """Pre-started, single-use `IsolatedRuntime`s.

    Args:
        config: The `RuntimeConfig` every pooled runtime is built with.
        size: How many runtimes to keep ready (default 4). Each is a live worker process
            (tens of MB of memory).
        max_concurrent_starts: How many replacements may start at once (default 2). A higher
            value refills faster after a burst, at the cost of CPU while it does.
        **options: Any other `IsolatedRuntime` keyword argument. The ones in `SESSION_OPTIONS`
            are defaults that `checkout()` can override; the rest are fixed for the pool.

    The constructor starts the first runtime itself, so invalid options, or a platform that
    cannot give `sandbox="require"` what it demands, fail here and not in the background. The
    others start in the background; `wait_ready()` blocks until they have.

    Use `with SandboxPool(...) as pool:` or call `close()`: it kills the workers still waiting.
    Runtimes already checked out belong to their callers and are not affected.
    """

    #: The options `checkout()` accepts: the ones only the parent enforces.
    SESSION_OPTIONS = SESSION_OPTIONS
    # What builds and holds the runtimes; a subclass may build them differently (`_Core.new`).
    _core_type: type[_Core] = _Core

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        size: int = DEFAULT_POOL_SIZE,
        max_concurrent_starts: int = DEFAULT_MAX_CONCURRENT_STARTS,
        **options: Any,
    ) -> None:
        size, max_concurrent_starts = _check_sizes(size, max_concurrent_starts)
        spawn, session = _split(options, SESSION_OPTIONS)
        _session_options(
            **session
        )  # a bad default fails now, not at the first checkout
        core = self._core_type(
            config, spawn, session, size, min(size, max_concurrent_starts)
        )
        core.ready.append(core.new())
        core.start_fillers()
        self._core = core
        _CORES.add(core)
        self._finalizer = weakref.finalize(self, core.close, wait=False)

    def checkout(self, **session_options: Any) -> IsolatedRuntime:
        """A ready `IsolatedRuntime`, now yours alone; close it (or use `with`) when done.

        `session_options` override the pool's defaults for this runtime only (`SESSION_OPTIONS`:
        `request_timeout`, `max_host_calls`, ...). If no runtime is ready, one is started here
        (a cold start); that is slower, never an error, unless starting itself fails."""
        core = self._core
        session = _session_for(core.session, session_options, SESSION_OPTIONS)
        options = _session_options(**session) if session_options else None
        rt = core.take()
        if rt is None:
            return core.new(session)
        if options is not None:
            rt._apply_session(options)  # noqa: SLF001
        return rt

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until `size` runtimes are ready (True), or `timeout` seconds pass (False)."""
        core = self._core
        with core.cond:
            core.refill_after_fork()
            return (
                core.cond.wait_for(
                    lambda: core.closed or len(core.ready) >= core.size, timeout
                )
                and not core.closed
            )

    def stats(self) -> dict[str, Any]:
        """`size`, `ready`, `starting`, `checkouts`, `cold_starts` (checkouts that found the pool
        empty) and `last_error` (the last background start that failed, or None)."""
        return self._core.stats()

    def close(self) -> None:
        """Kill every runtime still waiting in the pool and stop refilling. Idempotent."""
        self._finalizer.detach()
        self._core.close()

    def __enter__(self) -> SandboxPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _forget_parents_pools() -> None:
    for core in list(_CORES):
        core.forget_parents()


def _close_all_at_exit() -> None:
    for core in list(_CORES):
        try:
            core.close(wait=False, graceful=False)
        except Exception:  # noqa: BLE001, S110
            pass


os.register_at_fork(after_in_child=_forget_parents_pools)
atexit.register(_close_all_at_exit)


# ---------------------------------------------------------------------------
# asyncio
# ---------------------------------------------------------------------------


class _AsyncCheckout:
    """What `AsyncSandboxPool.checkout()` returns: `await` it for the runtime, or use it as
    `async with pool.checkout() as rt:`, which also closes the runtime at the end."""

    def __init__(self, pool: AsyncSandboxPool, overrides: dict[str, Any]) -> None:
        self._pool = pool
        self._overrides = overrides
        self._rt: AsyncIsolatedRuntime | None = None

    def __await__(self) -> Generator[Any, None, AsyncIsolatedRuntime]:
        return self._pool._checkout(self._overrides).__await__()  # noqa: SLF001

    async def __aenter__(self) -> AsyncIsolatedRuntime:
        self._rt = await self._pool._checkout(self._overrides)  # noqa: SLF001
        return self._rt

    async def __aexit__(self, *exc: object) -> None:
        if self._rt is not None:
            await self._rt.close()


class AsyncSandboxPool:
    """Pre-started, single-use `AsyncIsolatedRuntime`s, for asyncio.

    The same contract as `SandboxPool` (same arguments; `handler_executor` can also be set per
    checkout). Pooled runtimes are bound to the event loop the pool was started on, so start it
    with `async with AsyncSandboxPool(...) as pool:` (or `await pool.start()`) inside that loop.
    Constructing the object starts nothing and validates the options."""

    #: The options `checkout()` accepts: `SandboxPool.SESSION_OPTIONS` and `handler_executor`.
    SESSION_OPTIONS = _ASYNC_SESSION_OPTIONS

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        size: int = DEFAULT_POOL_SIZE,
        max_concurrent_starts: int = DEFAULT_MAX_CONCURRENT_STARTS,
        **options: Any,
    ) -> None:
        size, max_concurrent_starts = _check_sizes(size, max_concurrent_starts)
        self._config = config
        self._spawn, self._session = _split(options, _ASYNC_SESSION_OPTIONS)
        # Constructing an AsyncIsolatedRuntime validates every option and starts nothing.
        AsyncIsolatedRuntime(config, prewarm=False, **self._spawn, **self._session)
        self._size = size
        self._fillers = min(size, max_concurrent_starts)
        self._ready: collections.deque[AsyncIsolatedRuntime] = collections.deque()
        self._starting = 0
        self._closed = False
        self._started = False
        self._checkouts = 0
        self._cold_starts = 0
        self._last_error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond: asyncio.Condition | None = None
        self._tasks: list[asyncio.Task[None]] = []

    async def _new(self, session: dict[str, Any] | None = None) -> AsyncIsolatedRuntime:
        return await AsyncIsolatedRuntime.create(
            self._config,
            prewarm=False,
            **self._spawn,
            **(self._session if session is None else session),
        )

    async def start(self) -> AsyncSandboxPool:
        """Start the first runtime (errors surface here) and the background refill."""
        if self._started or self._closed:
            raise RuntimeError("this AsyncSandboxPool has already been started")
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._cond = asyncio.Condition()
        try:
            self._ready.append(await self._new())
        except BaseException:
            self._closed = True
            raise
        self._tasks = [
            self._loop.create_task(self._fill()) for _ in range(self._fillers)
        ]
        return self

    async def __aenter__(self) -> AsyncSandboxPool:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _fill(self) -> None:
        assert self._cond is not None
        cond = self._cond
        backoff = 0.0
        while True:
            if backoff:
                await asyncio.sleep(backoff)
            async with cond:
                await cond.wait_for(
                    lambda: (
                        self._closed or len(self._ready) + self._starting < self._size
                    )
                )
                if self._closed:
                    return
                self._starting += 1
            rt: AsyncIsolatedRuntime | None = None
            try:
                rt = await self._new()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a filler must not die of one failed start
                self._last_error = f"{type(exc).__name__}: {exc}"
                backoff = min(max(_FIRST_BACKOFF, backoff * 2), _MAX_BACKOFF)
            finally:
                self._starting -= 1
            if rt is None:
                continue
            backoff = 0.0
            if self._closed:
                await rt.close()
                return
            self._ready.append(rt)
            async with cond:
                cond.notify_all()

    def checkout(self, **session_options: Any) -> _AsyncCheckout:
        """A ready `AsyncIsolatedRuntime`, now yours alone: `rt = await pool.checkout()` (close it
        when done) or `async with pool.checkout() as rt:`. See `SandboxPool.checkout`."""
        return _AsyncCheckout(self, session_options)

    async def _checkout(self, overrides: dict[str, Any]) -> AsyncIsolatedRuntime:
        session = _session_for(self._session, overrides, _ASYNC_SESSION_OPTIONS)
        options = _parent_side(session) if overrides else None
        if self._closed:
            raise RuntimeError("this AsyncSandboxPool is closed")
        if not self._started:
            raise RuntimeError(
                "start the pool first: `async with AsyncSandboxPool(...)` or `await pool.start()`"
            )
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "an AsyncSandboxPool can only be used on the event loop it was started on"
            )
        rt = None
        dead = []
        # No await between taking from the deque and handing out: on one loop, nothing can take
        # the same runtime twice.
        while self._ready:
            candidate = self._ready.popleft()
            if (
                not candidate.is_closed()
                and not candidate._killed  # noqa: SLF001
                and candidate._proc.poll() is None  # noqa: SLF001
            ):
                rt = candidate
                break
            dead.append(candidate)
        self._checkouts += 1
        if rt is None:
            self._cold_starts += 1
        self._wake_fillers()
        for candidate in dead:
            await candidate.close()
        if rt is None:
            return await self._new(session)
        if options is not None:
            rt._apply_session(options)  # noqa: SLF001
        return rt

    def _wake_fillers(self) -> None:
        cond = self._cond
        if cond is None:
            return

        async def wake() -> None:
            async with cond:
                cond.notify_all()

        assert self._loop is not None
        task = self._loop.create_task(wake())
        self._tasks.append(task)
        task.add_done_callback(self._forget_task)

    def _forget_task(self, task: asyncio.Task[None]) -> None:
        try:
            self._tasks.remove(task)
        except ValueError:
            pass

    async def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait until `size` runtimes are ready (True), or `timeout` seconds pass (False)."""
        if self._cond is None:
            return False
        cond = self._cond
        try:
            async with asyncio.timeout(timeout):
                async with cond:
                    await cond.wait_for(
                        lambda: self._closed or len(self._ready) >= self._size
                    )
        except TimeoutError:
            return False
        return not self._closed

    def stats(self) -> dict[str, Any]:
        """As `SandboxPool.stats()`."""
        return {
            "size": self._size,
            "ready": len(self._ready),
            "starting": self._starting,
            "checkouts": self._checkouts,
            "cold_starts": self._cold_starts,
            "last_error": self._last_error,
        }

    async def close(self) -> None:
        """Kill every runtime still waiting in the pool and stop refilling. Idempotent."""
        self._closed = True
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        items = list(self._ready)
        self._ready.clear()
        if items:
            await asyncio.gather(*(rt.close() for rt in items), return_exceptions=True)
