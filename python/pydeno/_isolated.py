"""`IsolatedRuntime`: guest JavaScript in a worker process the parent can kill.

`Runtime` runs V8 inside the host process, so a guest that makes V8 abort
(`new Array(2**32-1).fill(0)`) or sits in one uninterruptible native builtin
(`sparse.sort()`) takes the host with it, and `timeout=` cannot help.
`IsolatedRuntime` keeps the same guest behind a process boundary, the way
pydantic/monty's pool does: the worker dies, the parent observes it and raises.

Trust model, copied from Monty: every frame from the worker is untrusted input
(`_wire.loads` / `decode_value` bound its size, depth and vocabulary), the worker
starts with an empty environment, and a worker that misbehaves is killed rather
than argued with. Host functions never leave this process; the worker only holds
stubs that ask for them by id.

This is a process boundary, not an OS sandbox. A guest that escapes V8 would land
in a disposable, secret-free process; confining what that process may do (seccomp,
`sandbox_init`, containers) stays the host's job, as it does for Monty.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import contextvars
import inspect
import itertools
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import weakref
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from . import _sandbox, _wire
from ._result import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    ExecutionResult,
    OutputCapture,
    capture_result,
    check_limit,
)
from ._pydeno import (
    JavaScriptError,
    RuntimeConfig,
    RuntimeForceKilled,
    RuntimeTerminated,
    RuntimeTimeout,
)

__all__ = ["IsolatedRuntime", "WorkerCrashed"]

_ERROR_CLASSES: dict[str, type[Exception]] = {
    cls.__name__: cls
    for cls in (
        JavaScriptError,
        RuntimeTimeout,
        RuntimeTerminated,
        RuntimeForceKilled,
        RuntimeError,
        ValueError,
        TypeError,
    )
}

_CONFIG_KEYS = (
    "max_heap_size",
    "initial_heap_size",
    "max_buffer_bytes",
    "timeout",
    "bootstrap",
    "enable_console",
    "max_serialization_depth",
    "max_serialization_bytes",
    "force_kill_grace",
)
# `snapshot` is refused, not ignored: RuntimeConfig forbids a snapshot together with a bootstrap,
# so dropping it would also drop the bootstrap the caller meant to be in force.
_UNSUPPORTED_CONFIG = ("inspector", "snapshot")

_MAX_REMOTE_MESSAGE = 64 * 1024
_POLL_SECONDS = 0.1
_RSS_EVERY_SECONDS = 0.05
_STDERR_TAIL_BYTES = 2048
_IDLE_CHECK_SECONDS = 0.25
# A worker with nothing to do has no business burning CPU. A little is normal (V8 finishes
# collecting garbage after a command), so this is generous; a compromised idle worker mining
# or spinning is not.
_IDLE_CPU_LIMIT_SECONDS = 2.0
# Worker CPU may exceed wall-clock time (V8 collects garbage on other threads), so the CPU cap
# is a multiple of the hard deadline rather than equal to it.
_CPU_CAP_FACTOR = 2.0
# A command's CPU baseline is the latest reading of the worker's CPU time (the previous command's
# final sample, or the idle watchdog's), not a fresh one: CPU time only grows, so an older reading
# can only charge the command MORE (the idle CPU since), never less. Older than this, it is read
# afresh, so that charge stays bounded (a quarter-second watchdog tick, plus slack for a late one).
_CPU_BASELINE_MAX_AGE = 2 * _IDLE_CHECK_SECONDS

# Defaults for code you do not trust. Pass `None` to remove one; a sandbox that
# silently has no limits until you remember to set them is not much of a sandbox.
DEFAULT_MAX_MEMORY = 1024 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_MAX_HOST_WAIT = 600.0
DEFAULT_MAX_INFLIGHT_HOST_CALLS = 64
DEFAULT_WRITE_STALL_TIMEOUT = 10.0

# Set while a host function runs on behalf of the guest, so a function that tries to call back
# into the same runtime fails clearly instead of deadlocking on it. (A thread-local covers the
# synchronous path; a context variable also covers asynchronous handlers, which run on the
# caller's event loop and see the context their coroutine was started in.)
_IN_HOST_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pydeno_in_host_call", default=False
)

_NATIVE_FRAME = re.compile(r"0x[0-9a-fA-F]{4,}|\.(?:so|dylib)\b|\+\s*\d+\s*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(text: str, limit: int = 500) -> str:
    """Text that came from the worker, made safe to put in an exception message and a log:
    no control or escape characters, bounded length."""
    return _CONTROL.sub("?", text)[:limit]


_REVOKED_MEMORY = 4096
# V8 flags every worker gets besides `--jitless` (each checked against dagre, three.js with the glTF
# exporter and vega-lite under the sandbox, and against the benchmark suite):
#  * regexp fallback: a catastrophic regular expression switches to a linear-time engine after
#    50 000 backtracks and returns, instead of running until the deadline kills the worker.
#  * freeze flags: V8 refuses further flag changes once it has started, so a V8 bug that could flip
#    a flag at run time cannot be used to switch a protection off.
_HARDENING_V8_FLAGS = (
    "--enable-experimental-regexp-engine-on-excessive-backtracks",
    "--freeze-flags-after-init",
)
# `strict_eval=True`: `eval`, `new Function` (and the async/generator function constructors) throw
# an EvalError in the guest. V8 sets this once per context at start-up and the freeze above keeps
# it set. It does not cover WebAssembly (`jitless=False` compiles Wasm bytes regardless), nor
# `import()`, which the module loader decides.
_STRICT_EVAL_FLAG = "--disallow-code-generation-from-strings"
_STRICT_EVAL_NAME = _STRICT_EVAL_FLAG[2:]


def _strict_eval_setting(flags: Sequence[str]) -> bool | None:
    """What `flags` leave V8's code-generation flag at (the last mention wins, as in V8), or None
    when they do not mention it. Accepts V8's spellings: `--x`, `--no-x`, `--nox`, `--x=false`,
    with `_` or `-`."""
    value: bool | None = None
    for flag in flags:
        if not isinstance(flag, str):
            continue  # refused by the worker's own check
        name, sep, arg = flag.lstrip("-").replace("_", "-").partition("=")
        negated = False
        if name.startswith("no-"):
            name, negated = name[3:], True
        elif name.startswith("no") and name[2:] == _STRICT_EVAL_NAME:
            name, negated = name[2:], True
        if name != _STRICT_EVAL_NAME:
            continue
        on = arg.lower() not in ("false", "0") if sep else True
        value = on != negated
    return value


def _strict_eval_requested(options: Mapping[str, Any]) -> bool:
    """Whether runtime keyword arguments (`strict_eval=`, `v8_flags=`) make a strict runtime:
    `IsolatedRuntime(**options).strict_eval`, without starting one."""
    if options.get("strict_eval", False) is True:
        return True
    return bool(_strict_eval_setting(list(options.get("v8_flags", ()))))


def _worker_v8_flags(
    *,
    jitless: bool,
    random_seed: int | None,
    v8_flags: Sequence[str],
    strict_eval: bool,
) -> list[str]:
    """The V8 flags a worker starts with: `--jitless`, the hardening flags, the seed, the
    caller's own, and then the strict-eval flag (last, so nothing before it can undo it)."""
    if not isinstance(strict_eval, bool):
        raise TypeError("strict_eval must be a bool")
    v8_flags = list(v8_flags)
    if strict_eval and _strict_eval_setting(v8_flags) is False:
        raise ValueError(
            f"strict_eval=True contradicts v8_flags, which switch {_STRICT_EVAL_FLAG} off"
        )
    return (
        (["--jitless"] if jitless else [])
        + list(_HARDENING_V8_FLAGS)
        + ([] if random_seed is None else [f"--random-seed={random_seed}"])
        + v8_flags
        + ([_STRICT_EVAL_FLAG] if strict_eval else [])
    )


# A worker runs about 13 threads (17 on macOS); this is far past that and far below a thread bomb.
_MAX_WORKER_THREADS = 64


