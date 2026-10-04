"""Round-4 review of the front door (PR #57): refused tool calls are ordinary tool calls.

D1: a call refused for want of a thread is journaled and charged like a failed tool call, on every
    path, so `dump()` / `load_session()` round-trip and `max_suspensions` holds.
D2: refusals are logged once per session, then counted (reported when the session closes).
D3: a pool's `max_tool_threads` is clamped to the process ceiling; caps adding up past it warn.
D5: an async session's console sink runs on the session's own thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import pytest

from pydeno import AsyncPydeno, Pydeno, PydenoTimeoutError
from pydeno import _agent

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"

_SWALLOW = "try { await f() } catch (e) {}\ntry { await f() } catch (e) {}\n'done'"


def _wedge(pool: Pydeno, gate: threading.Event, sessions: int) -> None:
    """Leave `sessions` tool threads wedged in the pool (one each, through resume_auto)."""
    for _ in range(sessions):
        with pool.checkout() as s:
            snap = s.feed_start("await f()", external_lookup={"f": gate.wait})
            with pytest.raises(PydenoTimeoutError):
                snap.resume_auto()


# ---------------------------------------------------------------------------
# D1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "path"), [(2, "loop thread"), (3, "tool thread")], ids=["loop", "tool"]
)
def test_refused_calls_are_journaled_and_charged_sync(cap: int, path: str) -> None:
    gate = threading.Event()
    try:
        with Pydeno(
            sandbox=MODE,
            min_processes=1,
            max_tool_threads=cap,
            limits={"max_host_wait_secs": 0.3, "max_suspensions": 10},
        ) as pool:
            _wedge(
                pool, gate, 2
            )  # two units held: 0 left (loop path) or 1 left (tool path)
            with pool.checkout() as s:
                assert s.feed_run(_SWALLOW, external_lookup={"f": lambda: 1}) == "done"
                assert s._agent.calls_remaining == 8  # noqa: SLF001 - both calls charged
                assert s._agent._core.refused is not None, path  # noqa: SLF001
                state = s.dump()
            with pool.checkout() as other:
                other.load_session(state)  # replays: must not diverge
                assert other._agent.calls_remaining == 8  # noqa: SLF001
                assert other.feed_run("1 + 1") == 2
    finally:
        gate.set()


async def test_refused_calls_are_journaled_and_charged_async() -> None:
    gate = threading.Event()
    try:
        async with AsyncPydeno(
            sandbox=MODE,
            min_processes=1,
            max_tool_threads=1,
            limits={"max_host_wait_secs": 0.3, "max_suspensions": 10},
        ) as pool:
            async with pool.checkout() as s:
                with pytest.raises(PydenoTimeoutError):
                    await s.feed_run("await f()", external_lookup={"f": gate.wait})
            async with pool.checkout() as s:
                assert (
                    await s.feed_run(_SWALLOW, external_lookup={"f": lambda: 1})
                    == "done"
                )
                assert s._agent.calls_remaining == 8  # noqa: SLF001
                state = await s.dump()
            async with pool.checkout() as other:
                await other.load_session(state)
                assert other._agent.calls_remaining == 8  # noqa: SLF001
    finally:
        gate.set()


# ---------------------------------------------------------------------------
# D2
# ---------------------------------------------------------------------------


def test_refusals_are_logged_once_per_session_then_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = threading.Event()
    many = "for (let i = 0; i < 50; i++) { try { await f() } catch (e) {} }\n'done'"
    try:
        with Pydeno(
            sandbox=MODE,
            min_processes=1,
            max_tool_threads=2,
            limits={"max_host_wait_secs": 0.3},
        ) as pool:
            _wedge(pool, gate, 2)
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="pydeno"):
                with pool.checkout() as s:
                    assert s.feed_run(many, external_lookup={"f": lambda: 1}) == "done"
                    during = [r for r in caplog.records if "refused" in r.getMessage()]
                    assert len(during) == 1
                after = [r for r in caplog.records if "refused" in r.getMessage()]
            assert len(after) == 2
            assert "50 tool calls were refused" in after[-1].getMessage()
    finally:
        gate.set()


# ---------------------------------------------------------------------------
# D3
# ---------------------------------------------------------------------------


def test_a_pool_cap_is_clamped_to_the_process_ceiling() -> None:
    with pytest.warns(RuntimeWarning, match="clamped"):
        pool = Pydeno(sandbox=MODE, min_processes=1, max_tool_threads=10**9)
    try:
        assert pool._budget.limit == _agent.MAX_TOOL_THREADS  # noqa: SLF001
    finally:
        pool.close()


def test_caps_adding_up_past_the_ceiling_warn_that_they_are_not_reservations() -> None:
    pools = []
    try:
        with pytest.warns(RuntimeWarning, match="not a reservation"):
            for _ in range(5):  # 5 x 128 > 512
                pools.append(Pydeno(sandbox=MODE, min_processes=1))
    finally:
        for p in pools:
            p.close()


# ---------------------------------------------------------------------------
# D5
# ---------------------------------------------------------------------------


async def test_a_slow_console_sink_holds_up_only_its_own_session() -> None:
    release = threading.Event()

    def slow_sink(stream: str, text: str) -> None:
        release.wait(10)

    async with AsyncPydeno(sandbox=MODE, min_processes=4) as pool:
        # More sessions with a stuck sink than any shared handler pool has threads.
        stuck = []
        tasks = []
        for _ in range(40):
            s = pool.checkout()
            await s.__aenter__()
            stuck.append(s)
            tasks.append(
                asyncio.ensure_future(
                    s.feed_run("console.log('x')", print_callback=slow_sink)
                )
            )
        await asyncio.sleep(0.5)
        try:
            got: list[str] = []
            async with pool.checkout() as b:
                start = time.monotonic()
                await b.feed_run(
                    "console.log('b')", print_callback=lambda s, t: got.append(t)
                )
                assert time.monotonic() - start < 1.0
            assert got == ["b\n"]
            names = {t.name for t in threading.enumerate()}
            assert any(n.startswith("pydeno-front-console") for n in names)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            for s in stuck:
                await s.close()
