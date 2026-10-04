"""`classify_error` against REAL exceptions.

Every kind in the table is provoked from a real runtime, a real `ToolBridge`/`AgentSandbox`, or (for
what a healthy worker never does: die, grow threads, speak nonsense) a fake worker that speaks the
wire protocol and misbehaves on purpose. Nothing here assumes what an exception looks like: if a
message is reworded or a class changes, the case fails instead of silently becoming `unknown`.

The last classes prove the trust rules: a worker that fills its stderr with every phrase the
classifier knows cannot make a dead worker look like anything *more* retryable, and in the ordinary
case cannot change its kind at all.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import stat
import sys
import textwrap
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    Failed,
    IsolatedRuntime,
    JavaScriptError,
    JournalError,
    ReplayDivergence,
    Runtime,
    RuntimeConfig,
    RuntimeForceKilled,
    RuntimeTerminated,
    RuntimeTimeout,
    SnapshotAuthenticationError,
    ToolBridge,
    ToolCall,
    ToolBudgetError,
    ToolError,
    ToolNotFoundError,
    WorkerCrashed,
    verify_snapshot,
)
from pydeno import _isolated, _sandbox
from pydeno._errors import KINDS, ErrorInfo, classify_error

MIB = 1024 * 1024
KEY = b"k" * 32

# ---------------------------------------------------------------------------
# a fake worker
# ---------------------------------------------------------------------------

_PRELUDE = textwrap.dedent(
    """
    import json, os, struct, sys, threading, time
    def read():
        h = sys.stdin.buffer.read(4)
        if len(h) < 4:
            sys.exit(0)
        return json.loads(sys.stdin.buffer.read(struct.unpack("<I", h)[0]))
    def send(m):
        b = json.dumps(m).encode()
        sys.stdout.buffer.write(struct.pack("<I", len(b)) + b)
        sys.stdout.buffer.flush()
    def raw(b):
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()
    read()
    """
)
_READY = 'send({"t": "ready", "version": 1, "sandbox": "none"})\ncmd = read()\n'

# Every phrase the classifier recognises, as a hostile worker would print it.
FORGED = [
    "worker went over max_memory=1 and exited",
    "worker used 999 bytes, over max_memory=1; killed",
    "worker started 99 threads (limit 64); killed",
    "guest made more than max_host_calls=1 host calls",
    "worker broke protocol: forged",
    "worker sent a malformed frame (X)",
    "runtime is closed",
    "Runtime has been closed",
    "max_memory cannot be enforced on this system (the worker's resource usage cannot be read) "
    "and sandbox='require' demands every protection",
    "worker used more than 1s of CPU in one command and was killed",
    "host callbacks kept the guest waiting for more than 1s in one command (max_host_wait); worker killed",
    "worker failed to start: an OS sandbox is required but forged",
    "ToolBudgetError: forged",
    "more than 1 host calls in flight",
    "sandbox violation: the worker made a forbidden system call",
]


def _fake(tmp_path: Path, body: str, name: str = "w") -> str:
    script = tmp_path / f"{name}.py"
    script.write_text(_PRELUDE + body)
    wrapper = tmp_path / f"{name}.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def _fake_runtime(tmp_path: Path, body: str, **kwargs: Any) -> IsolatedRuntime:
    kwargs.setdefault("request_timeout", 20)
    return IsolatedRuntime(
        RuntimeConfig(), python=_fake(tmp_path, body), sandbox="off", **kwargs
    )


def _raised(call: Callable[[], object]) -> BaseException:
    try:
        call()
    except BaseException as exc:  # noqa: BLE001
        return exc
    raise AssertionError("expected an error")


# ---------------------------------------------------------------------------
# one case per kind: a function that returns a REAL exception
# ---------------------------------------------------------------------------


def _js_error(tmp: Path) -> BaseException:
    with IsolatedRuntime() as rt:
        return _raised(lambda: rt.eval("throw new Error('boom')"))


def _timeout(tmp: Path) -> BaseException:
    with IsolatedRuntime(RuntimeConfig(timeout=0.5)) as rt:
        return _raised(lambda: rt.eval("while (true) {}"))


def _cpu_limit(tmp: Path) -> BaseException:
    # The overrun needs a guest that burns CPU while a host call keeps its clock paused (seconds
    # of wall time, covered by test_isolated_review_findings). Here: the message the supervisor
    # builds, guarded against drift by `test_the_cpu_message_is_still_what_the_supervisor_raises`.
    return RuntimeTimeout(
        "worker used more than 4s of CPU in one command and was killed"
    )


def _memory_limit(tmp: Path) -> BaseException:
    body = _READY + "big = b'x' * (400 << 20)\ntime.sleep(30)\n"
    rt = _fake_runtime(tmp, body, max_memory=150 * MIB)
    return _raised(lambda: rt.eval("1"))


def _thread_limit(tmp: Path) -> BaseException:
    body = (
        _READY
        + "for _ in range(120):\n    threading.Thread(target=time.sleep, args=(30,), daemon=True).start()\n"
        + "time.sleep(30)\n"
    )
    rt = _fake_runtime(tmp, body)
    return _raised(lambda: rt.eval("1"))


def _worker_crashed(tmp: Path) -> BaseException:
    rt = _fake_runtime(tmp, _READY + "os._exit(3)\n")
    return _raised(lambda: rt.eval("1"))


def _sandbox_violation(tmp: Path) -> BaseException:
    # The kernel's seccomp kill rule ends a worker with SIGSYS (end to end on Linux in
    # test_isolated_runtime.py); the host only ever sees the signal, so raising it here takes the
    # same path on every POSIX platform.
    rt = _fake_runtime(
        tmp, _READY + "import signal\nos.kill(os.getpid(), signal.SIGSYS)\n"
    )
    return _raised(lambda: rt.eval("1"))


def _terminated(tmp: Path) -> BaseException:
    with Runtime() as rt:
        handle = rt.termination_handle()
        timer = threading.Timer(0.3, handle.terminate)
        timer.start()
        try:
            return _raised(lambda: rt.eval("while (true) {}"))
        finally:
            timer.cancel()


def _force_killed(tmp: Path) -> BaseException:
    # Needs a runtime wedged in a host callback for the whole grace period; the class itself is the
    # contract here and it is a real, constructible pydeno error.
    return RuntimeForceKilled("runtime never acknowledged termination")


def _host_wait(tmp: Path) -> BaseException:
    async def slow() -> None:
        await asyncio.sleep(60)

    async def go() -> BaseException:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.0, max_host_wait=1.5)
        rt.bind_function("slow", slow)
        try:
            return await _araised(lambda: rt.eval_async("slow()"))
        finally:
            rt.close()

    return asyncio.run(go())


async def _araised(call: Callable[[], Any]) -> BaseException:
    try:
        await call()
    except BaseException as exc:  # noqa: BLE001
        return exc
    raise AssertionError("expected an error")


def _host_call_budget(tmp: Path) -> BaseException:
    with IsolatedRuntime(max_host_calls=2) as rt:
        rt.bind_function("f", lambda: 1)
        return _raised(lambda: rt.eval("for (let i = 0; i < 10; i++) f(); 1"))


def _inflight_limit(tmp: Path) -> BaseException:
    async def slow() -> None:
        await asyncio.sleep(0.5)

    async def go() -> BaseException:
        with IsolatedRuntime(max_inflight_host_calls=1) as rt:
            rt.bind_function("slow", slow)
            return await _araised(
                lambda: rt.eval_async("Promise.all([slow(), slow()])")
            )

    return asyncio.run(go())


def _tool_errors(max_calls: int | None = 1) -> ToolBridge:
    def raise_(exc: Exception) -> Callable[[], None]:
        def f() -> None:
            raise exc

        return f

    return ToolBridge(
        {
            "a": lambda: 1,
            "missing": raise_(ToolNotFoundError("no such key")),
            "bad": raise_(ToolError("nope")),
        },
        max_calls=max_calls,
        namespace="t",
    )


def _tool_via_isolated(code: str, max_calls: int | None = 1) -> BaseException:
    with IsolatedRuntime(redact_host_errors=False) as rt:
        _tool_errors(max_calls).attach(rt)
        return _raised(lambda: rt.eval(code))


def _tool_budget(tmp: Path) -> BaseException:
    return _tool_via_isolated("t.a(); t.a()")


def _tool_not_found(tmp: Path) -> BaseException:
    return _tool_via_isolated("t.missing()")


def _tool_failed(tmp: Path) -> BaseException:
    return _tool_via_isolated("t.bad()")


def _max_pause(tmp: Path) -> BaseException:
    import time

    s = AgentSandbox({"add": lambda a, b: a + b}, max_pause=0.5)
    try:
        step = s.start("return await add(1, 2)")
        deadline = time.monotonic() + 10
        while not s._core.rt.is_closed() and time.monotonic() < deadline:  # noqa: SLF001
            time.sleep(0.05)
        after = s.resume(step, 3)
        assert isinstance(after, Failed)
        return after.error
    finally:
        s.close()


def _protocol_violation(tmp: Path) -> BaseException:
    rt = _fake_runtime(
        tmp, _READY + 'raw(struct.pack("<I", 9) + b"{not json")\ntime.sleep(30)\n'
    )
    return _raised(lambda: rt.eval("1"))


def _sandbox_unavailable(tmp: Path) -> BaseException:
    body = (
        'send({"t": "error", "msg": "an OS sandbox is required but [\'landlock\'] could not be applied"})\n'
        "time.sleep(30)\n"
    )
    return _raised(lambda: _fake_runtime(tmp, body))


def _limits_unmeasurable(tmp: Path) -> BaseException:
    original = _sandbox.rss_bytes
    _sandbox.rss_bytes = lambda pid: None  # type: ignore[assignment]
    try:
        return _raised(lambda: IsolatedRuntime(sandbox="require", max_memory=512 * MIB))
    finally:
        _sandbox.rss_bytes = original  # type: ignore[assignment]


def _closed(tmp: Path) -> BaseException:
    rt = IsolatedRuntime()
    rt.close()
    return _raised(lambda: rt.eval("1"))


def _journal_invalid(tmp: Path) -> BaseException:
    return _raised(lambda: AgentSandbox.load(b"not a journal", KEY, {}))


def _replay_divergence(tmp: Path) -> BaseException:
    # A divergence needs a journal that is authentic but disagrees with what the guest does
    # (covered end to end in test_agent_sandbox.py); the class is what a caller receives.
    return ReplayDivergence("replay diverged at record 3")


def _snapshot_invalid(tmp: Path) -> BaseException:
    return _raised(lambda: verify_snapshot(b"definitely not signed", KEY))


def _invalid_input(tmp: Path) -> BaseException:
    return _raised(lambda: IsolatedRuntime(max_memory=-1))


def _cancelled(tmp: Path) -> BaseException:
    async def go() -> BaseException:
        task = asyncio.ensure_future(asyncio.sleep(30))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as exc:
            return exc
        raise AssertionError("not cancelled")

    return asyncio.run(go())


def _unknown(tmp: Path) -> BaseException:
    return KeyError("something pydeno never raises")


CASES: dict[str, Callable[[Path], BaseException]] = {
    "js_error": _js_error,
    "timeout": _timeout,
    "cpu_limit": _cpu_limit,
    "memory_limit": _memory_limit,
    "thread_limit": _thread_limit,
    "worker_crashed": _worker_crashed,
    "sandbox_violation": _sandbox_violation,
    "terminated": _terminated,
    "force_killed": _force_killed,
    "host_wait": _host_wait,
    "max_pause": _max_pause,
    "host_call_budget": _host_call_budget,
    "inflight_limit": _inflight_limit,
    "tool_budget": _tool_budget,
    "tool_not_found": _tool_not_found,
    "tool_failed": _tool_failed,
    "protocol_violation": _protocol_violation,
    "sandbox_unavailable": _sandbox_unavailable,
    "limits_unmeasurable": _limits_unmeasurable,
    "closed": _closed,
    "journal_invalid": _journal_invalid,
    "replay_divergence": _replay_divergence,
    "snapshot_invalid": _snapshot_invalid,
    "invalid_input": _invalid_input,
    "cancelled": _cancelled,
    "unknown": _unknown,
}


def test_every_kind_in_the_table_has_a_case_and_nothing_else_does() -> None:
    assert set(CASES) == set(KINDS)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_a_real_error_gets_its_documented_kind(kind: str, tmp_path: Path) -> None:
    exc = CASES[kind](tmp_path)
    info = classify_error(exc, via_agent=(kind == "max_pause"))
    assert info.kind == kind, f"{type(exc).__name__}: {exc}"
    retryable, larger, summary = KINDS[kind]
    assert (info.retryable, info.retry_with_larger_limits, info.summary) == (
        retryable,
        larger,
        summary,
    )


def test_the_retry_rule_is_one_rule() -> None:
    """Only environmental kinds are retryable; limit overruns are not, but say a larger limit helps."""
    assert {k for k, (r, _, _) in KINDS.items() if r} == {"worker_crashed"}
    for kind in ("timeout", "cpu_limit", "memory_limit"):
        retryable, larger, _ = KINDS[kind]
        assert not retryable and larger
    for kind in ("js_error", "journal_invalid", "snapshot_invalid", "tool_failed"):
        assert KINDS[kind][:2] == (False, False)


def test_the_error_info_is_frozen_and_serialisable() -> None:
    info = classify_error(ValueError("x"))
    assert isinstance(info, ErrorInfo)
    assert info.to_dict()["kind"] == "invalid_input"
    with pytest.raises(AttributeError):
        info.kind = "other"  # type: ignore[misc]


def test_the_cpu_message_is_still_what_the_supervisor_raises() -> None:
    """The CPU cap cannot be provoked cheaply, so pin the host's wording instead."""
    source = inspect.getsource(_isolated)
    assert "worker used more than {pump.cpu_cap:g}s of CPU in one command" in source