def _revoked_handler(*_args: Any) -> Any:
    raise PermissionError("capability revoked")


_CONSOLE_LEVELS = frozenset({"log", "info", "warn", "error", "debug", "trace"})
_MAX_SPECIFIER = 4096


def _checked_specifiers(fn: Callable[..., Any], arity: int) -> Callable[..., Any]:
    """Wrap a module resolver/loader so the worker can only call it with `arity` plain, bounded,
    NUL-free strings. The parent made these handlers and knows their contract; a compromised
    worker choosing other arguments (a path-traversal string is the obvious one, a non-string the
    subtle one) should never reach the host's own code."""

    def check(args: tuple[Any, ...]) -> None:
        if len(args) != arity or not all(
            isinstance(a, str) and len(a) <= _MAX_SPECIFIER and "\0" not in a
            for a in args
        ):
            raise ValueError("invalid module specifier")

    if inspect.iscoroutinefunction(fn):

        async def acall(*args: Any) -> Any:
            check(args)
            return await fn(*args)

        return acall

    def call(*args: Any) -> Any:
        check(args)
        return fn(*args)

    return call


def _console_router(
    ref: weakref.ref[IsolatedRuntime], user: Callable[..., Any] | None
) -> Callable[..., Any]:
    """The parent's console handler: the capture of the command in flight (`execute`), then the
    caller's `on_console`. Holds the runtime weakly, so it does not keep it alive."""

    def route(level: str, args: list[Any]) -> None:
        rt = ref()
        capture = rt._capture if rt is not None else None  # noqa: SLF001
        if capture is not None:
            capture(level, args)
        if user is not None:
            user(level, args)

    return route


