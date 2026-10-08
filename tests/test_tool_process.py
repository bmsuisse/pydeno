"""`ToolProcess`: host tools in a supervised child process (#45, item 5, phase 1; experimental).

What is pinned here:

- tools are importable `'module:function'` names (or importable module-level functions); closures,
  lambdas, `__main__` functions and callable objects are refused with a clear error;
- sync and async tools run in a child process, concurrently, with state that lives in the child;
- a call has a deadline (the guest gets the same `TimeoutError` as `tool_timeout`), a result cap, a
  memory ceiling and a CPU cap; each kills the tool host, and the next call starts a new one;
- a tool that segfaults, aborts or exits is contained and is a recorded failure of that call
  (`ToolProcessDied`) while the parent lives on;
- an exception keeps its class name and its message, redacted as `redact_host_errors` says, exactly
  like a tool run in the parent;
- empty environment by default; no process, thread or descriptor outlives `close()`, and the tool
  host exits when its parent dies;
- agent journals record a died tool host as a failed call and replay it without starting a tool
  host or running the tool;
- tools that stay in the parent are untouched;
- `sandbox=`: the tool host is confined with the worker's layers, and `"require"` refuses to start
  where one is missing.
"""

from __future__ import annotations

import asyncio
import functools
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

import toolproc_fixture_tools as T
from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    IsolatedRuntime,
    ToolProcess,
    ToolProcessDied,
    ToolProcessError,
    ToolProcessStartError,
    ToolResultTooLarge,
)
from pydeno import _sandbox
from pydeno._isolated import TOOL_TIMEOUT_MESSAGE
from pydeno._wire import WireError

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
# Variables the interpreter or the OS adds to a child whatever the caller passed (PEP 538 locale
# coercion; macOS's CoreFoundation).
_INTERPRETER_ENV = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
KEY = b"0123456789abcdef0123456789abcdef"
TIMED_OUT = f"TimeoutError:{TOOL_TIMEOUT_MESSAGE}"
HERE = os.path.dirname(os.path.abspath(__file__))
MOD = "toolproc_fixture_tools"


def _guest(call: str) -> str:
    """A guest expression that awaits `call` and reports what it threw."""
    return (
        "(async () => { try { await " + call + "; return 'returned' } "
        "catch (e) { return e.name + ':' + e.message } })()"
    )


def _run(rt: IsolatedRuntime, code: str) -> Any:
    return asyncio.run(rt.eval_async(code))


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


def _named(prefix: str) -> int:
    return sum(t.name.startswith(prefix) for t in threading.enumerate())


