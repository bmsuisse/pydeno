"""Round-3 review of the front door (PR #57): per-session threads are always reclaimed and bounded.

F1: a session dropped without close() gives its tool thread (and its share of the cap) back.
F2: tool threads are capped per pool (host-configurable), the refusal is a typed `PydenoError`
    for the host and a generic failure for the guest, and a fork()ed child starts from zero.
F3: `resume_auto` refused for want of a thread answers the call; the session is not wedged.
F4: the self-close guard compares ids from one namespace.
"""

from __future__ import annotations

import gc
import os
import threading
import time

import pytest

import pydeno
from pydeno import (
    AsyncPydeno,
    Pydeno,
    PydenoComplete,
    PydenoError,
    PydenoTimeoutError,
    ToolThreadLimitError,
)
from pydeno import _agent

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"

_CATCH = "try { await f() } catch (e) { return [e.name, e.message] }"


def _named(prefix: str) -> int:
    return sum(t.name.startswith(prefix) for t in threading.enumerate())


def _settle(predicate, timeout: float = 15.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gc.collect()
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# F1
# ---------------------------------------------------------------------------


async def test_dropped_async_sessions_give_their_threads_back() -> None:
    before = _named("pydeno-front-tool")
    process_before = _agent._PROCESS_THREADS.count  # noqa: SLF001
    async with AsyncPydeno(sandbox=MODE, min_processes=4) as pool:
        for _ in range(600):
            session = pool.checkout()
            await session.__aenter__()
            assert (
                await session.feed_run("await f()", external_lookup={"f": lambda: 1})
                == 1
            )
            del session  # dropped without close()
        assert _settle(lambda: _named("pydeno-front-tool") <= before)
        assert _settle(lambda: _agent._PROCESS_THREADS.count <= process_before)  # noqa: SLF001
        # A later tenant's plain external still runs.
        async with pool.checkout() as later:
            assert (
                await later.feed_run("await f()", external_lookup={"f": lambda: 7}) == 7
            )


def test_dropped_sync_sessions_give_their_threads_back() -> None:
    before = _named("pydeno-agent-tool")
    with Pydeno(sandbox=MODE, min_processes=4, max_tool_threads=8) as pool:
        for _ in range(30):  # more than the pool's cap allows at once
            session = pool.checkout()
            session.__enter__()
            assert session.feed_run("await f()", external_lookup={"f": lambda: 1}) == 1
            del session
            gc.collect()
        assert _settle(lambda: _named("pydeno-agent-tool") <= before)
        with pool.checkout() as later:
            assert later.feed_run("await f()", external_lookup={"f": lambda: 7}) == 7


# ---------------------------------------------------------------------------
# F2
# ---------------------------------------------------------------------------


def test_the_cap_is_per_pool_typed_for_the_host_and_generic_for_the_guest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert issubclass(ToolThreadLimitError, PydenoError)
    assert pydeno.ToolThreadLimitError is ToolThreadLimitError
    gate = threading.Event()
    try:
        with (
            Pydeno(
                sandbox=MODE,
                min_processes=1,
                max_tool_threads=2,
                limits={"max_host_wait_secs": 0.3},
            ) as greedy,
            Pydeno(sandbox=MODE, min_processes=1, max_tool_threads=2) as neighbour,
        ):
            # A wedged tool keeps its session's tool thread; the next session needs two threads
            # (its loop and its tool thread), and the pool has one left.
            with greedy.checkout() as s:
                with pytest.raises(PydenoTimeoutError):
                    s.feed_run("await f()", external_lookup={"f": gate.wait})
            with greedy.checkout() as s:
                # The guest sees exactly what a redacted RuntimeError from a tool looks like.
                assert s.feed_run(_CATCH, external_lookup={"f": lambda: 1}) == [
                    "RuntimeError",
                    "host function failed",
                ]
                with pytest.raises(ToolThreadLimitError, match="this pool"):
                    s.feed_run("await f()", external_lookup={"f": lambda: 1})
                assert s.feed_run("1 + 1") == 2  # the session survives
            assert "refused" in caplog.text
            # The neighbour pool is untouched.
            with neighbour.checkout() as s:
                assert s.feed_run("await f()", external_lookup={"f": lambda: 5}) == 5

        def boom() -> None:
            raise RuntimeError("secret detail")

        with Pydeno(sandbox=MODE, min_processes=1) as pool, pool.checkout() as s:
            assert s.feed_run(_CATCH, external_lookup={"f": boom}) == [
                "RuntimeError",
                "host function failed",
            ]
    finally:
        gate.set()


async def test_the_async_cap_is_per_pool() -> None:
    gate = threading.Event()
    try:
        async with AsyncPydeno(
            sandbox=MODE,
            min_processes=1,
            max_tool_threads=1,
            limits={"max_host_wait_secs": 0.3},
        ) as pool:
            async with pool.checkout() as s:
                with pytest.raises(PydenoTimeoutError):
                    await s.feed_run("await f()", external_lookup={"f": gate.wait})
            async with pool.checkout() as s:
                out = await s.feed_run(_CATCH, external_lookup={"f": lambda: 1})
                assert out == ["RuntimeError", "host function failed"]
                with pytest.raises(ToolThreadLimitError):
                    await s.feed_run("await f()", external_lookup={"f": lambda: 1})
    finally:
        gate.set()


def test_max_tool_threads_is_validated() -> None:
    with pytest.raises(ValueError, match="max_tool_threads"):
        Pydeno(max_tool_threads=0)


@pytest.mark.filterwarnings(
    "ignore::DeprecationWarning"
)  # fork() in a threaded process
def test_a_forked_child_starts_from_zero() -> None:
    budget = _agent._PROCESS_THREADS  # noqa: SLF001
    saved = budget.count
    budget.count = budget.limit  # as if the parent were saturated
    try:
        pid = os.fork()
        if pid == 0:  # the child: none of the parent's threads exist here
            ok = budget.count == 0 and budget.lock.acquire(timeout=1)
            os._exit(0 if ok else 1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        budget.count = saved


# ---------------------------------------------------------------------------
# F3
# ---------------------------------------------------------------------------


def test_resume_auto_refused_for_want_of_a_thread_does_not_wedge_the_session() -> None:
    gate = threading.Event()
    try:
        with Pydeno(
            sandbox=MODE,
            min_processes=1,
            max_tool_threads=2,
            limits={"max_host_wait_secs": 0.3},
        ) as pool:
            for _ in range(2):  # each leaves its tool thread wedged: the pool's two
                with pool.checkout() as s:
                    snap = s.feed_start("await f()", external_lookup={"f": gate.wait})
                    with pytest.raises(PydenoTimeoutError):
                        snap.resume_auto()
            with pool.checkout() as s:
                snap = s.feed_start(_CATCH, external_lookup={"f": lambda: 1})
                done = snap.resume_auto()
                assert isinstance(done, PydenoComplete)
                assert done.output == ["RuntimeError", "host function failed"]
                snap = s.feed_start("await f()", external_lookup={"f": lambda: 1})
                with pytest.raises(ToolThreadLimitError):
                    snap.resume_auto()
                assert s.feed_run("3") == 3  # not left paused
    finally:
        gate.set()


# ---------------------------------------------------------------------------
# F4
# ---------------------------------------------------------------------------


async def test_session_ids_come_from_one_namespace() -> None:
    async_ids, agent_ids = set(), set()
    async with AsyncPydeno(sandbox=MODE, min_processes=2) as apool:
        with Pydeno(sandbox=MODE, min_processes=2) as spool:
            for _ in range(5):
                async with apool.checkout() as a:
                    async_ids.add(a._sid)  # noqa: SLF001
                    agent_ids.add(a._agent._core.session_id)  # noqa: SLF001
                with spool.checkout() as s:
                    agent_ids.add(s._agent._core.session_id)  # noqa: SLF001
            assert not async_ids & agent_ids

            # An async external may close an unrelated session, whatever its id.
            async with apool.checkout() as a:
                victim = spool.checkout()
                victim.__enter__()

                def close_other() -> str:
                    victim.close()
                    return "closed"

                assert await a.feed_run(
                    "await c()", external_lookup={"c": close_other}
                ) == ("closed")
                with pytest.raises(RuntimeError, match="closed"):
                    victim.feed_run("1")