def _checked_console(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Only the six console methods, with a list of arguments: `getattr(logger, level)` in a
    typical `on_console` must not be steerable to `__init__` by the worker."""

    def call(*args: Any) -> Any:
        if (
            len(args) != 2
            or args[0] not in _CONSOLE_LEVELS
            or not isinstance(args[1], list)
        ):
            raise ValueError("invalid console call")
        return fn(*args)

    return call


class _Default:
    def __repr__(self) -> str:
        return "<default>"


_DEFAULT: Any = _Default()

_LIVE: weakref.WeakSet[IsolatedRuntime] = weakref.WeakSet()


class WorkerCrashed(RuntimeError):
    """The worker process died, was killed, or broke protocol. The runtime is closed."""


class _HostCallBudgetExceeded(Exception):
    """Internal: the guest asked for more host calls than `max_host_calls` allows."""


def _clock_ms(clock: datetime | float | int | None) -> int | None:
    """A frozen instant as whole epoch milliseconds, or None for the real clock."""
    if clock is None:
        return None
    if isinstance(clock, datetime):
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        return int(clock.timestamp() * 1000)
    if isinstance(clock, bool) or not isinstance(clock, (int, float)):
        raise ValueError("clock must be a datetime, epoch seconds, or None")
    if (
        not math.isfinite(clock) or abs(clock) > 8.64e12
    ):  # the range of a JS Date, in seconds
        raise ValueError("clock is outside the range a JavaScript Date can hold")
    return int(clock * 1000)


def _seconds(value: float | int | timedelta | None) -> float | None:
    if value is None:
        return None
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


# The options that only the parent enforces: none of them reaches the worker, so a worker started
# ahead of time (`SandboxPool`) can be given them when it is handed out.
SESSION_OPTIONS = (
    "request_timeout",
    "timeout_grace",
    "max_host_calls",
    "max_host_wait",
    "max_inflight_host_calls",
    "write_stall_timeout",
    "redact_host_errors",
)


def _session_options(
    *,
    request_timeout: float | int | timedelta | None | Any = _DEFAULT,
    timeout_grace: float | int = 2.0,
    max_host_calls: int | None = None,
    max_host_wait: float | int | timedelta | None | Any = _DEFAULT,
    max_inflight_host_calls: int | None | Any = _DEFAULT,
    write_stall_timeout: float | int | timedelta | None | Any = _DEFAULT,
    redact_host_errors: bool = True,
) -> dict[str, Any]:
    """The parent-side options, validated and normalised, as the runtime attributes that hold
    them. One function for `IsolatedRuntime`, `AsyncIsolatedRuntime` and the pools' checkout, so
    an option set at checkout means exactly what it means in the constructor."""
    if max_host_calls is not None and max_host_calls < 0:
        raise ValueError("max_host_calls must be non-negative")
    max_inflight = (
        DEFAULT_MAX_INFLIGHT_HOST_CALLS
        if max_inflight_host_calls is _DEFAULT
        else max_inflight_host_calls
    )
    if max_inflight is not None and max_inflight < 1:
        raise ValueError("max_inflight_host_calls must be at least 1")
    return {
        "_request_timeout": (
            _DEFAULT if request_timeout is _DEFAULT else _seconds(request_timeout)
        ),
        "_grace": float(timeout_grace),
        "_max_host_calls": max_host_calls,
        "_max_host_wait": (
            DEFAULT_MAX_HOST_WAIT
            if max_host_wait is _DEFAULT
            else _seconds(max_host_wait)
        ),
        "_max_inflight": max_inflight,
        "_stall": (
            DEFAULT_WRITE_STALL_TIMEOUT
            if write_stall_timeout is _DEFAULT
            else _seconds(write_stall_timeout)
        ),
        "_redact": bool(redact_host_errors),
    }


class _Pump:
    """State of the one command in flight: its limits and its host callbacks.

    Three limits, because each one alone has a way round:

    - the **hard deadline**, in wall-clock time that does not run while a host callback does
      (a slow tool must not eat the guest's budget);
    - a cap on the **total time spent waiting on host callbacks**, because "paused while a
      callback is outstanding" is exactly what a guest can arrange to be true forever, by
      always keeping one asynchronous call in flight;
    - a cap on the **CPU the worker burns**, which is the one thing a guest cannot hide:
      computing costs CPU whether or not a callback is outstanding.
    """

    __slots__ = (
        "loop",
        "hard",
        "deadline",
        "max_host_wait",
        "cpu_cap",
        "cpu_start",
        "_outstanding",
        "_paused_at",
        "_paused_total",
        "_lock",
    )

    def __init__(
        self,
        hard_timeout: float | None,
        loop: asyncio.AbstractEventLoop | None,
        *,
        max_host_wait: float | None = None,
        cpu_cap: float | None = None,
        cpu_start: float | None = None,
    ) -> None:
        self.loop = loop
        self.hard = hard_timeout
        self.deadline = (
            None if hard_timeout is None else time.monotonic() + hard_timeout
        )
        self.max_host_wait = max_host_wait
        self.cpu_cap = cpu_cap
        self.cpu_start = cpu_start
        self._outstanding = 0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self._lock = threading.Lock()

    @property
    def outstanding(self) -> int:
        return self._outstanding

    def begin_call(self) -> None:
        """The deadline stops while the host runs a callback, as in Monty."""
        with self._lock:
            if self._outstanding == 0:
                self._paused_at = time.monotonic()
            self._outstanding += 1

    def end_call(self) -> None:
        with self._lock:
            self._outstanding -= 1
            if self._outstanding == 0:
                paused = time.monotonic() - self._paused_at
                self._paused_total += paused
                if self.deadline is not None:
                    self.deadline += paused

    def expired(self) -> bool:
        with self._lock:
            if self.deadline is None or self._outstanding:
                return False
            return time.monotonic() > self.deadline

    def waited_too_long(self) -> bool:
        """Has the guest spent more than `max_host_wait` waiting on host callbacks?"""
        if self.max_host_wait is None:
            return False
        with self._lock:
            waited = self._paused_total
            if self._outstanding:
                waited += time.monotonic() - self._paused_at
            return waited > self.max_host_wait


class IsolatedRuntime:
    """A `Runtime` whose V8 isolate lives in a supervised worker process.

    Args:
        config: Limits and bootstrap for the guest. `inspector` and `snapshot` are not
            supported across the boundary yet. `on_console` is: each `console.*` call is a
            (synchronous) host call, counted by `max_host_calls`.
        max_memory: Kill the worker if its resident memory exceeds this many bytes
            (default 1 GiB; `None` removes the limit). Enforced twice: by the worker itself every ~20ms (it exits with a dedicated
            code) and by the parent every ~50ms (Linux and macOS).
        request_timeout: Hard wall-clock limit per command, enforced by killing the
            worker. Time spent running host callbacks is not charged. By default it is
            the command's own `timeout` plus `timeout_grace`, or 60s when the command has
            no timeout; pass `None` to remove the hard deadline.
        timeout_grace: Seconds the worker gets past a soft `timeout` before it is killed.
        max_host_calls: Total host-function calls the guest may make over this runtime's
            life, then the worker is killed. The guest's clock is paused while a host
            callback runs, so an endless stream of quick calls needs its own cap.
            (`None`: unlimited.)
        max_host_wait: Most time (seconds, default 600) one command may spend waiting on host
            callbacks in total. The hard deadline does not run while a callback does, which a
            guest could exploit by always keeping one asynchronous call in flight; this bounds it.
            The worker's CPU use is also capped at twice the hard deadline per command, which
            callbacks cannot pause. `None` removes the wait cap.
        max_inflight_host_calls: Most host calls that may be outstanding at once (default 64);
            further ones are answered with an error instead of being run.
        write_stall_timeout: If the worker stops reading its input and the pipe stays full this
            long (seconds, default 10), the worker is killed rather than letting the host block
            forever. `None` waits indefinitely.
        redact_host_errors: Replace the message of an exception raised by a host function with a
            generic one before the guest sees it (the exception's class name is kept). Use this
            when your tools' error text can contain paths, queries or secrets.
        sandbox: OS confinement for the worker (macOS Seatbelt; Linux Landlock + seccomp).
            "auto" applies whatever the platform offers. "require" refuses to start unless
            *every* layer the platform has is in force (macOS: Seatbelt; Linux: Landlock and
            seccomp), so a kernel that lacks one cannot silently weaken you. "off" disables it.
            Read `.sandbox` for what is active.
        empty_root: On Linux, also give the worker a private mount namespace whose root is
            empty, plus empty network and IPC namespaces, so it cannot even tell which host paths
            exist. Needs unprivileged user namespaces; silently skipped where they are not
            allowed (see `.sandbox_extras`).
        jitless: Run V8 in the worker with `--jitless`: no JIT compiler and no
            WebAssembly, which removes the largest class of V8 exploits at a modest
            speed cost. Pass `False` to allow WebAssembly and JIT speed.
        v8_flags: Extra V8 flags for the worker, applied before the isolate exists.
        strict_eval: Forbid code generation from strings in the guest: ``eval(...)``,
            ``new Function(...)`` and the async, generator and async-generator function
            constructors throw ``EvalError``, however the guest reaches them. The host's own
            `eval` / `execute` of a script is unaffected. Set with V8's
            ``--disallow-code-generation-from-strings`` and frozen with the other flags, so the
            guest cannot switch it off. It removes no engine code and does not cover WebAssembly
            (with ``jitless=False``); see the isolation guide. Read `.strict_eval`.
        clock: Freeze the guest's clock at this instant (a `datetime`, naive meaning UTC, or
            epoch seconds). `Date.now()`, `new Date()` and `Intl.DateTimeFormat#format()`
            then never advance, which removes the wall clock as a timing source (a busy loop
            can still count) and makes runs reproducible. `None`: the real clock.
        random_seed: Seed `Math.random` (V8's `--random-seed`) for reproducible runs.
        capture_console: Route the guest's `console.*` to the parent even without an
            `on_console`, so `execute()` can return it as `stdout`/`stderr`. Off by default:
            every `console.*` call is then a host call (counted by `max_host_calls`). With an
            `on_console`, console output is captured either way.
        python: Interpreter for the worker (default: this one).

    A worker crash, a hard timeout or a memory kill closes the runtime and raises
    (`WorkerCrashed` or `RuntimeTimeout`); create a new `IsolatedRuntime` to continue.
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        max_memory: int | None = _DEFAULT,
        request_timeout: float | int | None = _DEFAULT,
        timeout_grace: float | int = 2.0,
        max_host_calls: int | None = None,
        max_host_wait: float | int | None = _DEFAULT,
        max_inflight_host_calls: int | None = _DEFAULT,
        write_stall_timeout: float | int | None = _DEFAULT,
        redact_host_errors: bool = True,
        sandbox: str = "auto",
        empty_root: bool = True,
        jitless: bool = True,
        v8_flags: Sequence[str] = (),
        strict_eval: bool = False,
        clock: datetime | float | int | None = None,
        random_seed: int | None = None,
        capture_console: bool = False,
        python: str | None = None,
        prewarm: bool = True,
    ) -> None:
        clock_ms = _clock_ms(clock)
        if random_seed is not None and (
            isinstance(random_seed, bool)
            or not isinstance(random_seed, int)
            or not 0 <= random_seed < 2**31
        ):
            raise ValueError("random_seed must be an integer in [0, 2**31)")
        worker_flags = _worker_v8_flags(
            jitless=jitless,
            random_seed=random_seed,
            v8_flags=v8_flags,
            strict_eval=strict_eval,
        )
        if max_memory is _DEFAULT:
            max_memory = DEFAULT_MAX_MEMORY
        if max_host_calls is not None and max_host_calls < 0:
            raise ValueError("max_host_calls must be non-negative")
        if sandbox not in ("auto", "require", "off"):
            raise ValueError("sandbox must be 'auto', 'require' or 'off'")
        if max_memory is not None and max_memory <= 0:
            raise ValueError("max_memory must be a positive integer")
        if os.name != "posix":
            raise NotImplementedError("IsolatedRuntime currently supports POSIX only")
        config = config or RuntimeConfig()
        for attr in _UNSUPPORTED_CONFIG:
            if getattr(config, attr, None) is not None:
                raise ValueError(
                    f"RuntimeConfig.{attr} is not supported by IsolatedRuntime yet"
                )

        self._config = {k: getattr(config, k) for k in _CONFIG_KEYS}
        if max_memory is not None and self._config["max_buffer_bytes"] is None:
            # ArrayBuffer storage is outside the V8 heap, so `max_heap_size` cannot bound it, and
            # without a cap a `new Uint8Array(2 ** 31)` is only caught by the RSS poll, which kills
            # the whole worker. With a cap the guest gets a catchable RangeError and the session
            # survives. (No default heap cap: with one, V8 turns an over-cap allocation into a
            # fatal "heap limit exceeded" instead of that RangeError.)
            self._config["max_buffer_bytes"] = max(1, max_memory // 4)
        self._soft_timeout = _seconds(config.timeout)
        self._max_memory = max_memory
        self._host_calls = 0
        # `_request_timeout` has three states: unset (soft timeout + grace, else a default
        # ceiling), a number, or an explicit None meaning "no hard deadline".
        self._request_timeout: float | None | Any
        for attr, value in _session_options(
            request_timeout=request_timeout,
            timeout_grace=timeout_grace,
            max_host_calls=max_host_calls,
            max_host_wait=max_host_wait,
            max_inflight_host_calls=max_inflight_host_calls,
            write_stall_timeout=write_stall_timeout,
            redact_host_errors=redact_host_errors,
        ).items():
            setattr(self, attr, value)
        self._python = python or sys.executable
        self._options: dict[str, Any] = {
            "sandbox": sandbox,
            "empty_root": empty_root,
            "v8_flags": worker_flags,
            "max_memory": max_memory,
        }
        if clock_ms is not None:
            self._options["clock_ms"] = clock_ms
        #: What the worker reports after start-up: the OS layers in force
        #: ("seatbelt", "landlock+seccomp", ... or "none") and the V8 flags set.
        self.sandbox = "none"
        #: Bonus layers that also took effect, e.g. ["emptyroot"] (a private mount namespace
        #: with nothing in it; needs unprivileged user namespaces, so it is not everywhere).
        self.sandbox_extras: list[str] = []
        self.v8_flags: list[str] = []

        self._handlers: dict[int, tuple[Callable[..., Any], bool]] = {}
        self._token_to_hid: dict[int, int] = {}
        self._hids = itertools.count(1)
        self._cmd_ids = itertools.count(1)
        self._lock = threading.Lock()
        self._guard = threading.local()
        self._closed = False
        self._last_rss_check = 0.0
        # Why the idle watchdog killed the worker, for the pump to report: the watchdog thread
        # cannot raise into the caller, so without this the caller sees only "killed by SIGKILL".
        self._kill_reason: str | None = None
        # Where `execute()` collects the console output of the command in flight.
        self._capture: OutputCapture | None = None
        if config.on_console is not None or capture_console:
            # `console.*` in the guest calls this in the parent, like any host function.
            console_hid = next(self._hids)
            self._handlers[console_hid] = (
                _checked_console(_console_router(weakref.ref(self), config.on_console)),
                False,
            )
            self._options["console_hid"] = console_hid

        self._idle_cpu_base: float | None = None
        self._idle_since = time.monotonic()
        # The latest reading of the worker's CPU time and when it was taken: the next command's
        # baseline (see `_CPU_BASELINE_MAX_AGE`). Written only by whoever holds `_lock`.
        self._last_cpu: float | None = None
        self._last_cpu_at = 0.0
        # The CPU reading `_pump` took as the command's answer arrived (see `_request`).
        self._end_cpu: float | None = None

        # A worker started ahead of time (Python up, everything imported, waiting for `init`)
        # saves most of the ~55 ms start-up. The default interpreter only: a custom `python=`
        # (the tests' fake workers) is always spawned fresh.
        self._proc, self._stderr = (
            _take_worker()
            if prewarm and python is None
            else _start_worker(self._python)
        )
        stdin_fd = self._proc.stdin.fileno()  # type: ignore[union-attr]
        # Non-blocking, so that a worker which stops reading its input is a timeout we can act on
        # and not a thread stuck in write() forever.
        os.set_blocking(stdin_fd, False)
        self._writer = _wire.FrameWriter(stdin_fd, stall_timeout=self._stall)
        self._reader = _wire.FrameReader(self._proc.stdout.fileno())  # type: ignore[union-attr]
        # A runtime dropped without close() must not leave a worker behind.
        self._finalizer = weakref.finalize(
            self, _terminate_process, self._proc, self._stderr
        )
        self._owner_pid = os.getpid()
        # Asynchronous host calls still running, across commands: `max_inflight_host_calls` is a
        # cap on these, not on one command's, or 1000 commands could each leave 64 behind.
        self._async_inflight = 0
        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._async_inflight_lock = threading.Lock()
        # Recently revoked handler ids, oldest first (bounded): see `_on_call`.
        self._revoked_hids: dict[int, None] = {}
        _LIVE.add(self)
        self._handshake()
        self._check_limits_can_be_enforced()
        if prewarm and python is None:
            _refill_spare()
        self._idle_since = time.monotonic()
        self._idle_cpu_base = self._last_cpu = _sandbox.cpu_seconds(self._proc.pid)
        self._last_cpu_at = time.monotonic()
        threading.Thread(
            target=_idle_watch,
            args=(weakref.ref(self),),
            name="pydeno-idle-watch",
            daemon=True,
        ).start()

    @property
    def strict_eval(self) -> bool:
        """Whether the guest is refused code generation from strings (`strict_eval=True`, or the
        same V8 flag passed in `v8_flags`), read from the flags the worker is started with.
        Sessions record it in their journal."""
        return bool(_strict_eval_setting(self._options["v8_flags"]))

    # -- lifecycle ---------------------------------------------------------

    def _apply_session(self, options: dict[str, Any]) -> None:
        """Install `_session_options(...)` on a runtime nobody has used yet (a pool checkout)."""
        for attr, value in options.items():
            setattr(self, attr, value)
        self._writer._stall = self._stall  # noqa: SLF001

    def _check_limits_can_be_enforced(self) -> None:
        """A limit that cannot be measured is a limit that is not there.

        Memory and CPU are read from outside (`/proc` on Linux, `proc_pidinfo` on macOS). On a
        system where that read fails (a hardened /proc mount, a missing libproc) the checks would
        quietly never fire while `sandbox` still reports success. Say so, and under
        `sandbox="require"`, which promises a complete sandbox, refuse to start."""
        missing = []
        if self._max_memory is not None and _sandbox.rss_bytes(self._proc.pid) is None:
            missing.append("max_memory")
        if _sandbox.cpu_seconds(self._proc.pid) is None:
            missing.append("the CPU cap")
        if _sandbox.thread_count(self._proc.pid) is None:
            missing.append("the thread cap")
        if not missing:
            return
        what = " and ".join(missing)
        if self._options["sandbox"] == "require":
            self._kill()
            raise WorkerCrashed(
                f"{what} cannot be enforced on this system (the worker's resource usage "
                "cannot be read) and sandbox='require' demands every protection"
            )
        warnings.warn(
            f"{what} cannot be enforced on this system: the worker's resource usage cannot be "
            "read, so those limits will never fire.",
            RuntimeWarning,
            stacklevel=3,
        )

    def _handshake(self) -> None:
        try:
            self._writer.send(
                {
                    "t": "init",
                    "config": _wire.Enc(self._config),
                    "options": self._options,
                }
            )
            payload = self._reader.read(time.monotonic() + 30.0)
            if payload is None:
                raise WorkerCrashed(
                    self._describe_death("worker exited during startup")
                )
            message = _wire.loads(payload)
            if message["t"] == "error":
                text = message.get("msg")
                raise WorkerCrashed(
                    "worker failed to start: "
                    + (_clean(text) if isinstance(text, str) else "unknown error")
                )
            if message["t"] != "ready":
                raise _wire.WireError("expected ready")
            applied = message.get("sandbox")
            if not isinstance(applied, str) or len(applied) > 64:
                raise _wire.WireError("bad ready frame")
            extras = message.get("extras", [])
            if (
                not isinstance(extras, list)
                or len(extras) > 8
                or not all(isinstance(x, str) and len(x) <= 32 for x in extras)
            ):
                raise _wire.WireError("bad ready frame")
            self.sandbox = applied
            self.sandbox_extras = list(extras)
            missing = (
                _sandbox.missing_layers(applied) if applied != "off" else frozenset()
            )
            if missing and self._options["sandbox"] == "auto":
                warnings.warn(
                    f"IsolatedRuntime is running with a degraded OS sandbox ({applied!r}; "
                    f"missing {sorted(missing)}). Untrusted code has less containment than "
                    "intended; pass sandbox='require' to refuse instead.",
                    RuntimeWarning,
                    stacklevel=3,
                )
            self.v8_flags = list(self._options["v8_flags"])
        except TimeoutError:
            self._kill()
            raise WorkerCrashed("worker did not become ready within 30s") from None
        except (_wire.WireError, OSError) as exc:
            self._kill()
            raise WorkerCrashed(f"worker failed to start: {_clean(str(exc))}") from None
        except WorkerCrashed:
            self._kill()
            raise

    def _describe_death(self, prefix: str) -> str:
        if (
            self._kill_reason is not None
        ):  # the watchdog killed it and kept the reason for us
            return self._kill_reason
        code = self._proc.poll()
        if code is None:
            try:
                code = self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                code = None
        if code == _sandbox.MEMORY_EXIT_CODE:
            return (
                f"{prefix}: worker went over max_memory={self._max_memory} and exited"
            )
        if code is not None and code < 0:
            try:
                prefix += f" (killed by {signal.Signals(-code).name})"
            except ValueError:
                prefix += f" (killed by signal {-code})"
        elif code:
            prefix += f" (exit code {code})"
        try:
            self._stderr.seek(0, os.SEEK_END)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - _STDERR_TAIL_BYTES))
            tail = self._stderr.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            tail = ""
        # The worker wrote this, and a compromised one can write anything: it goes into an
        # exception message, so no control or escape characters.
        last = tail.splitlines()[-1] if tail else ""
        if not last or _NATIVE_FRAME.search(last):
            # A native stack frame names libraries and load addresses (a map of the host's ASLR
            # for anything that forwards `str(exc)` to a user), and says nothing a person can act on.
            return prefix
        return f"{prefix}: {_clean(last)}"

    def _kill(self) -> None:
        if os.getpid() != self._owner_pid:
            self._drop_inherited()
            return
        self._closed = True
        _terminate_process(self._proc, None)
        self._reap()

    def _reap(self) -> None:
        try:
            self._proc.wait(timeout=5)
        except (
            subprocess.TimeoutExpired
        ):  # pragma: no cover - SIGKILL does not time out
            pass
        # Invalidate before closing: a late callback or a thread still polling must fail, not
        # read or write whatever the process opens next under the same descriptor number.
        self._writer.invalidate()
        self._reader.invalidate()
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        _LIVE.discard(self)

    def is_closed(self) -> bool:
        return self._closed

    def _drop_inherited(self) -> None:
        """In a fork()ed child: let go of this process's copies of the worker's pipes and stderr
        file, without signalling, waiting on or talking to a worker that belongs to the parent."""
        self._closed = True
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._stderr.close()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        """Ask the worker to exit; kill it if it does not within a second."""
        if os.getpid() != self._owner_pid:
            self._drop_inherited()
            return
        if self._executor is not None:
            self._executor.shutdown(wait=False)
        if not self._closed:
            self._closed = True
            try:
                self._writer.send({"t": "close"})
                self._proc.wait(timeout=1)
            except (OSError, _wire.WireError, subprocess.TimeoutExpired):
                pass
            self._kill()
        # Idempotent, and done even when a crash already closed the runtime: the file that held
        # the worker's stderr must not wait for the garbage collector.
        try:
            self._stderr.close()
        except OSError:
            pass

    def __enter__(self) -> IsolatedRuntime:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- requests ----------------------------------------------------------

    def _hard_timeout(self, soft: float | None) -> float | None:
        if self._request_timeout is not _DEFAULT:
            return self._request_timeout  # a number, or None: the caller opted out
        return DEFAULT_REQUEST_TIMEOUT if soft is None else soft + self._grace

    def _acquire_slot(self, hard: float | None) -> None:
        """Take the runtime for one command (the caller releases `_lock`), but never wait for it
        forever. A plain method, not a context manager: this is on every command's path.

        Commands run one at a time. A host function that hands work to *another* thread which then
        calls back into this runtime waits on the lock its own command holds: the re-entrancy guard
        cannot see it (it is another thread), and the pump that enforces the deadline is the thread
        stuck in that host function, so nothing would ever time it out. Bounding the wait by the
        request's own deadline turns that deadlock into an error the guest can see."""
        wait = -1 if hard is None else hard + self._grace
        if not self._lock.acquire(timeout=wait):
            raise RuntimeTimeout(
                "timed out waiting for another command on this IsolatedRuntime to finish "
                "(a host function that waits on another thread which calls back into the "
                "same runtime would deadlock)"
            )

    def _request(
        self,
        message: dict[str, Any],
        *,
        soft_timeout: float | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        capture: OutputCapture | None = None,
    ) -> Any:
        self._refuse_reentry()
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "this IsolatedRuntime belongs to the process that created it, not to a fork() of it"
            )
        hard = self._hard_timeout(soft_timeout)
        if not self._lock.acquire(
            False
        ):  # the usual case is a free runtime: no timed wait
            self._acquire_slot(hard)
        try:
            if self._closed:
                raise WorkerCrashed("runtime is closed")
            message["id"] = cmd_id = next(self._cmd_ids)
            cpu_start = self._last_cpu
            if (
                cpu_start is None
                or time.monotonic() - self._last_cpu_at > _CPU_BASELINE_MAX_AGE
            ):
                cpu_start = _sandbox.cpu_seconds(self._proc.pid)
            pump = _Pump(
                hard,
                loop,
                max_host_wait=self._max_host_wait,
                cpu_cap=None if hard is None else hard * _CPU_CAP_FACTOR,
                cpu_start=cpu_start,
            )
            try:
                self._writer.send(message)
            except _wire.WireError as exc:
                raise TypeError(str(exc)) from None
            except _wire.StalledWrite:
                self._kill()
                raise WorkerCrashed(
                    f"worker stopped reading its input for {self._stall:g}s; killed"
                ) from None
            except OSError:
                self._kill()
                raise WorkerCrashed(self._describe_death("worker is gone")) from None
            self._capture = capture
            self._end_cpu = None
            try:
                return self._pump(cmd_id, pump)
            finally:
                self._capture = None
                # Where "idle" starts: what the worker burns from here on, with no command
                # running, is the idle watchdog's business. The reading is the one the pump took
                # as the answer arrived (`_check_memory`), or a fresh one if it never got that far.
                now = self._idle_since = time.monotonic()
                cpu = self._end_cpu
                if cpu is None and not self._closed:
                    cpu = _sandbox.cpu_seconds(self._proc.pid)
                self._idle_cpu_base = cpu
                if cpu is not None:
                    self._last_cpu, self._last_cpu_at = cpu, now
        finally:
            self._lock.release()

    def _pump(self, cmd_id: int, pump: _Pump) -> Any:
        monotonic = time.monotonic
        read = self._reader.read
        last_check = monotonic()
        remote: Exception | None = None
        try:
            while True:
                try:
                    payload = read(monotonic() + _POLL_SECONDS)
                except TimeoutError:
                    self._supervise(pump)
                    last_check = monotonic()
                    continue
                # The limits are enforced on a clock, not on silence. If they ran only when
                # the pipe went quiet, a guest that never lets it go quiet (a loop of cheap
                # host calls) would switch the hard deadline and memory ceiling off.
                now = monotonic()
                if now - last_check >= _POLL_SECONDS:
                    self._supervise(pump)
                    last_check = now
                if payload is None:
                    self._kill()
                    raise WorkerCrashed(self._describe_death("worker process died"))
                message = _wire.loads_decoded(payload)
                kind = message["t"]
                if kind == "call":
                    self._on_call(message, pump)
                elif kind in ("result", "error") and message.get("id") == cmd_id:
                    self._end_cpu = self._check_memory(force=True)
                    if kind == "result":
                        return message.get("v")  # already decoded by `loads_decoded`
                    # Built here, raised after the guard below: a guest's own JavaScriptError is
                    # an answer, not a fault, and must reach the caller unchanged.
                    remote = self._remote_error(message)
                    break
                else:
                    raise _wire.WireError(f"unexpected {_clean(str(kind), 32)!r} frame")
        except (WorkerCrashed, RuntimeTimeout):
            raise  # our own verdicts from the supervisor, already acted on
        except _HostCallBudgetExceeded as exc:
            self._kill()
            raise WorkerCrashed(str(exc)) from None
        except _wire.WireError as exc:
            self._kill()
            raise WorkerCrashed(f"worker broke protocol: {_clean(str(exc))}") from None
        except OSError:
            self._kill()
            raise WorkerCrashed(self._describe_death("lost the worker")) from None
        except Exception as exc:  # noqa: BLE001
            # Whatever else went wrong, it came from reading what the worker sent: a field of
            # the wrong type, an absurd nesting depth. The worker is the one at fault, and a
            # worker that sends nonsense is killed, not left half-synchronised with us.
            self._kill()
            raise WorkerCrashed(
                f"worker sent a malformed frame ({type(exc).__name__})"
            ) from None
        assert remote is not None
        raise remote

    def _idle_check(self) -> None:
        """One pass of the idle watchdog. While a command holds the runtime the pump supervises
        (deadline, CPU, memory), except that the pump can be stuck inside a host handler for as
        long as the handler takes, and a hostile worker can allocate then. So memory and thread
        count are still checked here, without taking the lock."""
        if not self._lock.acquire(blocking=False):
            if not self._closed:
                try:
                    self._check_memory()
                except WorkerCrashed:
                    pass  # killed; the pump sees the pipe close and reports it
            return
        try:
            if self._closed:
                return
            try:
                now_cpu = self._check_memory(force=True)
            except WorkerCrashed:
                return  # `_check_memory` has already killed it
            if now_cpu is not None:
                self._last_cpu, self._last_cpu_at = now_cpu, time.monotonic()
            base = self._idle_cpu_base
            if base is None:
                return
            # A small flat allowance plus a thin trickle (1% of a core) that grows with idle time,
            # so a healthy worker that sits idle for days is never mistaken for a runaway one.
            allowed = _IDLE_CPU_LIMIT_SECONDS + 0.01 * (
                time.monotonic() - self._idle_since
            )
            if now_cpu is not None and now_cpu - base > allowed:
                self._kill()
        finally:
            self._lock.release()

    def _supervise(self, pump: _Pump) -> None:
        """Parent-side backstops: the hard deadline and the memory ceiling."""
        if pump.expired():
            self._kill()
            raise RuntimeTimeout(
                f"worker exceeded its {pump.hard:g}s hard deadline and was killed"
            )
        if pump.waited_too_long():
            self._kill()
            raise RuntimeTimeout(
                f"host callbacks kept the guest waiting for more than "
                f"{pump.max_host_wait:g}s in one command (max_host_wait); worker killed"
            )
        # One reading for the CPU cap, the memory ceiling and the thread cap.
        now_cpu = self._check_memory(force=True)
        if (
            pump.cpu_cap is not None
            and pump.cpu_start is not None
            and now_cpu is not None
            and now_cpu - pump.cpu_start > pump.cpu_cap
        ):
            self._kill()
            raise RuntimeTimeout(
                f"worker used more than {pump.cpu_cap:g}s of CPU in one command "
                f"and was killed"
            )

    def _check_memory(self, *, force: bool = False) -> float | None:
        """Kill the worker if its RSS is over `max_memory` or it has far more threads than a
        worker has. Sampled, so a spike that ends between samples is only caught by the check
        made as each command finishes. Returns the worker's CPU time from the same reading (None
        if not sampled or unreadable)."""
        now = time.monotonic()
        if not force and now - self._last_rss_check < _RSS_EVERY_SECONDS:
            return None
        self._last_rss_check = now
        rss, cpu, threads = _sandbox.usage(self._proc.pid)
        if threads is not None and threads > _MAX_WORKER_THREADS:
            self._kill_reason = f"worker started {threads} threads (limit {_MAX_WORKER_THREADS}); killed"
            self._kill()
            raise WorkerCrashed(self._kill_reason)
        if self._max_memory is not None and rss is not None and rss > self._max_memory:
            self._kill_reason = (
                f"worker used {rss} bytes, over max_memory={self._max_memory}; killed"
            )
            self._kill()
            raise WorkerCrashed(self._kill_reason)
        return cpu

    @staticmethod
    def _remote_error(message: dict[str, Any]) -> Exception:
        kind = message.get("kind")
        # `kind` is the worker's field: only a string can name one of our classes.
        cls = (
            _ERROR_CLASSES.get(kind, RuntimeError)
            if isinstance(kind, str)
            else RuntimeError
        )
        text = message.get("msg")
        if not isinstance(text, str):
            return cls("worker reported an error")
        # The text is the worker's to choose, and a frame can be megabytes: keep an exception
        # message an exception message, not a way to make the parent hold (and log) a huge string.
        if len(text) > _MAX_REMOTE_MESSAGE:
            text = (
                text[:_MAX_REMOTE_MESSAGE]
                + f"... [{len(text) - _MAX_REMOTE_MESSAGE} more characters]"
            )
        # Escape sequences in a message that lands in a terminal or a log are an injection channel.
        # Newlines and tabs stay: a JavaScript stack trace is made of them.
        return cls(_CONTROL.sub("?", text))

    # -- host callbacks ----------------------------------------------------

    def _on_call(self, message: dict[str, Any], pump: _Pump) -> None:
        cid, hid, args = message.get("cid"), message.get("hid"), message.get("args")
        # One lookup, not a membership test followed by a second one: another thread may revoke
        # in between.
        entry = self._handlers.get(hid) if isinstance(hid, int) else None
        if entry is None and hid in self._revoked_hids:
            # A call that was already in flight when the capability was revoked: the guest did
            # nothing wrong, it lost a race with the host. Answer it with an error, do not treat
            # it as a worker forging an id (which ends the session).
            entry = (_revoked_handler, False)
        if (
            not isinstance(cid, int)
            or isinstance(cid, bool)
            or entry is None
            or not isinstance(args, list)
        ):
            # An id the worker was never given is not a capability it holds.
            raise _wire.WireError("call for an unknown host function")
        self._host_calls += 1
        if self._max_host_calls is not None and self._host_calls > self._max_host_calls:
            # Monty's `max_suspensions`: while a host callback runs, the guest's
            # clock is paused, so an unbounded stream of quick calls needs its own cap.
            raise _HostCallBudgetExceeded(
                f"guest made more than max_host_calls={self._max_host_calls} host calls"
            )
        handler, is_async = entry
        # All the arguments under one budget: decoding each on its own would multiply the limit
        # by the argument count.
        decoded = args  # `loads_decoded` already decoded them, under one shared budget
        if self._max_inflight is not None and (
            pump.outstanding >= self._max_inflight
            or self._async_inflight >= self._max_inflight
        ):
            # Refused rather than run: the guest sees an error for this call, and nothing the
            # host owns is touched. Without a cap the number of asynchronous calls (and the
            # tasks and memory behind them) is whatever the guest asks for.
            self._send_reply(
                {
                    "t": "reply",
                    "cid": cid,
                    "err": f"more than {self._max_inflight} host calls in flight",
                    "etype": "RuntimeError",
                },
                None,
            )
            return
        pump.begin_call()
        if is_async and pump.loop is not None:
            coro = _call_guarded(handler, decoded)
            with self._async_inflight_lock:
                self._async_inflight += 1
            try:
                future = asyncio.run_coroutine_threadsafe(coro, pump.loop)
            except Exception as exc:  # noqa: BLE001 - e.g. the caller's loop has closed
                coro.close()
                with self._async_inflight_lock:
                    self._async_inflight -= 1
                self._send_reply(self._error(cid, exc), pump)
                return
            future.add_done_callback(
                lambda fut: self._finish_async_call(cid, pump, fut)
            )
            return
        try:
            self._guard.in_host_call = True
            try:
                value = handler(*decoded)
                if inspect.isawaitable(value):
                    value = asyncio.run(_await(value))
                reply = {"t": "reply", "cid": cid, "v": _wire.Enc(value)}
            finally:
                self._guard.in_host_call = False
        except Exception as exc:  # noqa: BLE001 - the guest sees the failure, the host keeps running
            reply = self._error(cid, exc)
        self._send_reply(reply, pump)

    def _error(self, cid: int, exc: BaseException) -> dict[str, Any]:
        return _error_reply(cid, exc, redact=self._redact)

    def _finish_async_call(self, cid: int, pump: _Pump, future: Any) -> None:
        with self._async_inflight_lock:
            self._async_inflight -= 1
        try:
            reply = {"t": "reply", "cid": cid, "v": _wire.Enc(future.result())}
        except BaseException as exc:  # noqa: BLE001
            reply = self._error(cid, exc)
        self._send_reply(reply, pump)

    def _send_reply(self, reply: dict[str, Any], pump: _Pump | None) -> None:
        try:
            if self._closed:
                return  # the descriptors may already belong to something else
            try:
                self._writer.send(reply)
            except _wire.WireError as exc:
                self._writer.send(self._error(reply["cid"], exc))
        except _wire.StalledWrite:
            # The worker stopped reading what we send. Waiting longer only freezes whoever is
            # sending (possibly the caller's event loop); kill it and let the pump report it.
            self._kill()
        except OSError:
            pass  # worker already gone; the pump will report it
        finally:
            if pump is not None:
                pump.end_call()

    # -- public API --------------------------------------------------------

    def eval(self, code: str) -> Any:
        """Evaluate JavaScript synchronously in the worker."""
        return self._request(
            {"t": "eval", "code": code}, soft_timeout=self._soft_timeout
        )

    async def eval_async(
        self, code: str, *, timeout: float | int | timedelta | None = None
    ) -> Any:
        """Evaluate JavaScript, awaiting promises. Async host functions run on this loop."""
        soft = _seconds(timeout)
        if soft is None:
            soft = self._soft_timeout
        loop = asyncio.get_running_loop()
        message = {"t": "eval_async", "code": code, "timeout": soft}
        try:
            return await self._in_own_thread(message, soft, loop)
        except asyncio.CancelledError:
            # The thread cannot be interrupted, and the worker is mid-command.
            self._kill()
            raise

    def execute(
        self,
        code: str,
        *,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    ) -> ExecutionResult:
        """`eval` the code and return an `ExecutionResult` instead of raising.

        `stdout`/`stderr` hold the command's console output when the runtime was created with
        `capture_console=True` or an `on_console` (empty otherwise), each capped at
        `max_output_bytes`; a result over `max_result_bytes` of JSON is a ``Failed`` result with
        ``error_type="ResultTooLarge"``. Every failure of the run (a JavaScript error, a timeout,
        a crashed worker) is reported in the result, not raised."""
        capture = OutputCapture(max_output_bytes)
        check_limit("max_result_bytes", max_result_bytes)
        try:
            value = self._request(
                {"t": "eval", "code": code},
                soft_timeout=self._soft_timeout,
                capture=capture,
            )
        except Exception as exc:  # noqa: BLE001 - the run's failure is the result
            return capture_result(capture, error=exc)
        return capture_result(capture, value=value, max_result_bytes=max_result_bytes)

    async def execute_async(
        self,
        code: str,
        *,
        timeout: float | int | timedelta | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    ) -> ExecutionResult:
        """`eval_async` (promises are awaited) returning an `ExecutionResult`; see `execute`."""
        capture = OutputCapture(max_output_bytes)
        check_limit("max_result_bytes", max_result_bytes)
        soft = _seconds(timeout)
        if soft is None:
            soft = self._soft_timeout
        loop = asyncio.get_running_loop()
        message = {"t": "eval_async", "code": code, "timeout": soft}
        try:
            value = await self._in_own_thread(message, soft, loop, capture)
        except asyncio.CancelledError:
            self._kill()
            raise
        except Exception as exc:  # noqa: BLE001 - the run's failure is the result
            return capture_result(capture, error=exc)
        return capture_result(capture, value=value, max_result_bytes=max_result_bytes)

    def _refuse_reentry(self) -> None:
        """A host function must not call back into the runtime that is waiting for it."""
        if getattr(self._guard, "in_host_call", False) or _IN_HOST_CALL.get():
            raise RuntimeError(
                "an IsolatedRuntime cannot be re-entered from its own host functions"
            )

    def _own_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="pydeno-command"
                )
            return self._executor

    async def _in_own_thread(
        self,
        message: dict[str, Any],
        soft: float | None,
        loop: asyncio.AbstractEventLoop,
        capture: OutputCapture | None = None,
    ) -> Any:
        """Run a command off the caller's event loop on a thread this runtime owns.

        `asyncio.to_thread` would use the loop's *default* executor, shared with everything else
        in the application. A command can sit in a host callback for minutes, so a handful of
        runtimes parked on slow tools would starve every other `to_thread` or
        `run_in_executor(None)` in the process. One thread per runtime cannot."""
        # Before queueing, not only inside `_request`: the runtime's thread is busy with the very
        # command this call came from, so a queued re-entrant call would wait behind it forever.
        self._refuse_reentry()
        # Run in a copy of the caller's context, as `asyncio.to_thread` does: host functions then see
        # the caller's contextvars (tracing spans, request ids, ...).
        context = contextvars.copy_context()
        call = functools.partial(
            context.run,
            self._request,
            message,
            soft_timeout=soft,
            loop=loop,
            capture=capture,
        )
        return await loop.run_in_executor(self._own_executor(), call)

    def _register_token(self, token: int, hid: int) -> None:
        """Remember which host handler a worker-chosen token stands for.

        The token is the worker's claim, not ours: a compromised worker that hands the same token
        to two bindings would make revoking the harmless one also drop the privileged one's entry
        in a map keyed by token, leaving the privileged handler callable for good. A duplicate is
        therefore proof of a lying worker, and the session ends."""
        if token in self._token_to_hid:
            self._handlers.pop(hid, None)
            self._kill()
            raise WorkerCrashed("worker reused a capability token")
        self._token_to_hid[token] = hid

    def bind_function(self, name: str, handler: Callable[..., Any]) -> int:
        """Expose a host function as a global; returns its capability token."""
        from ._tools import ToolBridge

        ToolBridge._check_name(name, what="function name")  # noqa: SLF001
        hid = next(self._hids)
        is_async = inspect.iscoroutinefunction(handler)
        self._handlers[hid] = (handler, is_async)
        try:
            token = self._request(
                {"t": "bind_function", "name": name, "hid": hid, "async": is_async}
            )
        except BaseException:
            self._handlers.pop(hid, None)
            raise
        if not _is_token(token):
            self._handlers.pop(hid, None)
            self._kill()
            raise WorkerCrashed("worker returned a malformed capability token")
        self._register_token(token, hid)
        return token

    def bind_object(self, name: str, obj: Mapping[str, Any]) -> dict[str, int]:
        """Expose a mapping as a global object; callables become host functions."""
        from ._tools import ToolBridge

        ToolBridge._check_name(name, what="object name")  # noqa: SLF001
        for key in obj:
            ToolBridge._check_name(key, what="property name")  # noqa: SLF001
        entries: dict[str, Any] = {}
        hids: dict[str, int] = {}
        for key, value in obj.items():
            if callable(value):
                hid = next(self._hids)
                is_async = inspect.iscoroutinefunction(value)
                self._handlers[hid] = (value, is_async)
                hids[key] = hid
                entries[key] = {"hid": hid, "async": is_async}
            else:
                entries[key] = {"v": _wire.Enc(value)}
        try:
            tokens = self._request(
                {"t": "bind_object", "name": name, "entries": entries}
            )
        except BaseException:
            for hid in hids.values():
                self._handlers.pop(hid, None)
            raise
        if (
            not isinstance(tokens, dict)
            or set(tokens) != set(hids)
            or not all(isinstance(k, str) and _is_token(v) for k, v in tokens.items())
        ):
            for hid in hids.values():
                self._handlers.pop(hid, None)
            self._kill()
            raise WorkerCrashed("worker returned malformed capability tokens")
        for key, token in tokens.items():
            self._register_token(token, hids[key])
        return tokens

    def revoke_op(self, op_id: int) -> bool:
        """Revoke a capability. The host handler is dropped as well as the worker's token."""
        # Drop the host handler first and unconditionally: what the worker answers (or whether it
        # answers) must not decide whether a revoked capability can still be called.
        hid = self._token_to_hid.pop(op_id, None)
        if hid is not None:
            self._handlers.pop(hid, None)
            self._revoked_hids[hid] = None
            if len(self._revoked_hids) > _REVOKED_MEMORY:
                self._revoked_hids.pop(next(iter(self._revoked_hids)))
        return bool(self._request({"t": "revoke", "token": op_id}))

    def add_static_module(self, name: str, source: str) -> None:
        self._request({"t": "add_module", "name": name, "source": source})

    def set_module_resolver(self, resolver: Callable[[str, str], str | None]) -> None:
        """Resolve import specifiers with a host function: `(specifier, referrer) -> str | None`."""
        hid = next(self._hids)
        self._handlers[hid] = (_checked_specifiers(resolver, 2), False)
        try:
            self._request({"t": "set_module_resolver", "hid": hid})
        except BaseException:
            self._handlers.pop(hid, None)
            raise

    def set_module_loader(self, loader: Callable[[str], Any]) -> None:
        """Supply module source with a host function: `(specifier) -> str`, sync or async.

        The source comes back across the process boundary as plain text; the worker, which
        is the one that compiles it, never sees the loader itself."""
        hid = next(self._hids)
        self._handlers[hid] = (
            _checked_specifiers(loader, 1),
            inspect.iscoroutinefunction(loader),
        )
        try:
            self._request({"t": "set_module_loader", "hid": hid})
        except BaseException:
            self._handlers.pop(hid, None)
            raise

    def eval_module(self, specifier: str) -> Any:
        """Evaluate a module synchronously and return its namespace as a dict."""
        return self._request(
            {"t": "eval_module", "specifier": specifier},
            soft_timeout=self._soft_timeout,
        )

    async def eval_module_async(
        self, specifier: str, *, timeout: float | int | timedelta | None = None
    ) -> Any:
        """Evaluate a module, awaiting top-level await; async host callbacks run on this loop."""
        soft = _seconds(timeout)
        if soft is None:
            soft = self._soft_timeout
        loop = asyncio.get_running_loop()
        message = {"t": "eval_module_async", "specifier": specifier, "timeout": soft}
        try:
            return await self._in_own_thread(message, soft, loop)
        except asyncio.CancelledError:
            self._kill()  # the thread cannot be interrupted, and the worker is mid-command
            raise


