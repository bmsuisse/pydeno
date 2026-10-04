"""`SessionPool`: per-owner `AsyncAgentSandbox` sessions over a pluggable async `JournalStore`.

Proved here: leasing and persistence (journals signed and bound to owner, session and counter);
rollback refused; size cap drops the state with a clear error; eviction (idle TTL, LRU cap,
per-owner cap); crash safety (a dead worker is restored from the last stored journal);
per-session serialisation or rejection; cancellation while running and while paused.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time

import pytest

from pydeno import (
    InMemoryJournalStore,
    JournalError,
    JournalStore,
    JournalTooLarge,
    PoolFull,
    SchemaTool,
    SessionBusy,
    SessionPool,
    StaleJournal,
    ToolCall,
)
from pydeno._agent import AgentSandbox

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"


async def add(a: int, b: int) -> int:
    return a + b


TOOLS = {"add": add}


def pool(store: JournalStore | None = None, **kwargs: object) -> SessionPool:
    if store is None:
        store = InMemoryJournalStore()
    return SessionPool(store, KEY, TOOLS, **kwargs)


async def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


class TestLeasing:
    async def test_get_run_release_get_again(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as p:
            sb = await p.get("alice", "s1")
            await sb.run("globalThis.n = await add(1, 2)")
            await p.release("alice", "s1")
            assert await store.get("pydeno:session:alice:s1:counter") == b"1"
            again = await p.get("alice", "s1")
            assert again is sb  # still live: no replay
            assert await again.run("return n") == 3
            await p.release("alice", "s1")
            assert await store.get("pydeno:session:alice:s1:counter") == b"2"

    async def test_another_process_picks_the_session_up(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as first:
            async with first.session("alice", "s1") as sb:
                await sb.run("globalThis.n = await add(20, 22)")
        async with pool(store) as second:  # shares only the store and the key
            async with second.session("alice", "s1") as sb:
                assert await sb.run("return n") == 42
                assert sb.calls_made == 1

    async def test_paused_session_survives_a_release(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                step = await sb.start("return await add(1, 2)")
                assert isinstance(step, ToolCall)
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                assert sb.pending is not None
                assert (await sb.resume(sb.pending, 30)).value == 30

    async def test_journal_is_bound_to_owner_session_and_counter(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 1")
        raw = await store.get("pydeno:session:alice:s1:journal")
        assert raw is not None
        blob = raw[len(b"pydeno-session1\x00") + 8 :]
        # It verifies only under exactly that owner, session and counter.
        await asyncio.to_thread(
            lambda: AgentSandbox.load(
                blob, KEY, TOOLS, associated_data=b"alice:s1:1"
            ).close()
        )
        for wrong in (b"bob:s1:1", b"alice:s2:1", b"alice:s1:2"):
            with pytest.raises(JournalError, match="authentication"):
                AgentSandbox.load(blob, KEY, TOOLS, associated_data=wrong)
        # Moving alice's journal under bob's key does not give bob alice's session.
        await store.set("pydeno:session:bob:s1:journal", raw, ttl=None)
        await store.set("pydeno:session:bob:s1:counter", b"1", ttl=None)
        async with pool(store) as p:
            with pytest.raises(JournalError, match="authentication"):
                await p.get("bob", "s1")

    async def test_ids_with_separators_are_refused(self) -> None:
        async with pool() as p:
            for owner, sid in (("a:b", "c"), ("a", "b:c"), ("", "x"), ("a\n", "x")):
                with pytest.raises(ValueError):
                    await p.get(owner, sid)

    async def test_release_without_lease(self) -> None:
        async with pool() as p:
            sb = await p.get("alice", "s1")
            await p.release("alice", "s1")
            with pytest.raises(RuntimeError, match="not leased"):
                await p.release("alice", "s1")
            assert not sb.is_closed()

    async def test_per_owner_tools(self) -> None:
        def tools_for(owner: str, session_id: str) -> dict:
            def whoami() -> str:
                return owner

            return {"whoami": whoami}

        async with SessionPool(InMemoryJournalStore(), KEY, tools_for) as p:
            async with p.session("alice", "s") as a, p.session("bob", "s") as b:
                assert await a.run("return await whoami()") == "alice"
                assert await b.run("return await whoami()") == "bob"


class TestConcurrency:
    async def test_two_gets_are_serialised(self) -> None:
        async with pool() as p:
            order: list[str] = []

            async def use(tag: str) -> None:
                async with p.session("alice", "s1") as sb:
                    order.append(f"{tag}-in")
                    await sb.run("globalThis.c = (globalThis.c || 0) + 1")
                    await asyncio.sleep(0.05)
                    order.append(f"{tag}-out")

            await asyncio.gather(use("a"), use("b"), use("c"))
            for i in range(0, 6, 2):
                assert order[i].endswith("-in") and order[i + 1].endswith("-out")
                assert order[i][0] == order[i + 1][0]
            async with p.session("alice", "s1") as sb:
                assert await sb.run("return c") == 3

    async def test_busy_session_is_rejected_with_a_typed_error(self) -> None:
        async with pool(acquire_timeout=0) as p:
            await p.get("alice", "s1")
            with pytest.raises(SessionBusy):
                await p.get("alice", "s1")
            with pytest.raises(SessionBusy):
                await p.get("alice", "s1", timeout=0.1)
            await p.release("alice", "s1")
            await p.get("alice", "s1")  # free again
            await p.release("alice", "s1")

    async def test_wait_times_out(self) -> None:
        async with pool(acquire_timeout=0.2) as p:
            await p.get("alice", "s1")
            t = time.monotonic()
            with pytest.raises(SessionBusy, match="leased"):
                await p.get("alice", "s1")
            assert time.monotonic() - t >= 0.15
            assert p.stats()["waiting"] == 0

    async def test_different_sessions_run_concurrently(self) -> None:
        async with pool() as p:
            sessions = await asyncio.gather(*(p.get("o", f"s{i}") for i in range(10)))
            got = await asyncio.gather(
                *(sb.run(f"return await add({i}, 1)") for i, sb in enumerate(sessions))
            )
            assert got == [i + 1 for i in range(10)]
            await asyncio.gather(*(p.release("o", f"s{i}") for i in range(10)))


class TestRollback:
    async def test_an_older_journal_is_refused(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store, max_tool_calls=2) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("await add(1, 1)")
            old = await store.get("pydeno:session:alice:s1:journal")
            async with p.session("alice", "s1") as sb:
                await sb.run("await add(1, 1)")  # budget spent
                assert sb.calls_remaining == 0
        # Put the older journal (one call left in it) back.
        await store.set("pydeno:session:alice:s1:journal", old, ttl=None)
        async with pool(store) as p:
            with pytest.raises(StaleJournal, match="rollback"):
                await p.get("alice", "s1")

    async def test_a_forged_counter_in_the_envelope_fails_the_signature(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("1")
            async with p.session("alice", "s1") as sb:
                await sb.run("2")
        raw = await store.get("pydeno:session:alice:s1:journal")
        assert raw is not None
        head = len(b"pydeno-session1\x00")
        forged = raw[:head] + (99).to_bytes(8, "big") + raw[head + 8 :]
        await store.set("pydeno:session:alice:s1:journal", forged, ttl=None)
        async with pool(store) as p:
            with pytest.raises(JournalError, match="authentication"):
                await p.get("alice", "s1")

    async def test_drop_advances_the_counter(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 1")
            last = await store.get("pydeno:session:alice:s1:journal")
            await p.drop("alice", "s1")
            assert await store.get("pydeno:session:alice:s1:journal") is None
            assert await store.get("pydeno:session:alice:s1:counter") == b"2"
            # The latest journal from before the drop cannot be brought back.
            await store.set("pydeno:session:alice:s1:journal", last, ttl=None)
            with pytest.raises(StaleJournal):
                await p.get("alice", "s1")

    async def test_a_crash_between_journal_and_counter_writes_is_tolerated(
        self,
    ) -> None:
        store = InMemoryJournalStore()
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 5")
        await store.delete(
            "pydeno:session:alice:s1:counter"
        )  # the counter write was lost
        async with pool(store) as p:
            async with p.session("alice", "s1") as sb:
                assert await sb.run("return n") == 5


class TestLimits:
    async def test_over_cap_journal_drops_the_state(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store, max_journal_bytes=2000) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 1")
            sb = await p.get("alice", "s1")
            await sb.run("return '" + "x" * 3000 + "'")
            with pytest.raises(JournalTooLarge, match="state was dropped"):
                await p.release("alice", "s1")
            assert sb.is_closed()
            # What is stored now is a journal without state that only charges the spent budget.
            stored = await store.get("pydeno:session:alice:s1:journal")
            assert (
                stored is not None
                and b'"lost"' in stored
                and b"globalThis" not in stored
            )
            async with p.session("alice", "s1") as fresh:
                assert await fresh.run("return typeof n") == "undefined"
                assert fresh.lost_runs == 1

    async def test_over_cap_journal_keeps_the_spent_tool_budget(self) -> None:
        # A session whose journal outgrows the cap is restored with the budget it already spent.
        store = InMemoryJournalStore()
        ran: list[int] = []

        def big(i: int) -> str:
            ran.append(i)
            return "x" * 5000

        async with SessionPool(
            store, KEY, {"big": big}, max_tool_calls=3, max_journal_bytes=8000
        ) as p:
            for _ in range(3):
                sb = await p.get("alice", "s1")
                await sb.execute(
                    "for (let i = 0; i < 9; i++) { try { await big(i) } catch { break } }"
                )
                with contextlib.suppress(JournalTooLarge):
                    await p.release("alice", "s1")
            assert len(ran) == 3
            async with p.session("alice", "s1") as again:
                assert again.calls_remaining == 0

    async def test_journals_are_written_with_the_ttl(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store, ttl=0.3, idle_timeout=None) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 1")
            assert await store.get("pydeno:session:alice:s1:journal") is not None
            await asyncio.sleep(0.5)
            assert await store.get("pydeno:session:alice:s1:journal") is None
            assert await store.get("pydeno:session:alice:s1:counter") == b"1"


class TestEviction:
    async def test_idle_sessions_are_evicted_in_the_background(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store, idle_timeout=0.2, eviction_interval=0.05) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 7")
            pid = sb.worker_pid
            await asyncio.sleep(0.6)
            assert len(p) == 0
            assert sb.is_closed()
            assert await _gone(pid)
            async with p.session("alice", "s1") as again:  # restored from the store
                assert again is not sb
                assert await again.run("return n") == 7

    async def test_leased_sessions_are_never_evicted(self) -> None:
        async with pool(idle_timeout=0.1, eviction_interval=0.05) as p:
            sb = await p.get("alice", "s1")
            await asyncio.sleep(0.4)
            assert not sb.is_closed()
            assert await sb.run("return 1") == 1
            await p.release("alice", "s1")

    async def test_max_sessions_evicts_the_least_recently_used(self) -> None:
        async with pool(max_sessions=2) as p:
            for sid in ("a", "b"):
                async with p.session("o", sid) as sb:
                    await sb.run(f"globalThis.id = '{sid}'")
            async with p.session("o", "a"):
                pass  # "a" is now the most recently used
            b = await p.get("o", "b")
            await p.release("o", "b")
            async with p.session("o", "a"):
                pass
            async with p.session("o", "c"):
                pass
            assert set(k[1] for k in p._entries) == {"a", "c"}  # noqa: SLF001
            assert b.is_closed()
            async with p.session("o", "b") as again:
                assert await again.run("return id") == "b"

    async def test_max_per_owner(self) -> None:
        async with pool(max_per_owner=2, acquire_timeout=0) as p:
            await p.get("alice", "1")
            await p.get("alice", "2")
            with pytest.raises(PoolFull, match="max_per_owner"):
                await p.get("alice", "3")
            await p.get("bob", "1")  # other owners are not affected
            await p.release("alice", "1")
            await p.get("alice", "3")  # evicts alice:1, the idle one
            assert ("alice", "1") not in p._entries  # noqa: SLF001
            for k in (("alice", "2"), ("alice", "3"), ("bob", "1")):
                await p.release(*k)

    async def test_pool_full_when_everything_is_leased(self) -> None:
        async with pool(max_sessions=1) as p:
            await p.get("a", "1")
            with pytest.raises(PoolFull, match="max_sessions"):
                await p.get("b", "1")
            await p.release("a", "1")


class TestCrashSafety:
    async def test_dead_worker_is_restored_from_the_last_journal(self) -> None:
        async with pool() as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = await add(1, 1)")
            os.kill(sb.worker_pid, signal.SIGKILL)
            assert await _gone(sb.worker_pid)
            async with p.session("alice", "s1") as again:
                assert again is not sb
                assert await again.run("return n") == 2
                assert again.calls_made == 1

    async def test_a_run_that_kills_the_worker_restores_the_last_good_state(
        self,
    ) -> None:
        async with pool(timeout=0.5) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 1")
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 2")
                step = await sb.start("while (true) {}")
                assert sb.is_closed(), step
            async with p.session("alice", "s1") as again:
                # The last good run before the crash, not the last release.
                assert await again.run("return n") == 2
                assert again.lost_runs == 1

    async def test_cancel_while_running_releases_the_worker_and_the_lease(self) -> None:
        async with pool() as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 3")

            async def stuck() -> None:
                async with p.session("alice", "s1") as sb:
                    await sb.run("while (true) {}")

            task = asyncio.create_task(stuck())
            await asyncio.sleep(0.3)
            sb = p._entries["alice", "s1"].sandbox  # noqa: SLF001
            assert sb is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert sb.is_closed() and await _gone(sb.worker_pid)
            assert p.stats()["leased"] == 0
            async with p.session("alice", "s1", timeout=0) as again:
                assert await again.run("return n") == 3

    async def test_cancel_while_paused(self) -> None:
        gate = asyncio.Event()

        async def approve() -> bool:
            gate.set()
            await asyncio.sleep(3600)  # waiting on a human
            return True

        store = InMemoryJournalStore()
        async with SessionPool(store, KEY, {"approve": approve}) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 4")

            async def agent() -> None:
                async with p.session("alice", "s1") as sb:
                    await sb.run("return await approve()")

            task = asyncio.create_task(agent())
            await gate.wait()
            sb = p._entries["alice", "s1"].sandbox  # noqa: SLF001
            assert sb is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert sb.is_closed() and await _gone(sb.worker_pid)
            async with p.session("alice", "s1", timeout=0) as again:
                assert await again.run("return n") == 4

    async def test_drop_while_leased_kills_the_run(self) -> None:
        async with pool() as p:
            sb = await p.get("alice", "s1")
            run = asyncio.create_task(sb.run("while (true) {}"))
            await asyncio.sleep(0.2)
            await p.drop("alice", "s1")
            with pytest.raises(Exception):  # noqa: B017, PT011 - the worker is gone
                await run
            await p.release("alice", "s1")  # no effect after a drop
            assert len(p) == 0

    async def test_close_kills_every_worker(self) -> None:
        p = pool()
        sessions = [await p.get("o", str(i)) for i in range(3)]
        await p.release("o", "0")
        await p.close()
        assert all(s.is_closed() for s in sessions)
        for s in sessions:
            assert await _gone(s.worker_pid)
        with pytest.raises(RuntimeError, match="closed"):
            await p.get("o", "0")


class TestParityFeatures:
    async def test_a_crash_while_paused_keeps_the_spent_budget(self) -> None:
        store = InMemoryJournalStore()
        async with pool(store, max_tool_calls=3) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = await add(1, 1)")
            async with p.session("alice", "s1") as sb:
                step = await sb.start("globalThis.n = 99; await add(1, 1)")
                assert isinstance(step, ToolCall)
                os.kill(sb.worker_pid, signal.SIGKILL)
                await sb.resume(step, 2)
                assert sb.is_closed() and sb.calls_made == 2
        async with pool(store) as p:  # another process: only the store is shared
            async with p.session("alice", "s1") as again:
                assert await again.run("return n") == 2  # the last good run
                assert again.calls_made == 2 and again.calls_remaining == 1
                assert again.lost_runs == 1

    async def test_a_worker_dying_while_paused_and_unreleased(self) -> None:
        async with pool(max_tool_calls=5) as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("globalThis.n = 1")
            sb = await p.get("alice", "s1")
            await sb.start("globalThis.n = 2; await add(1, 1)")
            os.kill(sb.worker_pid, signal.SIGKILL)
            assert await _gone(sb.worker_pid)
            await p.release("alice", "s1")
            async with p.session("alice", "s1") as again:
                assert await again.run("return n") == 1
                assert again.calls_made == 1  # the call the lost run made stays spent

    async def test_execute_and_console_through_the_pool(self) -> None:
        async with pool(max_output_bytes=64, max_result_bytes=100) as p:
            async with p.session("alice", "s1") as sb:
                r = await sb.execute("console.log('hi'); return await add(1, 2)")
                assert (r.status, r.stdout, r.result) == ("Succeeded", "hi\n", 3)
                big = await sb.execute("return 'x'.repeat(500)")
                assert big.error_type == "ResultTooLarge"
            p._entries.clear()  # noqa: SLF001 - forget the live session: restore from the store
            async with p.session("alice", "s1") as sb:
                # The result cap came back from the journal (load refuses it as an option).
                assert (await sb.execute("return 'x'.repeat(500)")).error_type == (
                    "ResultTooLarge"
                )

    async def test_schema_tools_and_the_catalog_through_the_pool(self) -> None:
        weather = SchemaTool(
            "get_weather",
            "Weather for a city.",
            {"type": "object", "properties": {"city": {"type": "string"}}},
            lambda args: {"city": args["city"], "temp": 20},
        )
        catalog = [
            SchemaTool(
                "translate_text",
                "Translate text.",
                {"type": "object", "properties": {"text": {"type": "string"}}},
                lambda args: args["text"].upper(),
            )
        ]
        store = InMemoryJournalStore()
        async with SessionPool(store, KEY, [weather], tools_catalog=catalog) as p:
            async with p.session("alice", "s1") as sb:
                assert (
                    await sb.run("return (await get_weather({city: 'Bern'})).temp")
                    == 20
                )
                await sb.run("await search_tools('translate')")
        async with SessionPool(store, KEY, [weather], tools_catalog=catalog) as p:
            async with p.session("alice", "s1") as sb:
                assert sb.discovered_tools == {"translate_text"}
                assert (
                    await sb.run("return await tools.translate_text({text: 'a'})")
                    == "A"
                )


class TestStore:
    async def test_in_memory_store(self) -> None:
        store = InMemoryJournalStore()
        await store.set("k", b"v", ttl=None)
        assert await store.get("k") == b"v"
        await store.set("t", b"v", ttl=0.05)
        await asyncio.sleep(0.1)
        assert await store.get("t") is None
        await store.delete("k")
        await store.delete("missing")
        assert await store.get("k") is None
        with pytest.raises(TypeError):
            await store.set("k", "str", ttl=None)  # type: ignore[arg-type]

    async def test_a_custom_store(self) -> None:
        class Recording(JournalStore):
            def __init__(self) -> None:
                self.data: dict[str, bytes] = {}
                self.ttls: dict[str, float | None] = {}

            async def get(self, key: str) -> bytes | None:
                return self.data.get(key)

            async def set(self, key: str, value: bytes, *, ttl: float | None) -> None:
                self.data[key] = value
                self.ttls[key] = ttl

            async def delete(self, key: str) -> None:
                self.data.pop(key, None)

        store = Recording()
        async with SessionPool(store, KEY, TOOLS, ttl=60, key_prefix="t/") as p:
            async with p.session("alice", "s1") as sb:
                await sb.run("1")
        assert store.ttls == {"t/alice:s1:journal": 60, "t/alice:s1:counter": None}

    def test_constructor_validation(self) -> None:
        with pytest.raises(TypeError):
            SessionPool({}, KEY, TOOLS)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            SessionPool(InMemoryJournalStore(), b"short", TOOLS)
        with pytest.raises(TypeError, match="sets"):
            SessionPool(InMemoryJournalStore(), KEY, TOOLS, clock=0)
        with pytest.raises(TypeError, match="sets"):
            SessionPool(InMemoryJournalStore(), KEY, TOOLS, request_timeout=1)
