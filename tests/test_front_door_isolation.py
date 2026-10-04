"""Round-2 review of the front door (PR #57): no tool execution resource is shared across sessions.

T1: sessions whose tools are wedged cannot starve another session's tools.
T2: thread-local state never crosses sessions; each external call gets a fresh context copy.
T3: a blocking async tool in one session does not delay another session's tools.
And: a session cannot be closed by its own external; pools and sessions leave no threads behind.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time

import pytest

from pydeno import AgentSandbox, AsyncPydeno, Pydeno, PydenoTimeoutError

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"


def _tool_threads() -> int:
    return sum(
        t.name.startswith(
            ("pydeno-agent-tool", "pydeno-front-tool", "pydeno-agent-loop")
        )
        for t in threading.enumerate()
    )


def _settle(predicate, timeout: float = 10.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# T1
# ---------------------------------------------------------------------------


def test_wedged_tools_in_64_sessions_do_not_starve_a_65th() -> None:
    gate = threading.Event()

    def hang() -> int:
        gate.wait(60)
        return 1

    outcomes: list[str] = []
    try:
        with Pydeno(
            sandbox=MODE, min_processes=4, limits={"max_host_wait_secs": 1.0}
        ) as pool:

            def victim() -> None:
                try:
                    with pool.checkout() as s:
                        s.feed_run("await hang()", external_lookup={"hang": hang})
                    outcomes.append("ok")
                except PydenoTimeoutError:
                    outcomes.append("timeout")

            threads = [threading.Thread(target=victim) for _ in range(64)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            assert outcomes == ["timeout"] * 64
            with pool.checkout() as s:
                start = time.monotonic()
                assert (
                    s.feed_run("await quick()", external_lookup={"quick": lambda: 42})
                    == 42
                )
                assert time.monotonic() - start < 1.0
    finally:
        gate.set()


def test_a_session_leaves_at_most_its_own_threads_behind() -> None:
    gate = threading.Event()
    before = _tool_threads()
    try:
        with Pydeno(
            sandbox=MODE, min_processes=1, limits={"max_host_wait_secs": 0.3}
        ) as pool:
            with pool.checkout() as s:
                with pytest.raises(PydenoTimeoutError):
                    s.feed_run("await hang()", external_lookup={"hang": gate.wait})
            # One wedged tool thread, plus its session's loop thread, at most.
            assert _tool_threads() - before <= 2
    finally:
        gate.set()
    assert _settle(lambda: _tool_threads() <= before)


# ---------------------------------------------------------------------------
# T2
# ---------------------------------------------------------------------------

_local = threading.local()


def _set_tenant(name: str) -> str:
    _local.tenant = name
    return threading.current_thread().name


def _get_tenant() -> list:
    return [getattr(_local, "tenant", None), threading.current_thread().name]


_FIVE_SETS = "[" + ", ".join("await s('A')" for _ in range(5)) + "]"
_FIVE_GETS = "[" + ", ".join("await g()" for _ in range(5)) + "]"


def test_thread_locals_never_cross_sessions_sync() -> None:
    with Pydeno(sandbox=MODE, min_processes=2) as pool:
        for _ in range(3):
            with pool.checkout() as a:
                a_threads = set(
                    a.feed_run(_FIVE_SETS, external_lookup={"s": _set_tenant})
                )
            with pool.checkout() as b:
                got = b.feed_run(_FIVE_GETS, external_lookup={"g": _get_tenant})
            assert [seen for seen, _ in got] == [None] * 5
            assert not a_threads & {name for _, name in got}


def test_no_thread_ever_serves_two_sessions() -> None:
    """More sessions than any shared pool could have threads: each still gets threads no other
    session ever used (so no thread-local can travel between them)."""
    seen: dict[str, int] = {}
    with Pydeno(sandbox=MODE, min_processes=2) as pool:
        for i in range(70):
            with pool.checkout() as session:
                name = session.feed_run(
                    "await who()",
                    external_lookup={"who": lambda: threading.current_thread().name},
                )
            assert name not in seen, (
                f"session {i} reused session {seen.get(name)}'s thread"
            )
            seen[name] = i


async def test_thread_locals_never_cross_sessions_async() -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=2) as pool:
        for _ in range(3):
            async with pool.checkout() as a:
                a_threads = set(
                    await a.feed_run(_FIVE_SETS, external_lookup={"s": _set_tenant})
                )
            async with pool.checkout() as b:
                got = await b.feed_run(_FIVE_GETS, external_lookup={"g": _get_tenant})
            assert [seen for seen, _ in got] == [None] * 5
            assert not a_threads & {name for _, name in got}


_cv: contextvars.ContextVar[str] = contextvars.ContextVar("cv", default="unset")


def _cv_set(value: str) -> str:
    _cv.set(value)
    return _cv.get()


def _cv_get() -> str:
    return _cv.get()


def test_each_external_call_gets_a_fresh_context_sync() -> None:
    _cv.set("caller")
    lookup = {"s": _cv_set, "g": _cv_get}
    with Pydeno(sandbox=MODE, min_processes=1) as pool:
        with pool.checkout() as session:
            assert session.feed_run(
                "[await s('X'), await g()]", external_lookup=lookup
            ) == [
                "X",
                "caller",
            ]
            snap = session.feed_start(
                "await s('Y')\nreturn await g()", external_lookup=lookup
            )
            assert snap.resume_auto().resume_auto().output == "caller"
    assert _cv.get() == "caller"


async def test_each_external_call_gets_a_fresh_context_async() -> None:
    _cv.set("caller")
    lookup = {"s": _cv_set, "g": _cv_get}
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            out = await session.feed_run(
                "[await s('X'), await g()]", external_lookup=lookup
            )
            assert out == ["X", "caller"]


# ---------------------------------------------------------------------------
# T3
# ---------------------------------------------------------------------------


def test_a_blocking_async_tool_does_not_delay_another_session() -> None:
    async def bad() -> int:
        time.sleep(2.0)  # blocks its loop: the session's own one
        return 1

    with (
        AgentSandbox({"bad": bad}, sandbox=MODE) as a,
        AgentSandbox({"quick": lambda: 1}, sandbox=MODE) as b,
    ):
        a.run("return 0")
        b.run("return 0")
        results: dict[str, object] = {}

        def run_a() -> None:
            results["a"] = a.run("return await bad()")

        thread = threading.Thread(target=run_a)
        thread.start()
        time.sleep(0.2)
        start = time.monotonic()
        assert b.run("return await quick()") == 1
        assert time.monotonic() - start < 0.5
        thread.join(10)
        assert results == {"a": 1}


# ---------------------------------------------------------------------------
# close from a tool, and leaks
# ---------------------------------------------------------------------------


def test_an_external_cannot_close_its_own_session() -> None:
    with Pydeno(sandbox=MODE, min_processes=1) as pool:
        with pool.checkout() as session:
            holder = [session]

            def closer() -> str:
                try:
                    holder[0].close()
                except RuntimeError as exc:
                    return str(exc)
                return "closed"

            out = session.feed_run("await c()", external_lookup={"c": closer})
            assert "cannot close" in out
            assert session.feed_run("1 + 1") == 2  # the worker was not killed


def test_200_cycles_leave_no_threads() -> None:
    before = set(threading.enumerate())
    for i in range(200):
        with Pydeno(sandbox=MODE, min_processes=1) as pool:
            with pool.checkout() as session:
                if i % 20 == 0:
                    session.feed_run("await f()", external_lookup={"f": lambda: 1})
                else:
                    session.feed_run("1")
        if i % 50 == 49:
            time.sleep(0.05)

    def no_extra() -> bool:
        extra = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        return not [t for t in extra if t.name.startswith("pydeno")]

    assert _settle(no_extra), [t.name for t in threading.enumerate() if t not in before]