def _error_reply(
    cid: int, exc: BaseException, *, redact: bool = False
) -> dict[str, Any]:
    """Tell the worker what a host function raised: its class name and its message, separately,
    exactly what an in-process `Runtime` would hand to guest JS as `e.name` and `e.message`.
    A value that could not be encoded is the guest's TypeError, not a protocol fault.

    `redact` keeps the class name (guests branch on it) but replaces the message, for hosts whose
    tools put paths, queries or secrets in their error text."""
    etype = "TypeError" if isinstance(exc, _wire.WireError) else type(exc).__name__
    # pydeno's own guidance to the guest ("search for the tool first") is written by pydeno, not
    # by the host's tools, so it holds nothing to redact and is the whole point of the error.
    public = getattr(exc, "_pydeno_public", False) is True
    text = "host function failed" if redact and not public else str(exc)
    return {"t": "reply", "cid": cid, "err": text, "etype": etype}


def _is_token(value: object) -> bool:
    """A capability token is a plain integer; anything else from the worker is nonsense."""
    return isinstance(value, int) and not isinstance(value, bool)


async def _await(awaitable: Any) -> Any:
    return await awaitable


async def _call_guarded(handler: Callable[..., Any], args: list[Any]) -> Any:
    """Run an asynchronous host function with the re-entrancy flag set in *its* context, so that
    a handler calling back into the runtime that is waiting on it fails instead of deadlocking."""
    token = _IN_HOST_CALL.set(True)
    try:
        return await handler(*args)
    finally:
        _IN_HOST_CALL.reset(token)