# ---------------------------------------------------------------------------
# the paths the tool errors really take
# ---------------------------------------------------------------------------


class TestToolErrorsOnTheRealPaths:
    def test_isolated_runtime_gives_no_name_only_a_message(self) -> None:
        exc = _tool_via_isolated("t.a(); t.a()")
        assert isinstance(exc, JavaScriptError)
        assert getattr(exc, "name", None) is None  # why the message has to be read
        assert classify_error(exc).kind == "tool_budget"

    def test_isolated_runtime_with_redacted_host_errors(self) -> None:
        with (
            IsolatedRuntime() as rt
        ):  # redact_host_errors defaults to True: the text is gone
            _tool_errors().attach(rt)
            exc = _raised(lambda: rt.eval("t.missing()"))
        assert classify_error(exc).kind == "tool_not_found"

    def test_plain_runtime_has_a_name(self) -> None:
        with Runtime() as rt:
            _tool_errors().attach(rt)
            exc = _raised(lambda: rt.eval("t.a(); t.a()"))
        assert exc.name == "ToolBudgetError"  # type: ignore[attr-defined]
        assert classify_error(exc).kind == "tool_budget"

    def test_agent_sandbox_run_reports_it_in_failed(self) -> None:
        def a() -> int:
            return 1

        with AgentSandbox({"a": a}, max_tool_calls=1) as s:
            step = s.start("await a(); return await a()")
            while isinstance(step, ToolCall):
                step = s.resume(step, 1)
            assert isinstance(step, Failed), step
            assert classify_error(step.error, via_agent=True).kind == "tool_budget"

    def test_host_side_exceptions_are_classified_by_type(self) -> None:
        assert classify_error(ToolBudgetError("x")).kind == "tool_budget"
        assert classify_error(ToolNotFoundError("x")).kind == "tool_not_found"
        assert classify_error(ToolError("x")).kind == "tool_failed"