def _settle(predicate: Callable[[], Any], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _gone(pid: int) -> bool:
    """The process no longer runs (a zombie nobody has reaped yet does not run either)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[0] == b"Z"
    except OSError:
        return False


def _bind(rt: IsolatedRuntime, tp: ToolProcess, **names: str) -> None:
    for name, spec in names.items():
        rt.bind_function(name, tp.tool(f"{MOD}:{spec}"))


@pytest.fixture
def tp() -> Any:
    made: list[ToolProcess] = []

    def make(**kwargs: Any) -> ToolProcess:
        proc = ToolProcess(**kwargs)
        made.append(proc)
        return proc

    yield make
    for proc in made:
        proc.close()


# ---------------------------------------------------------------------------
# what may be a tool
# ---------------------------------------------------------------------------


def _module_level(x: int) -> int:
    return x


class TestImportableOnly:
    def test_a_lambda_is_refused_with_a_clear_error(self, tp: Any) -> None:
        with pytest.raises(
            TypeError, match=r"lambda.*cannot receive it.*module:function"
        ):
            tp().tool(lambda x: x)

    def test_a_closure_is_refused_with_a_clear_error(self, tp: Any) -> None:
        secret = 1

        def closure() -> int:
            return secret

        with pytest.raises(TypeError, match=r"closure.*module:function"):
            tp().tool(closure)

    def test_a_function_of_main_is_refused(self, tp: Any) -> None:
        namespace: dict[str, Any] = {"__name__": "__main__"}
        exec("def f():\n    return 1\n", namespace)  # noqa: S102
        with pytest.raises(TypeError, match="__main__"):
            tp().tool(namespace["f"])
        with pytest.raises(ValueError, match="__main__"):
            tp().tool("__main__:f")

    def test_callable_objects_are_refused(self, tp: Any) -> None:
        class Tool:
            def __call__(self) -> int:
                return 1

        for bad in (Tool(), functools.partial(T.add, 1), T.COUNTER.get, len):
            with pytest.raises(TypeError, match="importable module-level functions"):
                tp().tool(bad)

    def test_a_function_that_a_decorator_replaced_is_refused(self, tp: Any) -> None:
        # The module's own attribute under that name is not this function.
        def impostor() -> None: ...

        impostor.__qualname__ = "add"
        impostor.__module__ = MOD
        with pytest.raises(TypeError, match="not the function that module exports"):
            tp().tool(impostor)

    @pytest.mark.parametrize(
        "bad", ["nocolon", "a:b:c", ":f", "m:", "m:1f", "a b:c", ""]
    )
    def test_a_malformed_name_is_refused(self, tp: Any, bad: str) -> None:
        with pytest.raises(ValueError, match="module:function"):
            tp().tool(bad)

    def test_a_non_string_non_function_is_refused(self, tp: Any) -> None:
        with pytest.raises(TypeError):
            tp().tool(None)  # type: ignore[arg-type]

    def test_a_function_keeps_its_signature_and_docstring(self, tp: Any) -> None:
        import inspect

        handler = tp().tool(T.add)
        assert handler.__name__ == "add"
        assert handler.__doc__ == "Add two numbers."
        assert inspect.signature(handler) == inspect.signature(T.add)
        assert inspect.iscoroutinefunction(handler)

    def test_a_missing_module_fails_the_call_not_the_registration(
        self, tp: Any
    ) -> None:
        proc = tp()
        handler = proc.tool("no_such_module_anywhere:f")
        with IsolatedRuntime() as rt:
            rt.bind_function("f", handler)
            assert _run(rt, _guest("f()")) == "ModuleNotFoundError:host function failed"
        # The host survives a failed import and still serves what it can.
        with IsolatedRuntime() as rt:
            _bind(rt, proc, add="add")
            assert _run(rt, "add(1, 2)") == 3
        assert proc.starts == 1

    def test_the_options_are_validated(self) -> None:
        for kwargs in (
            {"call_timeout": 0},
            {"call_timeout": float("nan")},
            {"call_timeout": True},
            {"max_result_bytes": 0},
            {"max_result_bytes": True},
            {"max_memory": -1},
            {"cpu_seconds": 0},
            {"sandbox": "off"},
            {"sandbox": True},
            {"env": {"A": 1}},
        ):
            with pytest.raises((TypeError, ValueError)):
                ToolProcess(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------


class TestCalls:
    def test_a_sync_tool_runs_in_another_process(self, tp: Any) -> None:
        proc = tp()
        with IsolatedRuntime() as rt:
            _bind(rt, proc, add="add", where="pid")
            assert _run(rt, "add(2, 3)") == 5
            tool_pid = _run(rt, "where()")
        assert tool_pid != os.getpid()
        assert tool_pid == proc.pid

    def test_an_async_tool_runs_in_another_process(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), aadd="aadd")
            assert _run(rt, "aadd(40, 2)") == 42

    def test_the_guest_gets_a_promise_for_every_tool(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), add="add")
            assert _run(rt, "add(1, 2) instanceof Promise") is True

    def test_values_cross_both_ways_as_they_do_for_a_tool_in_the_parent(
        self, tp: Any
    ) -> None:
        code = (
            "echo({a: [1, 2n ** 70n, 'x'], b: new Uint8Array([1, 2]), "
            "c: new Set([1]), d: null, e: 1.5, f: true})"
        )
        with IsolatedRuntime() as rt:
            rt.bind_function("echo", T.echo)
            expected = _run(rt, code)
        with IsolatedRuntime() as rt:
            rt.bind_function("echo", tp().tool(f"{MOD}:echo"))
            got = _run(rt, code)
        assert got == expected
        assert (
            got["a"] == [1, 2**70, "x"] and got["b"] == b"\x01\x02" and got["c"] == {1}
        )

    def test_state_lives_in_the_tool_host_and_survives_between_calls(
        self, tp: Any
    ) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), count="count")
            assert [_run(rt, "count()") for _ in range(3)] == [1, 2, 3]
            # The parent's copy of the module is untouched.
            assert T.COUNTER == {"n": 0}

    def test_the_tool_host_is_started_once_and_only_when_needed(self, tp: Any) -> None:
        proc = tp()
        proc.tool(f"{MOD}:add")
        assert proc.starts == 0 and proc.pid is None
        with IsolatedRuntime() as rt:
            _bind(rt, proc, add="add")
            _run(rt, "add(1, 1)")
            _run(rt, "add(1, 1)")
        assert proc.starts == 1

    def test_an_argument_that_cannot_cross_is_refused_and_starts_nothing(
        self, tp: Any
    ) -> None:
        proc = tp()
        handler = proc.tool(T.add)

        async def go() -> None:
            # What the runtime hands the guest as a TypeError, as for any host tool.
            with pytest.raises(WireError):
                await handler(object(), 1)

        asyncio.run(go())
        assert proc.starts == 0

    def test_an_unencodable_result_is_the_guests_type_error(self, tp: Any) -> None:
        with IsolatedRuntime(redact_host_errors=False) as rt:
            _bind(rt, tp(), bad="unencodable", add="add")
            assert _run(rt, _guest("bad()")).startswith("TypeError:")
            assert _run(rt, "add(1, 2)") == 3

    def test_output_on_stdout_cannot_corrupt_the_protocol(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), chatty="chatty", add="add")
            assert _run(rt, "chatty()") == "done"
            assert _run(rt, "add(1, 2)") == 3

    def test_calls_run_concurrently_sync_tools_on_threads(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), pair="barrier_pair")
            assert _run(rt, "Promise.all([pair('a'), pair('b')])") == ["both", "both"]

    def test_calls_run_concurrently_async_tools_on_one_loop(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), pair="abarrier")
            assert _run(rt, "Promise.all([pair('a'), pair('b')])") == ["both", "both"]

    def test_a_slow_call_does_not_hold_up_a_quick_one(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), nap="asleep", add="add")
            start = time.monotonic()
            out = _run(
                rt,
                "(async () => { const s = nap(1.5); const a = await add(1, 2); "
                "return [a, await s] })()",
            )
            assert out == [3, "slept"]
            assert time.monotonic() - start < 10

    def test_calls_after_close_fail_clearly(self, tp: Any) -> None:
        proc = tp()
        handler = proc.tool(T.add)
        proc.close()

        async def go() -> None:
            with pytest.raises(ToolProcessError, match="closed"):
                await handler(1, 2)

        asyncio.run(go())


# ---------------------------------------------------------------------------
# exceptions: class name and (redacted) message, as for a tool in the parent
# ---------------------------------------------------------------------------


class TestExceptions:
    @pytest.mark.parametrize("redact", [True, False])
    def test_a_tool_error_matches_the_same_tool_run_in_the_parent(
        self, tp: Any, redact: bool
    ) -> None:
        proc = tp()
        guest = {
            "boom": _guest("boom('secret path /etc/x')"),
            "keyerror": _guest("keyerror('k')"),
            "custom": _guest("custom('m')"),
        }
        in_parent: dict[str, str] = {}
        with IsolatedRuntime(redact_host_errors=redact) as rt:
            for name in ("boom", "keyerror", "custom"):
                rt.bind_function(name, getattr(T, name))
            for name, code in guest.items():
                in_parent[name] = _run(rt, code)
        with IsolatedRuntime(redact_host_errors=redact) as rt:
            _bind(rt, proc, boom="boom", keyerror="keyerror", custom="custom")
            for name, code in guest.items():
                assert _run(rt, code) == in_parent[name]
        assert in_parent["boom"] == (
            "ValueError:host function failed"
            if redact
            else "ValueError:secret path /etc/x"
        )
        assert in_parent["custom"].startswith("CustomFailure:")
        assert proc.starts == 1  # an exception is an answer, not a death

    def test_pydeno_own_failures_are_not_redacted(self, tp: Any) -> None:
        with IsolatedRuntime(redact_host_errors=True) as rt:
            _bind(rt, tp(call_timeout=0.3), nap="sleep", seg="segfault")
            assert _run(rt, _guest("nap(30)")) == TIMED_OUT
            out = _run(rt, _guest("seg()"))
        assert out.startswith("ToolProcessDied:the tool process died (killed by signal")

    def test_start_errors_are_redacted_like_tool_errors(self, tp: Any) -> None:
        proc = tp(sandbox="auto")
        proc.tool("no_such_module_anywhere:f")
        with pytest.raises(ToolProcessStartError, match="no_such_module_anywhere"):
            proc.start()
        for redact, expected in (
            (True, "ToolProcessStartError:host function failed"),
            (False, None),
        ):
            with IsolatedRuntime(redact_host_errors=redact) as rt:
                rt.bind_function("f", proc.tool("no_such_module_anywhere:f"))
                out = _run(rt, _guest("f()"))
            if expected is not None:
                assert out == expected
            else:
                assert (
                    out.startswith("ToolProcessStartError:") and "no_such_module" in out
                )


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------


class TestDeadline:
    @pytest.mark.parametrize("tool", ["sleep", "asleep"])
    @pytest.mark.parametrize("redact", [True, False])
    def test_an_overrun_kills_the_tool_host_and_the_guest_gets_a_timeout(
        self, tp: Any, tool: str, redact: bool
    ) -> None:
        proc = tp(call_timeout=0.4)
        with IsolatedRuntime(redact_host_errors=redact) as rt:
            _bind(rt, proc, nap=tool, where="pid")
            first = _run(rt, "where()")
            start = time.monotonic()
            assert _run(rt, _guest("nap(60)")) == TIMED_OUT
            assert time.monotonic() - start < 10
            assert _settle(lambda: _gone(first))
            second = _run(rt, "where()")
        assert second != first
        assert proc.starts == 2

    def test_the_next_call_recovers_and_state_restarts(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(call_timeout=0.4), nap="sleep", count="count")
            assert [_run(rt, "count()") for _ in range(2)] == [1, 2]
            assert _run(rt, _guest("nap(60)")) == TIMED_OUT
            assert _run(rt, "count()") == 1

    def test_a_quick_call_under_the_deadline_is_untouched(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(call_timeout=30), nap="sleep")
            assert _run(rt, "nap(0.05)") == "slept"

    def test_other_calls_in_flight_die_with_the_host_and_say_so(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(call_timeout=0.6), nap="asleep")
            out = _run(
                rt,
                "Promise.all(["
                + _guest("nap(60)")
                + ", "
                + _guest("nap(0.1)")
                + ", "
                + _guest("nap(60)")
                + "])",
            )
        assert out[0] == TIMED_OUT and out[2] == TIMED_OUT
        assert out[1] in (
            "returned",
            "ToolProcessDied:the tool process was killed: a call "
            "outlasted its deadline",
        )

    def test_off_with_none(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(call_timeout=None), nap="sleep")
            assert _run(rt, "nap(1.2)") == "slept"

    def test_the_deadline_holds_even_when_nobody_awaits_the_call(self, tp: Any) -> None:
        proc = tp(call_timeout=0.4)
        handler = proc.tool(T.sleep)

        async def go() -> int | None:
            task = asyncio.ensure_future(handler(60))
            await asyncio.sleep(0.2)
            pid = proc.pid
            task.cancel()
            assert await asyncio.gather(task, return_exceptions=True)
            return pid

        pid = asyncio.run(go())
        assert pid is not None and _settle(lambda: _gone(pid))


class TestResultCap:
    @pytest.mark.parametrize("redact", [True, False])
    def test_an_oversized_result_is_refused_in_the_tool_host(
        self, tp: Any, redact: bool
    ) -> None:
        proc = tp(max_result_bytes=1000)
        with IsolatedRuntime(redact_host_errors=redact) as rt:
            _bind(rt, proc, big="big", where="pid")
            first = _run(rt, "where()")
            out = _run(rt, _guest("big(5000)"))
            assert out.startswith("ToolResultTooLarge:the tool's result is ")
            assert "over max_result_bytes" in out
            assert _run(rt, "(async () => (await big(100)).length)()") == 100
            # Not a death: the same tool host answered the next call.
            assert _run(rt, "where()") == first
        assert proc.starts == 1

    def test_the_default_cap_is_one_mebibyte(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), big="big")
            assert _run(rt, "(async () => (await big(900000)).length)()") == 900000
            assert _run(rt, _guest("big(1100000)")).startswith("ToolResultTooLarge:")

    def test_the_exception_type_is_public_api(self, tp: Any) -> None:
        proc = tp(max_result_bytes=100)
        handler = proc.tool(T.big)

        async def go() -> None:
            with pytest.raises(ToolResultTooLarge):
                await handler(10_000)

        asyncio.run(go())


class TestMemory:
    def test_resident_memory_over_the_ceiling_kills_the_tool_host(
        self, tp: Any
    ) -> None:
        # A shared mapping counts as resident but not toward the kernel's data limit, so this is
        # the parent's sampled ceiling (the one every platform has) and nothing else.
        proc = tp(max_memory=150 << 20, call_timeout=None)
        with IsolatedRuntime() as rt:
            _bind(rt, proc, eat="eat_shared", where="pid", add="add")
            first = _run(rt, "where()")
            out = _run(rt, _guest("eat(600)"))
            assert out.startswith(
                "ToolProcessDied:the tool process was killed: it used "
            )
            assert f"max_memory={150 << 20}" in out
            assert _settle(lambda: _gone(first))
            assert _run(rt, "add(1, 2)") == 3
        assert proc.starts == 2

    @pytest.mark.linux_only
    def test_a_private_allocation_is_stopped_by_the_kernel_ceiling_or_the_poll(
        self, tp: Any
    ) -> None:
        proc = tp(max_memory=150 << 20, call_timeout=None)
        with IsolatedRuntime() as rt:
            _bind(rt, proc, eat="eat_private", add="add")
            out = _run(rt, _guest("eat(2000)"))
            assert out.split(":")[0] in ("MemoryError", "ToolProcessDied")
            assert _run(rt, "add(1, 2)") == 3

    def test_without_a_ceiling_a_modest_allocation_is_fine(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), eat="eat_private")
            assert _run(rt, "eat(50)") == 50


class TestCpu:
    def test_a_runaway_loop_is_killed_by_the_cpu_cap(self, tp: Any) -> None:
        proc = tp(cpu_seconds=0.5, call_timeout=None)
        with IsolatedRuntime() as rt:
            _bind(rt, proc, spin="spin", add="add", where="pid")
            first = _run(rt, "where()")
            start = time.monotonic()
            out = _run(rt, _guest("spin()"))
            assert time.monotonic() - start < 15
            assert out.startswith(
                "ToolProcessDied:the tool process was killed: it used more than "
            )
            assert "cpu_seconds=0.5s" in out
            assert _settle(lambda: _gone(first))
            assert _run(rt, "add(1, 2)") == 3

    def test_the_cap_is_per_call_not_cumulative(self, tp: Any) -> None:
        proc = tp(cpu_seconds=5.0, call_timeout=None)
        with IsolatedRuntime() as rt:
            _bind(rt, proc, add="add")
            assert [_run(rt, "add(1, 1)") for _ in range(30)] == [2] * 30
        assert proc.starts == 1


# ---------------------------------------------------------------------------
# a tool that crashes
# ---------------------------------------------------------------------------


class TestCrashes:
    @pytest.mark.parametrize(
        ("tool", "expected"),
        [
            ("segfault", "killed by signal SIGSEGV"),
            ("abort", "killed by signal SIGABRT"),
            ("hard_exit", "exit code 7"),
        ],
    )
    def test_a_crash_is_contained_and_is_a_failure_of_that_call(
        self, tp: Any, tool: str, expected: str
    ) -> None:
        proc = tp()
        with IsolatedRuntime() as rt:
            _bind(rt, proc, crash=tool, add="add", where="pid")
            before = _run(rt, "where()")
            out = _run(rt, _guest("crash()"))
            assert out == f"ToolProcessDied:the tool process died ({expected})"
            assert _settle(lambda: _gone(before))
            # The parent, the runtime and the next call are all fine.
            assert _run(rt, "add(2, 2)") == 4
            assert _run(rt, "where()") != before
        assert proc.starts == 2

    def test_it_dies_again_and_restarts_again(self, tp: Any) -> None:
        proc = tp()
        with IsolatedRuntime() as rt:
            _bind(rt, proc, crash="segfault", add="add")
            for n in range(3):
                assert _run(rt, _guest("crash()")).startswith("ToolProcessDied:")
                assert _run(rt, "add(1, 1)") == 2
        assert proc.starts == 4  # each crash kills the host the previous add started

    def test_calls_in_flight_when_it_dies_all_fail(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), crash="segfault", nap="asleep")
            out = _run(
                rt,
                "Promise.all([" + _guest("nap(30)") + ", " + _guest("crash()") + "])",
            )
        assert [o.split(":")[0] for o in out] == ["ToolProcessDied", "ToolProcessDied"]

    def test_the_exception_is_public_api(self, tp: Any) -> None:
        handler = tp().tool(T.segfault)

        async def go() -> None:
            with pytest.raises(ToolProcessDied, match="SIGSEGV"):
                await handler()

        asyncio.run(go())

    def test_a_tool_may_not_take_the_parent_with_it_via_the_process_group(
        self, tp: Any
    ) -> None:
        # The tool host is its own session: killing its group does not touch the parent's.
        proc = tp()
        proc.tool(T.add)
        proc.start()
        assert proc.pid is not None
        assert os.getpgid(proc.pid) == proc.pid != os.getpgid(0)


# ---------------------------------------------------------------------------
# environment, lifecycle, cleanup
# ---------------------------------------------------------------------------


class TestEnvironment:
    def test_the_environment_is_empty_by_default(
        self, tp: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("PYDENO_TOOLPROC_SECRET", "hunter2")
        with IsolatedRuntime() as rt:
            _bind(rt, tp(), env="env")
            assert (
                set(_run(rt, "env()")) <= _INTERPRETER_ENV
            )  # CPython / macOS may add their own

    def test_the_caller_may_pass_one(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            _bind(rt, tp(env={"ONLY": "this"}), env="env")
            assert set(_run(rt, "env()")) - _INTERPRETER_ENV == {"ONLY"}


class TestCleanup:
    def test_close_leaves_no_process_thread_or_descriptor(self, tp: Any) -> None:
        before_fds, before_threads = _fds(), _named("pydeno-toolproc")
        proc = tp()
        with IsolatedRuntime() as rt:
            _bind(rt, proc, add="add", crash="segfault", where="pid")
            pids = [_run(rt, "where()")]
            _run(rt, _guest("crash()"))
            pids.append(_run(rt, "where()"))
        proc.close()
        assert all(_settle(lambda p=p: _gone(p)) for p in pids)
        assert _settle(lambda: _named("pydeno-toolproc") == before_threads)
        assert _settle(lambda: _fds() == before_fds)

    def test_closing_the_runtime_does_not_close_a_tool_process_it_does_not_own(
        self, tp: Any
    ) -> None:
        proc = tp()
        with IsolatedRuntime() as rt:
            _bind(rt, proc, where="pid")
            pid = _run(rt, "where()")
        assert proc.pid == pid and not _gone(pid)
        with IsolatedRuntime() as rt:
            _bind(rt, proc, where="pid")
            assert _run(rt, "where()") == pid

    def test_close_is_idempotent_and_a_context_manager_closes(self) -> None:
        with ToolProcess() as proc:
            proc.tool(T.add)
            proc.start()
            pid = proc.pid
        assert pid is not None and _settle(lambda: _gone(pid))
        proc.close()

    def test_a_forgotten_tool_process_is_collected_and_its_host_killed(self) -> None:
        import gc

        proc = ToolProcess()
        proc.tool(T.add)
        proc.start()
        pid = proc.pid
        assert pid is not None
        del proc
        gc.collect()
        assert _settle(lambda: _gone(pid))

    def test_close_while_a_call_is_in_flight_fails_it_as_a_death(self, tp: Any) -> None:
        proc = tp(call_timeout=None)
        handler = proc.tool(T.sleep)

        async def go() -> None:
            task = asyncio.ensure_future(handler(60))
            await asyncio.sleep(0.5)
            await asyncio.get_running_loop().run_in_executor(None, proc.close)
            with pytest.raises(ToolProcessDied, match="closed"):
                await task

        asyncio.run(go())

    def test_the_tool_host_exits_when_its_parent_dies(self) -> None:
        script = (
            "import asyncio, os, sys\n"
            f"sys.path.insert(0, {HERE!r})\n"
            "from pydeno import ToolProcess\n"
            f"tp = ToolProcess(); f = tp.tool('{MOD}:pid')\n"
            "print(asyncio.run(f()), flush=True)\n"
            "os.kill(os.getpid(), 9)\n"
        )
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
        assert done.returncode == -9, done.stderr
        pid = int(done.stdout.strip())
        assert _settle(lambda: _gone(pid), timeout=15), (
            "the tool host outlived its parent"
        )

    @pytest.mark.filterwarnings("ignore:This process:DeprecationWarning")
    def test_a_forked_child_does_not_signal_its_parents_tool_host(
        self, tp: Any
    ) -> None:
        if not hasattr(os, "fork"):
            pytest.fail("no fork on a platform that runs this file")
        proc = tp()
        proc.tool(T.add)
        proc.start()
        pid = proc.pid
        child = os.fork()
        if child == 0:
            code = 1
            try:
                handler = proc.tool(T.add)

                async def go() -> None:
                    with pytest.raises(ToolProcessError, match="another process"):
                        await handler(1, 2)

                asyncio.run(go())
                proc.close()
                code = 0
            finally:
                os._exit(code)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert pid is not None and not _gone(pid) and proc.pid == pid


# ---------------------------------------------------------------------------
# AsyncIsolatedRuntime parity
# ---------------------------------------------------------------------------


class TestAsyncRuntime:
    async def test_tools_run_and_fail_like_on_the_sync_runtime(self, tp: Any) -> None:
        proc = tp(call_timeout=0.4, max_result_bytes=1000)
        async with AsyncIsolatedRuntime(redact_host_errors=True) as rt:
            for name, spec in (
                ("add", "add"),
                ("aadd", "aadd"),
                ("boom", "boom"),
                ("crash", "segfault"),
                ("nap", "sleep"),
                ("big", "big"),
                ("env", "env"),
            ):
                await rt.bind_function(name, proc.tool(f"{MOD}:{spec}"))
            assert await rt.eval("add(1, 2)") == 3
            assert await rt.eval("aadd(1, 2)") == 3
            assert (
                await rt.eval(_guest("boom('x')")) == "ValueError:host function failed"
            )
            assert (await rt.eval(_guest("crash()"))).startswith("ToolProcessDied:")
            assert await rt.eval("add(5, 5)") == 10
            assert await rt.eval(_guest("nap(30)")) == TIMED_OUT
            assert await rt.eval("add(6, 6)") == 12
            assert (await rt.eval(_guest("big(5000)"))).startswith(
                "ToolResultTooLarge:"
            )
            assert set(await rt.eval("env()")) <= _INTERPRETER_ENV

    async def test_calls_run_concurrently(self, tp: Any) -> None:
        async with AsyncIsolatedRuntime() as rt:
            await rt.bind_function("pair", tp().tool(f"{MOD}:barrier_pair"))
            assert await rt.eval("Promise.all([pair('a'), pair('b')])") == [
                "both",
                "both",
            ]

    async def test_the_event_loop_is_not_blocked_by_a_slow_tool(self, tp: Any) -> None:
        async with AsyncIsolatedRuntime() as rt:
            await rt.bind_function("nap", tp().tool(f"{MOD}:sleep"))
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.05)
                    ticks += 1

            ticker = asyncio.ensure_future(tick())
            assert await rt.eval("nap(1)") == "slept"
            ticker.cancel()
            assert ticks >= 5


# ---------------------------------------------------------------------------
# agent journals
# ---------------------------------------------------------------------------

SCRIPT = (
    "globalThis.out = [];"
    "for (const call of [() => crash(), () => add(1, 2), () => crash(), () => nap(60)]) {"
    "  try { globalThis.out.push(['ok', await call()]); }"
    "  catch (e) { globalThis.out.push([e.name, e.message]); } }"
    "return globalThis.out;"
)


def _never(*args: Any) -> Any:
    raise AssertionError("a recorded tool call must not run again on replay")


def _agent_tools(proc: ToolProcess) -> dict[str, Any]:
    return {
        "crash": proc.tool(T.segfault),
        "add": proc.tool(T.add),
        "nap": proc.tool(T.sleep),
    }


def _died(message: str) -> bool:
    return message.startswith("the tool process died (killed by signal")


class TestAgentJournal:
    def test_a_died_tool_host_is_a_recorded_failure_and_replay_never_reruns_it(
        self, tp: Any
    ) -> None:
        proc = tp(call_timeout=0.5)
        with AgentSandbox(_agent_tools(proc)) as sb:
            out = sb.run(SCRIPT)
            records = [r for r in sb._records if r[0] == "ans"]  # noqa: SLF001
            blob = sb.dump(KEY)
        assert [o[0] for o in out] == [
            "ToolProcessDied",
            "ok",
            "ToolProcessDied",
            "TimeoutError",
        ]
        assert (
            out[1] == ["ok", 3]
            and _died(out[0][1])
            and out[3][1] == TOOL_TIMEOUT_MESSAGE
        )
        assert [r[:3] for r in records] == [
            ["ans", "e", "ToolProcessDied"],
            ["ans", "v", records[1][2]],
            ["ans", "e", "ToolProcessDied"],
            ["ans", "e", "TimeoutError"],
        ]
        assert _died(records[0][3]) and records[3][3] == TOOL_TIMEOUT_MESSAGE
        starts = proc.starts
        for _ in range(2):
            with AgentSandbox.load(
                blob,
                KEY,
                {"crash": _never, "add": _never, "nap": _never},
            ) as replayed:
                assert replayed.run("return globalThis.out") == out
        # Replay fed the recorded failures back identically and touched no tool host.
        assert proc.starts == starts

    @pytest.mark.parametrize("redact", [True, False])
    def test_the_recorded_message_is_pydeno_s_own_text_either_way(
        self, tp: Any, redact: bool
    ) -> None:
        proc = tp()
        with AgentSandbox(_agent_tools(proc), redact_host_errors=redact) as sb:
            out = sb.run(
                "try { await crash() } catch (e) { return [e.name, e.message] }"
            )
            blob = sb.dump(KEY)
        assert out[0] == "ToolProcessDied" and _died(out[1])
        with AgentSandbox.load(
            blob,
            KEY,
            {"crash": _never, "add": _never, "nap": _never},
            redact_host_errors=redact,
        ) as replayed:
            assert replayed.run("return 1") == 1

    def test_a_tool_error_in_the_host_is_recorded_with_its_redacted_message(
        self, tp: Any
    ) -> None:
        proc = tp()
        with AgentSandbox({"boom": proc.tool(T.boom)}) as sb:
            out = sb.run(
                "try { await boom('secret') } catch (e) { return [e.name, e.message] }"
            )
            records = [r for r in sb._records if r[0] == "ans"]  # noqa: SLF001
        assert out == ["ValueError", "host function failed"]
        assert records == [["ans", "e", "ValueError", "host function failed"]]

    def test_a_session_survives_a_died_tool_host_and_continues(self, tp: Any) -> None:
        proc = tp()
        with AgentSandbox(_agent_tools(proc)) as sb:
            sb.run("try { await crash() } catch (e) {}; return 1")
            assert sb.run("return await add(20, 22)") == 42
        assert proc.starts == 2

    async def test_the_async_class_records_and_replays_the_same_way(
        self, tp: Any
    ) -> None:
        proc = tp(call_timeout=0.5)
        async with AsyncAgentSandbox(_agent_tools(proc)) as sb:
            out = await sb.run(SCRIPT)
            blob = await sb.dump(KEY)
        assert [o[0] for o in out] == [
            "ToolProcessDied",
            "ok",
            "ToolProcessDied",
            "TimeoutError",
        ]
        starts = proc.starts
        async with await AsyncAgentSandbox.load(
            blob, KEY, {"crash": _never, "add": _never, "nap": _never}
        ) as replayed:
            assert await replayed.run("return globalThis.out") == out
        # A journal from the async class loads into the sync one, with the same result.
        with AgentSandbox.load(
            blob, KEY, {"crash": _never, "add": _never, "nap": _never}
        ) as replayed_sync:
            assert replayed_sync.run("return globalThis.out") == out
        assert proc.starts == starts

    def test_tool_process_tools_work_with_tool_timeout_budgets_and_catalogs(
        self, tp: Any
    ) -> None:
        from pydeno import Runtime, ToolBridge

        proc = tp()
        with AgentSandbox({"add": proc.tool(T.add)}, tool_timeout=30) as sb:
            assert sb.run("return await add(1, 2)") == 3
        bridge = ToolBridge(
            {"add": proc.tool(T.add), "crash": proc.tool(T.segfault)}, max_calls=2
        )
        with Runtime() as rt:
            bridge.attach(rt)

            async def go() -> list[Any]:
                return [
                    await rt.eval_async("tools.add(1, 2)"),
                    await rt.eval_async(_guest("tools.crash()")),
                    await rt.eval_async(_guest("tools.add(1, 2)")),
                ]

            first, crashed, over_budget = asyncio.run(go())
        assert first == 3
        assert crashed.startswith("ToolProcessDied:")
        assert over_budget.startswith("ToolBudgetError:")
        assert bridge.calls_made == 2


# ---------------------------------------------------------------------------
# tools that stay in the parent
# ---------------------------------------------------------------------------


class TestInParentToolsUntouched:
    def test_plain_tools_still_run_in_the_parent_and_stay_synchronous(
        self, tp: Any
    ) -> None:
        seen: dict[str, Any] = {}

        def local(x: int) -> int:
            seen["thread"] = threading.current_thread().name
            seen["pid"] = os.getpid()
            return x + 1

        with IsolatedRuntime() as rt:
            rt.bind_function("local", local)
            assert rt.eval("local(1)") == 2  # no await: not a Promise
        assert seen["pid"] == os.getpid()

    def test_a_tool_process_and_a_plain_tool_can_share_a_runtime(self, tp: Any) -> None:
        with IsolatedRuntime() as rt:
            rt.bind_function("local", lambda x: x + 1)
            _bind(rt, tp(), add="add")
            assert _run(rt, "(async () => [local(1), await add(1, 2)])()") == [2, 3]

    def test_the_runtimes_take_no_new_option_and_import_no_tool_machinery(self) -> None:
        script = (
            "import sys; import pydeno; from pydeno import IsolatedRuntime;"
            "import inspect;"
            "assert not any('tool_process' in p or 'toolproc' in p for p in "
            "inspect.signature(IsolatedRuntime).parameters);"
            "assert 'pydeno._toolproc' not in sys.modules and 'pydeno._toolhost' not in sys.modules"
        )
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
        assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------------
# optional confinement
# ---------------------------------------------------------------------------


class TestSandbox:
    @pytest.mark.full_sandbox
    def test_a_confined_tool_host_has_the_workers_layers_and_no_filesystem(
        self, tp: Any
    ) -> None:
        proc = tp(sandbox="require")
        with IsolatedRuntime() as rt:
            _bind(rt, proc, add="add", aadd="aadd", read="fileread", boom="boom")
            proc.start()
            assert not _sandbox.missing_layers(proc.sandbox)
            assert _run(rt, "add(2, 3)") == 5
            assert _run(rt, "aadd(2, 3)") == 5
            assert _run(rt, "Promise.all([add(1, 1), aadd(2, 2)])") == [2, 4]
            out = _run(rt, _guest("read('/etc/passwd')"))
            assert out == "PermissionError:host function failed"
            assert _run(rt, _guest("boom('x')")) == "ValueError:host function failed"
            assert _run(rt, "add(3, 4)") == 7

    @pytest.mark.full_sandbox
    def test_a_confined_tool_host_that_crashes_is_replaced_by_a_confined_one(
        self, tp: Any
    ) -> None:
        proc = tp(sandbox="require")
        with IsolatedRuntime() as rt:
            _bind(rt, proc, crash="segfault", add="add")
            assert _run(rt, _guest("crash()")).startswith("ToolProcessDied:")
            assert _run(rt, "add(1, 2)") == 3
            assert not _sandbox.missing_layers(proc.sandbox)

    @pytest.mark.full_sandbox
    def test_a_confined_tool_host_still_obeys_its_limits(self, tp: Any) -> None:
        proc = tp(sandbox="require", call_timeout=0.5, max_memory=150 << 20)
        with IsolatedRuntime() as rt:
            _bind(rt, proc, nap="sleep", eat="eat_shared", add="add")
            assert _run(rt, _guest("nap(30)")) == TIMED_OUT
            assert _run(rt, _guest("eat(600)")).startswith("ToolProcessDied:")
            assert _run(rt, "add(1, 2)") == 3

    @pytest.mark.full_sandbox
    def test_with_sandbox_every_tool_is_registered_before_the_first_call(
        self, tp: Any
    ) -> None:
        proc = tp(sandbox="require")
        proc.tool(T.add)
        proc.start()
        proc.tool(T.add)  # already registered: fine
        with pytest.raises(
            RuntimeError, match="register every tool before the first call"
        ):
            proc.tool(T.count)

    def test_require_refuses_where_a_layer_is_missing_and_says_which(
        self, tp: Any
    ) -> None:
        proc = tp(sandbox="require")
        proc.tool(T.add)
        try:
            proc.start()
        except ToolProcessStartError as exc:
            # A kernel (or container profile) without a layer: the refusal names what is missing.
            assert "sandbox is required" in str(exc)
            assert _EXPECTED not in (None, "landlock+seccomp", "seatbelt")
        else:
            assert not _sandbox.missing_layers(proc.sandbox)

    def test_auto_starts_and_reports_what_was_applied(self, tp: Any) -> None:
        proc = tp(sandbox="auto")
        handler = proc.tool(T.add)
        proc.start()
        assert proc.sandbox != ""

        async def go() -> int:
            return await handler(1, 2)

        assert asyncio.run(go()) == 3

    def test_without_sandbox_the_tool_host_is_not_confined_and_says_so(
        self, tp: Any, tmp_path: Any
    ) -> None:
        target = tmp_path / "readable.txt"
        target.write_text("visible")
        proc = tp()
        assert proc.sandbox == "none"
        with IsolatedRuntime() as rt:
            _bind(rt, proc, read="fileread")
            assert _run(rt, f"read({str(target)!r})") == "visible"
        assert proc.sandbox == "none"