# Where this `pydeno` package lives. The worker imports it from here, so parent and worker always
# run the same code (an `-I` worker would otherwise import whichever `pydeno` its own `sys.path`
# finds first, which need not be the parent's).
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# `-S`: no `site`, so no `.pth` file runs in the worker and its `sys.path` is the standard library
# plus this package's directory, *appended* so nothing next to `pydeno` can shadow a stdlib module.
# pydeno has no runtime dependencies, so the worker needs nothing else. Saves the `site` import.
_WORKER_BOOT = (
    "import sys; sys.path.append({!r}); from pydeno._worker import main; main()".format(
        _PACKAGE_PARENT
    )
)


def _worker_argv(python: str) -> list[str]:
    if python == sys.executable:
        return [python, "-I", "-S", "-c", _WORKER_BOOT]
    # Another interpreter may be another Python version, which cannot load this build's extension
    # module: it runs the `pydeno` it has installed itself.
    return [python, "-I", "-m", "pydeno._worker"]


def _start_worker(python: str) -> tuple[subprocess.Popen[bytes], Any]:
    stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed by close() / finalizer
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            _worker_argv(python),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env={},  # no host secrets ever reach the worker
            close_fds=True,
            start_new_session=True,  # so a kill takes any stray child with it
            bufsize=0,
        )
    except BaseException:
        stderr.close()
        raise
    return proc, stderr