# ---------------------------------------------------------------------------
# closed: exact phrases and types, not "is closed"
# ---------------------------------------------------------------------------


class TestClosed:
    @pytest.mark.parametrize(
        "text",
        [
            "runtime is closed",
            "Runtime has been closed",
            "Function has been closed",
            "Stream has been closed",
            "the session is closed",
            "the session was closed",
        ],
    )
    def test_the_messages_pydeno_really_raises(self, text: str) -> None:
        assert classify_error(RuntimeError(text)).kind == "closed"

    def test_a_real_closed_in_process_runtime(self) -> None:
        rt = Runtime()
        rt.close()
        assert classify_error(_raised(lambda: rt.eval("1"))).kind == "closed"

    def test_a_real_closed_session(self) -> None:
        s = AgentSandbox({})
        s.close()
        assert classify_error(_raised(lambda: s.start("return 1"))).kind == "closed"

    @pytest.mark.parametrize(
        "text", ["file is closed", "socket is closed", "the door is closed"]
    )
    def test_other_peoples_errors_are_unknown(self, text: str) -> None:
        assert classify_error(RuntimeError(text)).kind == "unknown"


# ---------------------------------------------------------------------------
# trust: worker-chosen text must not steer the classification
# ---------------------------------------------------------------------------


class TestAWorkerCannotForgeTheKind:
    @pytest.mark.parametrize("phrase", FORGED, ids=range(len(FORGED)))
    def test_every_phrase_in_stderr_after_a_crash_is_still_worker_crashed(
        self, phrase: str, tmp_path: Path
    ) -> None:
        body = (
            _READY
            + f"sys.stderr.write({phrase!r} + '\\n'); sys.stderr.flush()\nos._exit(3)\n"
        )
        rt = _fake_runtime(tmp_path, body)
        exc = _raised(lambda: rt.eval("1"))
        assert isinstance(exc, WorkerCrashed)
        assert phrase[:20] in str(exc) or "exit code 3" in str(
            exc
        )  # the worker's text is there
        info = classify_error(exc)
        assert (info.kind, info.retryable) == ("worker_crashed", True)

    def test_all_phrases_together_and_a_clean_exit_cannot_make_a_retry_more_likely(
        self, tmp_path: Path
    ) -> None:
        """Exit code 0 leaves the worker's last line right after the host's own prefix, the one
        shape where a whole-message forgery is possible. It can only turn `worker_crashed` into
        a kind that is *not* retryable, never the reverse."""
        for phrase in FORGED:
            body = (
                _READY
                + f"sys.stderr.write({phrase!r} + '\\n'); sys.stderr.flush()\nos._exit(0)\n"
            )
            exc = _raised(lambda: _fake_runtime(tmp_path, body).eval("1"))
            assert isinstance(exc, WorkerCrashed)
            info = classify_error(exc)
            assert info.kind == "worker_crashed" or not info.retryable, (phrase, info)

    @pytest.mark.parametrize("phrase", FORGED, ids=range(len(FORGED)))
    def test_a_worker_reported_error_cannot_choose_a_retryable_kind(
        self, phrase: str, tmp_path: Path
    ) -> None:
        """An error frame's text is the worker's. Whatever it says, the result is not retryable."""
        body = (
            _READY
            + 'send({"t": "error", "id": cmd["id"], "kind": "RuntimeError", "msg": %r})\ntime.sleep(30)\n'
            % phrase
        )
        exc = _raised(lambda: _fake_runtime(tmp_path, body).eval("1"))
        assert not classify_error(exc).retryable

    def test_a_genuine_violation_survives_a_hostile_stderr(
        self, tmp_path: Path
    ) -> None:
        """The violation text is the host's whole message: the worker's last words are dropped."""
        body = (
            _READY
            + "import signal\nsys.stderr.write('runtime is closed\\n'); sys.stderr.flush()\n"
            + "os.kill(os.getpid(), signal.SIGSYS)\n"
        )
        exc = _raised(lambda: _fake_runtime(tmp_path, body).eval("1"))
        assert "runtime is closed" not in str(exc)
        info = classify_error(exc)
        assert (info.kind, info.retryable) == ("sandbox_violation", False)

    def test_a_genuine_memory_kill_survives_a_hostile_stderr(
        self, tmp_path: Path
    ) -> None:
        # The worker exits with the dedicated memory code; the host writes the cause itself.
        body = (
            _READY
            + f"sys.stderr.write('runtime is closed\\n'); os._exit({_sandbox.MEMORY_EXIT_CODE})\n"
        )
        exc = _raised(
            lambda: _fake_runtime(tmp_path, body, max_memory=300 * MIB).eval("1")
        )
        assert classify_error(exc).kind == "memory_limit"


