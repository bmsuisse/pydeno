"""The synchronous runtimes enforce their limits while a host handler runs (#84).

`IsolatedRuntime` runs a synchronous host handler (`on_console`, the front door's
`print_callback`, a sync tool) on the thread that also supervises the command. Before this was
fixed, nothing checked the hard deadline while such a handler ran: a 6 s `on_console` under a 1 s
deadline let the worker live for about 6 s (the async runtime kills it at about 2 s, the deadline
plus the console allowance). Now the idle watchdog stands in for the pump while a handler runs: it
kills the *worker* on time, and the command raises `RuntimeTimeout` once the handler returns.

What is pinned here:

- the worker dies at about deadline + console allowance while a slow console handler is still
  running, in `eval`, `execute`, the front door and agent sessions;
- the handler is not interrupted and runs on the calling thread (no thread-affinity surprise);
- a slow console write within the allowance still does not kill a run;
- `max_host_wait` is enforced during a slow synchronous tool the same way.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    IsolatedRuntime,
    Pydeno,
    PydenoTimeoutError,
    RuntimeConfig,
    RuntimeTimeout,
)

HARD = 1.0
# The console allowance is one hard deadline, so the worker must die at about 2 * HARD.
KILL_AT = 2 * HARD
# A handler that outlives the kill by a wide margin, so "killed during the handler" is unambiguous
# even on a loaded machine.
HANDLER = 4.5
# Latest acceptable death: the watchdog polls every 0.1 s; the rest is slack for a loaded runner.
LATEST = KILL_AT + 1.2


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # a worker this test started
    except ProcessLookupError:
        return False
    return True


class _SlowHandler:
    """Sleeps `HANDLER` seconds on its first call, noting when the worker died meanwhile, and on
    which thread it ran. Later calls return at once."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self.start = 0.0
        self.died_after: float | None = None
        self.finished_after: float | None = None
        self.thread: int | None = None
        self.calls = 0

    def __call__(self, *args: Any) -> None:
        self.calls += 1
        if self.calls > 1:
            return
        self.thread = threading.get_ident()
        assert self.pid is not None
        end = time.monotonic() + HANDLER
        while time.monotonic() < end:
            if self.died_after is None and not _alive(self.pid):
                self.died_after = time.monotonic() - self.start
            time.sleep(0.02)
        self.finished_after = time.monotonic() - self.start

    def check(self, caller: int | None) -> None:
        assert self.died_after is not None, (
            "the worker outlived its deadline while the handler ran"
        )
        assert HARD < self.died_after < LATEST, self.died_after
        # The handler was not cut short, and it ran on the thread that runs the command.
        assert self.finished_after is not None
        assert self.finished_after >= HANDLER
        if caller is not None:
            assert self.thread == caller


@pytest.mark.parametrize("method", ["eval", "execute"])
def test_a_slow_on_console_is_cut_off_at_the_deadline(method: str) -> None:
    slow = _SlowHandler()
    cfg = RuntimeConfig(on_console=slow)
    with IsolatedRuntime(
        cfg,
        request_timeout=HARD,
        capture_console=method == "execute",
        prewarm=False,
    ) as rt:
        slow.pid = rt._proc.pid  # noqa: SLF001 - the worker this test watches
        slow.start = time.monotonic()
        if method == "eval":
            with pytest.raises(RuntimeTimeout, match="hard deadline"):
                rt.eval("console.log('x'); 1")
        else:  # `execute` reports a failed run in its result
            result = rt.execute("console.log('x'); 1")
            assert result.error_type == "RuntimeTimeout", result
            assert "hard deadline" in (result.error or ""), result
            assert result.stdout == "x\n"
        assert rt.is_closed()
    slow.check(threading.get_ident())


def test_a_slow_console_write_within_the_allowance_does_not_kill_the_run() -> None:
    def handler(level: str, args: list[Any]) -> None:
        time.sleep(0.6 * HARD)

    with IsolatedRuntime(
        RuntimeConfig(on_console=handler), request_timeout=HARD, prewarm=False
    ) as rt:
        assert rt.eval("console.log('a'); 42") == 42
        assert rt.eval("console.log('b'); 43") == 43  # the allowance is per command


def test_a_flood_of_quick_console_calls_is_still_bounded() -> None:
    def handler(level: str, args: list[Any]) -> None:
        time.sleep(0.002)

    with IsolatedRuntime(
        RuntimeConfig(on_console=handler),
        request_timeout=HARD,
        max_host_wait=60,
        prewarm=False,
    ) as rt:
        t = time.monotonic()
        with pytest.raises(RuntimeTimeout):
            rt.eval("for (;;) console.log('x')")
        assert time.monotonic() - t < LATEST + 1.0


def test_a_slow_print_callback_is_cut_off_in_a_feed() -> None:
    slow = _SlowHandler()
    limits = {"max_feed_duration_secs": HARD}
    with Pydeno(min_processes=1, limits=limits) as pool, pool.checkout() as session:
        slow.pid = session.worker_pid
        slow.start = time.monotonic()
        with pytest.raises(PydenoTimeoutError):
            session.feed_run("console.log('x'); 1", print_callback=slow)
    # The feed's console sink runs on the session's own thread (see `PydenoSession`).
    slow.check(None)


def test_a_slow_on_console_is_cut_off_in_an_agent_session() -> None:
    slow = _SlowHandler()
    s = AgentSandbox({}, config=RuntimeConfig(on_console=slow), timeout=HARD)
    try:
        slow.pid = s._core.rt._proc.pid  # noqa: SLF001 - the worker this test watches
        slow.start = time.monotonic()
        try:
            outcome = s.run("console.log('x'); return 1")
        except Exception as exc:  # noqa: BLE001 - either surface is a timeout
            outcome = exc
        error = getattr(outcome, "error", outcome)
        assert isinstance(error, (RuntimeTimeout, TimeoutError)), outcome
    finally:
        s.close()
    # Agent sessions run their commands on a thread of their own; the handler runs there.
    slow.check(None)


def test_max_host_wait_is_enforced_during_a_slow_sync_tool() -> None:
    state: dict[str, Any] = {}

    def slow_tool() -> int:
        end = time.monotonic() + HANDLER
        while time.monotonic() < end:
            if "died" not in state and not _alive(state["pid"]):
                state["died"] = time.monotonic() - state["start"]
            time.sleep(0.02)
        return 1

    with IsolatedRuntime(request_timeout=30, max_host_wait=HARD, prewarm=False) as rt:
        rt.bind_function("slow", slow_tool)
        state["pid"] = rt._proc.pid  # noqa: SLF001
        state["start"] = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="max_host_wait"):
            rt.eval("slow()")
    assert "died" in state, "the worker outlived max_host_wait while the tool ran"
    assert HARD < state["died"] < HARD + 1.2, state["died"]


def test_a_slow_tool_still_pauses_the_deadline() -> None:
    def slow() -> int:
        time.sleep(HARD + 0.5)
        return 1

    with IsolatedRuntime(request_timeout=HARD, prewarm=False) as rt:
        rt.bind_function("slow", slow)
        assert rt.eval("slow()") == 1
        assert rt.eval("1 + 1") == 2