# One worker kept ready. It holds no configuration and no data: it is a Python process that has
# finished importing and is blocked reading `init`, the same state a fresh spawn reaches, only
# earlier. It is handed out at most once (popped under the lock), and it exits by itself if the
# parent dies (stdin EOF) or is killed at interpreter exit.
_SPARE: tuple[subprocess.Popen[bytes], Any] | None = None
_SPARE_LOCK = threading.Lock()


def _take_worker() -> tuple[subprocess.Popen[bytes], Any]:
    global _SPARE  # noqa: PLW0603
    with _SPARE_LOCK:
        spare, _SPARE = _SPARE, None
    if spare is not None:
        if spare[0].poll() is None:
            return spare
        _terminate_process(*spare)  # died while waiting: reap it, start fresh
    return _start_worker(sys.executable)


def _refill_spare() -> None:
    def fill() -> None:
        global _SPARE  # noqa: PLW0603
        try:
            worker = _start_worker(sys.executable)
        except OSError:
            return
        with _SPARE_LOCK:
            if _SPARE is None:
                _SPARE = worker
                return
        _terminate_process(*worker)

    threading.Thread(target=fill, name="pydeno-prewarm", daemon=True).start()


def _discard_spare() -> None:
    global _SPARE  # noqa: PLW0603
    with _SPARE_LOCK:
        spare, _SPARE = _SPARE, None
    if spare is not None:
        _terminate_process(*spare)