# ---------------------------------------------------------------------------
# the rest of the vocabulary
# ---------------------------------------------------------------------------


class TestEdges:
    def test_asyncio_timeout(self) -> None:
        async def go() -> BaseException:
            return await _araised(lambda: asyncio.wait_for(asyncio.sleep(5), 0.05))

        assert classify_error(asyncio.run(go())).kind == "timeout"

    def test_builtin_timeout(self) -> None:
        assert classify_error(TimeoutError()).kind == "timeout"

    def test_type_and_value_errors_from_the_wire(self) -> None:
        with IsolatedRuntime() as rt:
            exc = _raised(lambda: rt.bind_object("o", {"x": object()}))
        assert isinstance(exc, TypeError)
        assert classify_error(exc).kind == "invalid_input"

    def test_classification_is_total(self) -> None:
        for exc in (
            Exception(),
            BaseException(),
            KeyboardInterrupt(),
            RuntimeError(),
            JavaScriptError("m"),
            OSError(),
        ):
            assert isinstance(classify_error(exc), ErrorInfo)

    def test_a_str_that_raises_does_not_escape(self) -> None:
        class Bad(Exception):
            def __str__(self) -> str:
                raise RuntimeError("no")

        assert classify_error(Bad()).kind == "unknown"

    def test_subclass_order(self) -> None:
        assert classify_error(RuntimeForceKilled("x")).kind == "force_killed"
        assert classify_error(RuntimeTerminated("x")).kind == "terminated"
        assert classify_error(JournalError("x")).kind == "journal_invalid"
        assert (
            classify_error(SnapshotAuthenticationError("x")).kind == "snapshot_invalid"
        )
        assert classify_error(ReplayDivergence("x")).kind == "replay_divergence"

    def test_the_module_is_importable_from_the_package(self) -> None:
        # `pydeno.classify_error` is exported lazily once __init__ lists it; the module path is public now.
        import pydeno._errors as mod

        assert mod.classify_error is classify_error
        assert os.path.exists(mod.__file__ or "")
