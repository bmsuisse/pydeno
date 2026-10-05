"""Regression tests for the independent security review of the front door (PR #57).

D1: the guest's clock is frozen even when the first feed fails before any of it executes.
D2: a pre-installed session and a session restored from its dump have the same global
    environment, so replay never diverges on it.
D3: limits are enforced while an external function runs, not only after it returns.
D4: closing a pool stops its threads.
And: a second thread on a busy session gets a `PydenoError` and disturbs nothing.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from pydeno import (
    AgentSandbox,
    AsyncPydeno,
    Pydeno,
    PydenoCrashedError,
    PydenoError,
    PydenoTimeoutError,
)

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"

# Fails before anything executes: V8's parser runs out of stack (a RangeError, not a SyntaxError).
_DEEP = "(" * 20000 + "1" + ")" * 20000
# Larger than one wire frame: refused on the host side before the worker sees it.
_HUGE = "'" + "x" * (17 * 1024 * 1024) + "'"

_FIRST_FEEDS = {
    "parser-range-error": _DEEP,
    "over-the-frame-cap": _HUGE,
    "syntax-error": "x y",
    "throws-at-top-level": "throw new Error('first')",
    "throws-a-syntax-error": "JSON.parse('{')",
}


@pytest.fixture(scope="module")
def pool():
    with Pydeno(sandbox=MODE) as p:
        yield p


def _assert_frozen(values: list[int], before: int) -> None:
    assert len(set(values)) == 1, f"the clock moved: {values}"
    assert before - 5 <= values[0] <= int(time.time() * 1000) + 5


# ---------------------------------------------------------------------------
# D1
# ---------------------------------------------------------------------------


class TestClockFrozenWhateverTheFirstFeedDoes:
    @pytest.mark.parametrize(
        "first", list(_FIRST_FEEDS.values()), ids=list(_FIRST_FEEDS)
    )
    def test_sync(self, pool: Pydeno, first: str) -> None:
        before = int(time.time() * 1000)
        with pool.checkout() as session:
            with pytest.raises((PydenoError, TypeError)):
                session.feed_run(first)
            a = session.feed_run("Date.now()")
            time.sleep(0.05)
            b = session.feed_run("Date.now()")
            c = session.feed_run("new Date().getTime()")
            _assert_frozen([a, b, c], before)
            # The journal holds the clock the guest really saw.
            if first is _HUGE:  # 17 MiB of code is over the journal's cap: dump says so
                with pytest.raises(PydenoError, match="max_journal_bytes"):
                    session.dump()
                return
            state = session.dump()
        with pool.checkout() as other:
            other.load_session(state)
            assert other.feed_run("Date.now()") == a

    def test_feed_start_as_the_first_call(self, pool: Pydeno) -> None:
        before = int(time.time() * 1000)
        with pool.checkout() as session:
            snap = session.feed_start(
                "const t0 = Date.now()\nawait f()\nreturn [t0, Date.now()]",
                external_lookup={"f": print},
            )
            time.sleep(0.05)
            done = snap.resume(value=None)
            _assert_frozen(done.output, before)

    @pytest.mark.parametrize(
        "first",
        [_DEEP, "throw new Error('first')", "x y"],
        ids=["deep", "throws", "syntax"],
    )
    async def test_async(self, first: str) -> None:
        before = int(time.time() * 1000)
        async with AsyncPydeno(sandbox=MODE, min_processes=1) as p:
            async with p.checkout() as session:
                with pytest.raises(PydenoError):
                    await session.feed_run(first)
                a = await session.feed_run("Date.now()")
                await asyncio.sleep(0.05)
                b = await session.feed_run("Date.now()")
                _assert_frozen([a, b], before)


# ---------------------------------------------------------------------------
# D2
# ---------------------------------------------------------------------------

_GLOBALS = "Object.getOwnPropertyNames(globalThis).sort()"


class TestRestoredSessionsHaveTheSameGlobals:
    def test_sync(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            names = session.feed_run(_GLOBALS)
            # Only the prelude's own (non-enumerable) compile check and settle step, which every
            # session has.
            assert [n for n in names if n.startswith("__pydeno_agent")] == [
                "__pydeno_agent_compiles",
                "__pydeno_agent_settle",
            ]
            session.feed_run('var probe = "__pydeno_agent_clock" in globalThis')
            state = session.dump()
            expected = session.feed_run(f"[{_GLOBALS}, probe]")
        with pool.checkout() as other:
            other.load_session(state)  # replays the enumeration: must not diverge
            assert other.feed_run(f"[{_GLOBALS}, probe]") == expected
        with AgentSandbox.load(
            state,
            pool._key,  # noqa: SLF001
            {"__pydeno_external": print},
            sandbox=MODE,
        ) as plain:
            assert plain.run(f"return [{_GLOBALS}, probe]") == expected

    async def test_async(self) -> None:
        async with AsyncPydeno(sandbox=MODE, min_processes=2) as p:
            async with p.checkout() as session:
                await session.feed_run(f"var g = {_GLOBALS}")
                state = await session.dump()
                expected = await session.feed_run(_GLOBALS)
            async with p.checkout() as other:
                await other.load_session(state)
                assert await other.feed_run(_GLOBALS) == expected


# ---------------------------------------------------------------------------
# D3
# ---------------------------------------------------------------------------


class TestLimitsHoldWhileAnExternalRuns:
    def test_host_wait_ends_a_slow_external(self, pool: Pydeno) -> None:
        def slow() -> int:
            time.sleep(5)
            return 1

        limits = {"max_host_wait_secs": 0.5, "max_feed_duration_secs": 1.0}
        with pool.checkout(limits=limits) as session:
            start = time.monotonic()
            with pytest.raises(PydenoTimeoutError):
                session.feed_run("await slow()", external_lookup={"slow": slow})
            assert time.monotonic() - start < 1.5

    def test_an_external_that_never_returns_releases_the_caller(
        self, pool: Pydeno
    ) -> None:
        never = threading.Event()
        limits = {"max_host_wait_secs": 0.5}
        try:
            with pool.checkout(limits=limits) as session:
                start = time.monotonic()
                with pytest.raises(PydenoTimeoutError):
                    session.feed_run(
                        "await hang()", external_lookup={"hang": never.wait}
                    )
                assert time.monotonic() - start < 1.5
                with pytest.raises(PydenoCrashedError):
                    session.feed_run("1")
        finally:
            never.set()

    def test_the_cpu_cap_stops_a_guest_computing_during_an_external(
        self, pool: Pydeno
    ) -> None:
        def slow(_: int) -> int:
            time.sleep(5)
            return 1

        with pool.checkout(limits={"max_feed_duration_secs": 1.0}) as session:
            start = time.monotonic()
            with pytest.raises(PydenoTimeoutError):
                session.feed_run(
                    "const p = slow(1)\nfor (;;) {}", external_lookup={"slow": slow}
                )
            # The CPU cap is twice the feed limit; the external would have taken 5 s.
            assert time.monotonic() - start < 3.5

    def test_agent_sandbox_run_too(self) -> None:
        def slow() -> int:
            time.sleep(5)
            return 1

        with AgentSandbox({"slow": slow}, sandbox=MODE, max_pause=0.5) as sb:
            start = time.monotonic()
            with pytest.raises(Exception, match="max_host_wait"):
                sb.run("return await slow()")
            assert time.monotonic() - start < 1.5

    def test_a_late_answer_does_not_touch_the_journal(self, pool: Pydeno) -> None:
        release = threading.Event()

        def slow() -> int:
            release.wait(5)
            return 1

        with pool.checkout(limits={"max_host_wait_secs": 0.3}) as session:
            session.feed_run("var kept = 1")
            with pytest.raises(PydenoTimeoutError):
                session.feed_run("await slow()", external_lookup={"slow": slow})
            state = session.dump()
            release.set()
            time.sleep(0.2)  # the external returns now; its answer must be dropped
            assert session.dump() == state
        with pool.checkout() as other:
            other.load_session(state)
            assert other.feed_run("kept") == 1

    def test_resume_auto_is_bounded_too(self, pool: Pydeno) -> None:
        def slow() -> int:
            time.sleep(5)
            return 1

        with pool.checkout(limits={"max_host_wait_secs": 0.5}) as session:
            snap = session.feed_start("await slow()", external_lookup={"slow": slow})
            start = time.monotonic()
            # The wait budget starts when the call is pending, so a loaded machine can have spent it
            # before this line: "timed out" (a subclass) or "already gone" are both bounded.
            with pytest.raises(PydenoCrashedError):
                snap.resume_auto()
            assert time.monotonic() - start < 1.5

    async def test_async_resume_auto_is_bounded_too(self) -> None:
        async def slow() -> int:
            await asyncio.sleep(5)
            return 1

        async with AsyncPydeno(sandbox=MODE, min_processes=1) as p:
            async with p.checkout(limits={"max_host_wait_secs": 0.5}) as session:
                snap = await session.feed_start(
                    "await slow()", external_lookup={"slow": slow}
                )
                start = time.monotonic()
                # See the sync twin: the budget may already be spent on a loaded machine.
                with pytest.raises(PydenoCrashedError):
                    await snap.resume_auto()
                assert time.monotonic() - start < 1.5

    async def test_async_host_wait(self) -> None:
        async def slow() -> int:
            await asyncio.sleep(5)
            return 1

        async with AsyncPydeno(sandbox=MODE, min_processes=1) as p:
            async with p.checkout(limits={"max_host_wait_secs": 0.5}) as session:
                start = time.monotonic()
                with pytest.raises(PydenoTimeoutError):
                    await session.feed_run(
                        "await slow()", external_lookup={"slow": slow}
                    )
                assert time.monotonic() - start < 1.5


# ---------------------------------------------------------------------------
# D4 and the busy session
# ---------------------------------------------------------------------------


def _reapers() -> int:
    return sum(t.name == "pydeno-front-reaper" for t in threading.enumerate())


def test_closing_pools_stops_their_threads() -> None:
    before = _reapers()
    for _ in range(20):
        with Pydeno(sandbox=MODE, min_processes=1) as p:
            with p.checkout() as session:
                session.feed_run("1")
    deadline = time.monotonic() + 5
    while _reapers() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _reapers() == before


def test_a_session_closed_after_its_pool_is_still_reaped() -> None:
    p = Pydeno(sandbox=MODE, min_processes=1)
    session = p.checkout()
    session.__enter__()
    pid = session.worker_pid
    p.close()
    session.close()
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_second_thread_on_a_busy_session_gets_a_pydeno_error(pool: Pydeno) -> None:
    printed: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def wait() -> int:
        entered.set()
        release.wait(5)
        return 1

    with pool.checkout() as session:
        result: list[object] = []

        def first() -> None:
            result.append(
                session.feed_run(
                    "console.log('before')\nawait wait()\nconsole.log('after')\n7",
                    external_lookup={"wait": wait},
                    print_callback=lambda s, t: printed.append(t),
                )
            )

        thread = threading.Thread(target=first)
        thread.start()
        assert entered.wait(5)
        with pytest.raises(PydenoError, match="busy"):
            session.feed_run("1")
        release.set()
        thread.join(10)
        assert result == [7]
        assert printed == ["before\n", "after\n"]  # the refused call dropped nothing


async def test_a_second_task_on_a_busy_session_gets_a_pydeno_error() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait() -> int:
        entered.set()
        await release.wait()
        return 1

    async with AsyncPydeno(sandbox=MODE, min_processes=1) as p:
        async with p.checkout() as session:
            task = asyncio.ensure_future(
                session.feed_run("await wait()\n7", external_lookup={"wait": wait})
            )
            await entered.wait()
            with pytest.raises(PydenoError, match="busy"):
                await session.feed_run("1")
            release.set()
            assert await task == 7