def _terminate_process(proc: subprocess.Popen[bytes], stderr: Any) -> None:
    """Kill a worker's whole process group and reap it. Safe to call on one that is already
    gone, which is what makes it usable both from `_kill` and as a garbage-collection finalizer."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL does not time out
        pass
    if stderr is not None:
        try:
            stderr.close()
        except OSError:
            pass


def _idle_watch(ref: weakref.ref[IsolatedRuntime]) -> None:
    """Supervise the worker *between* commands too.

    The per-command checks only run while a command does. A compromised worker that waits until
    it is idle to eat memory or spin would otherwise go unnoticed until the next command. Holds
    the runtime only weakly, so a forgotten runtime can still be collected (and its worker killed
    by the finalizer)."""
    while True:
        rt = ref()
        if rt is None or rt._closed:  # noqa: SLF001
            return
        try:
            rt._idle_check()  # noqa: SLF001
        except Exception:  # noqa: BLE001, S110 - a watchdog must never die of its own check
            pass
        del rt
        time.sleep(_IDLE_CHECK_SECONDS)


def _kill_all_at_exit() -> None:
    for runtime in list(_LIVE):
        try:
            runtime._kill()
        except Exception:  # noqa: BLE001, S110
            pass


atexit.register(_kill_all_at_exit)


def _forget_parents_workers() -> None:
    """After `fork()`, the child holds the parent's runtimes and spare worker as inherited file
    descriptors. They are the parent's: the child must not kill them at exit (its atexit hook and
    finalizers would), must not hand out the parent's spare, and must not write frames into a
    pipe the parent is also reading. So it forgets them, and `_request` refuses to run on one."""
    global _SPARE, _SPARE_LOCK  # noqa: PLW0603
    _SPARE = None
    # A parent thread may have held this lock at the instant of the fork; that thread does not
    # exist here, so the lock would never be released. A fresh one cannot deadlock the child.
    _SPARE_LOCK = threading.Lock()
    for runtime in list(_LIVE):
        runtime._finalizer.detach()  # noqa: SLF001
    _LIVE.clear()


os.register_at_fork(after_in_child=_forget_parents_workers)
atexit.register(_discard_spare)
