"""Regression tests for the slice C red-team findings (#75: host boundary and state).

Each test is the failing-first reproduction of a finding (the same attacks are probes in
`scripts/autoresearch/metric_security.py`, section "slice C"):

* a tool raising a BaseException, and a burst of concurrent tool calls, left a journal that
  `dump()` returned and `load()` refused;
* `SessionPool` gave a session a fresh tool budget after its journal outgrew the cap, accepted
  ids it could not persist, and let a concurrent `get()` undo a `drop()`;
* session-owned global names were accepted as tool names;
* the front door's compile check ran guest-reachable JavaScript outside the journal;
* a refused answer used up a front-door snapshot; front-door dumps could not be bound to a tenant;
* captured console output, error text, the default printer and the CLI passed escape sequences
  and bidi overrides through.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import subprocess
import sys
import time

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncPydeno,
    InMemoryJournalStore,
    IsolatedRuntime,
    JournalTooLarge,
    Pydeno,
    PydenoCrashedError,
    PydenoError,
    PydenoRuntimeError,
    SessionPool,
    StaleJournal,
    WorkerCrashed,
)

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"
KEY = b"0123456789abcdef0123456789abcdef"
BIDI = "\u202e\u2066"
pytestmark = pytest.mark.filterwarnings("ignore::RuntimeWarning")


class _Stop(BaseException):
    pass


def _stop(*args: object) -> None:
    raise _Stop("text that must not reach the guest")


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


class TestToolBaseException:
    def test_the_run_is_lost_and_the_journal_still_loads(self) -> None:
        tools = {"stop": _stop, "ok": lambda: 1}
        with AgentSandbox(tools, sandbox=MODE) as s:
            s.run("const kept = 41")
            r = s.execute("try { await stop() } catch (e) { return e.message }")
            assert r.status == "Failed" and r.error_type == "WorkerCrashed"
            assert "_Stop" in (r.error or "") and "must not" not in (r.error or "")
            assert s.is_closed()
            blob = s.dump(KEY)
        with AgentSandbox.load(blob, KEY, tools, sandbox=MODE) as restored:
            assert restored.lost_runs == 1
            assert restored.run("return kept + 1") == 42
            assert restored.calls_made == 1  # the lost run's call stays spent

    def test_front_door_feed_crashes_and_the_state_loads(self) -> None:
        with Pydeno(min_processes=1, sandbox=MODE) as pool:
            with pool.checkout() as s:
                s.feed_run("const kept = 41")
                with pytest.raises(PydenoCrashedError):
                    s.feed_run("await stop(1)", external_lookup={"stop": _stop})
                state = s.dump()
            with pool.checkout() as s2:
                s2.load_session(state)
                assert s2.feed_run("kept + 1") == 42


def _slow(i: int) -> int:
    time.sleep(0.0005 * (i % 4))
    return i


_STORM = (
    "const rs = await Promise.allSettled(Array.from({length: 200}, (_, i) => t(i)));"
    "return [rs.filter(r => r.status === 'rejected').length, rs.map(r => r.value)]"
)


class TestConcurrentCalls:
    def test_calls_past_the_inflight_cap_wait_their_turn_and_replay(self) -> None:
        with AgentSandbox({"t": _slow}, max_tool_calls=1000, sandbox=MODE) as s:
            rejected, values = s.run(_STORM)
            assert rejected == 0 and values == list(range(200))
            assert s.calls_made == 200
            blob = s.dump(KEY)
        with AgentSandbox.load(blob, KEY, {"t": _slow}, sandbox=MODE) as restored:
            assert restored.calls_made == 200

    def test_paused_driving_sees_every_call_in_order(self) -> None:
        with AgentSandbox({"t": _slow}, sandbox=MODE) as s:
            step = s.start(_STORM)
            seen = []
            while not hasattr(step, "value") and not hasattr(step, "error"):
                seen.append(step.args[0])
                step = s.resume(step, step.args[0])
            assert step.value[0] == 0 and seen == list(range(200))

    def test_the_budget_still_holds(self) -> None:
        ran: list[int] = []
        with AgentSandbox(
            {"t": lambda i: ran.append(i) or i}, max_tool_calls=5, sandbox=MODE
        ) as s:
            rejected, _ = s.run(_STORM)
            assert rejected == 195 and len(ran) == 5

    async def test_async_session(self) -> None:
        async def t(i: int) -> int:
            await asyncio.sleep(0.0005 * (i % 4))
            return i

        async with AsyncAgentSandbox({"t": t}, sandbox=MODE) as s:
            rejected, values = await s.run(_STORM)
            assert rejected == 0 and values == list(range(200))
            blob = await s.dump(KEY)
        restored = await AsyncAgentSandbox.load(blob, KEY, {"t": t}, sandbox=MODE)
        await restored.close()

    def test_front_door_feed(self) -> None:
        with Pydeno(min_processes=1, sandbox=MODE) as pool, pool.checkout() as s:
            got = s.feed_run(
                "(await Promise.all(Array.from({length: 150}, (_, i) => f(i)))).length",
                external_lookup={"f": lambda i: i},
            )
            assert got == 150


class TestToolNames:
    @pytest.mark.parametrize(
        "name", ["__pydeno_agent_settle", "__pydeno_external", "__host_op_async__"]
    )
    def test_reserved_names_are_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="reserved"):
            AgentSandbox({name: lambda: 1}, sandbox=MODE)

    @pytest.mark.parametrize(
        "name", ["globalThis", "Promise", "console", "JSON", "eval"]
    )
    def test_guest_globals_are_refused_as_bare_globals(self, name: str) -> None:
        with pytest.raises(ValueError, match="would replace"):
            AgentSandbox({name: lambda: 1}, sandbox=MODE)

    def test_on_a_namespace_they_are_fine(self) -> None:
        with AgentSandbox({"JSON": lambda: 7}, namespace="tools", sandbox=MODE) as s:
            assert s.run("return [await tools.JSON(), JSON.stringify(1)]") == [7, "1"]

    def test_namespace_must_not_replace_a_global(self) -> None:
        with pytest.raises(ValueError, match="namespace"):
            AgentSandbox({"f": lambda: 1}, namespace="Math", sandbox=MODE)

    def test_a_front_door_journal_still_loads_into_a_plain_session(self) -> None:
        with Pydeno(min_processes=1, sandbox=MODE) as pool, pool.checkout() as s:
            s.feed_run("var x = 3")
            state = s.dump()
            key = pool._key  # noqa: SLF001
        with AgentSandbox.load(
            state, key, {"__pydeno_external": print}, sandbox=MODE
        ) as p:
            assert p.run("return x") == 3


# ---------------------------------------------------------------------------
# the front door
# ---------------------------------------------------------------------------


class TestFrontDoor:
    def test_a_refused_answer_leaves_the_snapshot_resumable(self) -> None:
        with Pydeno(min_processes=1, sandbox=MODE) as pool, pool.checkout() as s:
            snap = s.feed_start("await f(1)", external_lookup={"f": lambda x: x})
            with pytest.raises(TypeError):
                snap.resume({"exception": "not an exception"})
            with pytest.raises(TypeError):
                snap.resume(error=KeyboardInterrupt())  # type: ignore[arg-type]
            assert snap.resume(value=5).output == 5

    async def test_a_refused_answer_leaves_the_async_snapshot_resumable(self) -> None:
        async with AsyncPydeno(min_processes=1, sandbox=MODE) as pool:
            async with pool.checkout() as s:
                snap = await s.feed_start(
                    "await f(1)", external_lookup={"f": lambda x: x}
                )
                with pytest.raises(TypeError):
                    await snap.resume(error="no")  # type: ignore[arg-type]
                assert (await snap.resume(value=6)).output == 6

    def test_snapshots_only_for_declared_functions(self) -> None:
        code = (
            "const seen = [];"
            "for (const f of [() => leftover(), () => __pydeno_external('drop_db', 1), () => read(2)])"
            " { try { seen.push(await f()) } catch (e) { seen.push(e.name) } }"
            "seen"
        )
        with Pydeno(min_processes=1, sandbox=MODE) as pool, pool.checkout() as s:
            s.feed_run("1", external_lookup={"leftover": lambda: 1})
            snap = s.feed_start(code, external_lookup={"read": lambda x: x})
            assert snap.function_name == "read" and snap.args == (2,)
            done = snap.resume(value="ok")
            assert done.output == ["ReferenceError", "ReferenceError", "ok"]
            snap = s.feed_start(
                "await read(1); let r; try { r = await other(2) } catch (e) { r = e.name }\nr",
                external_lookup={"read": print, "other": print},
            )
            state = snap.dump()
            with pool.checkout() as s2:
                # Restored without external_lookup: the names are not known, nothing is refused.
                snap = s2.load_snapshot(state)
                assert snap.function_name == "read"
                assert snap.resume(value=1).function_name == "other"
            with pool.checkout() as s3:
                # Restored with external_lookup: only those names are surfaced.
                snap = s3.load_snapshot(state, external_lookup={"read": print})
                assert snap.function_name == "read"
                assert snap.resume(value=1).output == "ReferenceError"

    def test_dumps_can_be_bound_to_a_tenant(self) -> None:
        with Pydeno(min_processes=1, sandbox=MODE) as pool:
            with pool.checkout() as s:
                s.feed_run("const who = 'a'")
                state = s.dump(associated_data=b"tenant-a")
                snap = s.feed_start("await f(1)", external_lookup={"f": lambda x: x})
                paused = snap.dump(associated_data=b"tenant-a")
            with pool.checkout() as s2:
                for wrong in (b"tenant-b", b""):
                    with pytest.raises(PydenoError, match="authentication"):
                        s2.load_session(state, associated_data=wrong)
                s2.load_session(state, associated_data=b"tenant-a")
                assert s2.feed_run("who") == "a"
                with pytest.raises(PydenoError):
                    s2.load_snapshot(paused)
                again = s2.load_snapshot(paused, associated_data=b"tenant-a")
                assert again.resume(value=1).output == 1
            with pool.checkout() as s3, pytest.raises(TypeError):
                s3.dump(associated_data="tenant-a")  # type: ignore[arg-type]

    async def test_async_dumps_can_be_bound_to_a_tenant(self) -> None:
        async with AsyncPydeno(min_processes=1, sandbox=MODE) as pool:
            async with pool.checkout() as s:
                await s.feed_run("const who = 'a'")
                state = await s.dump(associated_data=b"t-a")
            async with pool.checkout() as s2:
                with pytest.raises(PydenoError):
                    await s2.load_session(state, associated_data=b"t-b")
                await s2.load_session(state, associated_data=b"t-a")
                assert await s2.feed_run("who") == "a"

    def test_the_syntax_check_cannot_be_steered_by_the_guest(self) -> None:
        ran: list[int] = []
        with Pydeno(min_processes=1, sandbox=MODE) as pool:
            with pool.checkout() as s:
                s.feed_run(
                    "const real = Object.getPrototypeOf;"
                    "Object.getPrototypeOf = function (o) {"
                    " globalThis.n = (globalThis.n || 0) + 1;"
                    " throw new SyntaxError('lie') }"
                )
                with pytest.raises(PydenoRuntimeError, match="after a side effect"):
                    s.feed_run(
                        "await mark(1); throw new SyntaxError('after a side effect')",
                        external_lookup={"mark": lambda x: ran.append(x)},
                    )
                assert ran == [1]
                assert s.feed_run("globalThis.n") is None  # the check ran no guest code
                state = s.dump()
            with pool.checkout() as s2:
                s2.load_session(state)
                assert s2.feed_run("typeof real") == "function"


# ---------------------------------------------------------------------------
# SessionPool
# ---------------------------------------------------------------------------


class TestSessionPool:
    async def test_a_long_unicode_id_persists(self) -> None:
        owner, sid = "ä" * 256, "\U0001f600" * 256
        store = InMemoryJournalStore()
        async with SessionPool(store, KEY, {}, sandbox=MODE) as pool:
            async with pool.session(owner, sid) as sb:
                await sb.run("globalThis.x = 5")
        async with SessionPool(store, KEY, {}, sandbox=MODE) as pool:
            async with pool.session(owner, sid) as sb:
                assert await sb.run("return x") == 5

    async def test_drop_wins_against_a_concurrent_restore(self) -> None:
        class SlowStore(InMemoryJournalStore):
            gate: asyncio.Event | None = None

            async def get(self, key: str) -> bytes | None:
                gate = self.gate
                if gate is not None and key.endswith(":counter"):
                    self.gate = None
                    await gate.wait()
                return await super().get(key)

        store = SlowStore()
        async with SessionPool(
            store, KEY, {}, idle_timeout=0.01, eviction_interval=1000, sandbox=MODE
        ) as pool:
            async with pool.session("o", "s") as sb:
                await sb.run("globalThis.secret = 'kept'")
            await asyncio.sleep(0.05)
            await pool.evict_idle()
            gate = asyncio.Event()
            store.gate = gate
            dropping = asyncio.ensure_future(pool.drop("o", "s"))
            await asyncio.sleep(0.05)
            getting = asyncio.ensure_future(pool.get("o", "s"))
            await asyncio.sleep(0.3)
            assert not getting.done()  # waits for the drop instead of restoring
            gate.set()
            await dropping
            sb = await getting
            assert await sb.run("return typeof secret") == "undefined"
            await pool.release("o", "s")

    async def test_session_block_does_not_release_a_newer_lease(self) -> None:
        store = InMemoryJournalStore()
        async with SessionPool(store, KEY, {}, sandbox=MODE) as pool:
            async with pool.session("o", "s"):
                await pool.drop("o", "s")
                newer = await pool.get("o", "s")
            # The block's exit did not end the newer lease.
            assert pool.stats()["leased"] == 1
            await newer.run("return 1")
            await pool.release("o", "s")
            assert pool.stats()["leased"] == 0

    async def test_over_cap_journal_keeps_the_spent_budget_across_processes(
        self,
    ) -> None:
        store = InMemoryJournalStore()
        ran: list[int] = []

        def big(i: int) -> str:
            ran.append(i)
            return "x" * 5000

        async with SessionPool(
            store,
            KEY,
            {"big": big},
            max_tool_calls=4,
            max_journal_bytes=8000,
            sandbox=MODE,
        ) as pool:
            sb = await pool.get("a", "s")
            await sb.execute(
                "for (let i = 0; i < 9; i++) { try { await big(i) } catch { break } }"
            )
            with pytest.raises(JournalTooLarge, match="budget kept"):
                await pool.release("a", "s")
        async with SessionPool(
            store,
            KEY,
            {"big": big},
            max_tool_calls=4,
            max_journal_bytes=8000,
            sandbox=MODE,
        ) as pool:
            async with pool.session("a", "s") as sb:
                assert sb.calls_remaining == 0
                r = await sb.execute("try { await big(0) } catch (e) { return e.name }")
                assert r.result == "ToolBudgetError"
        assert len(ran) == 4


_LOOP = (
    "let n = 0; for (let i = 0; i < %d; i++) { try { await %s(i); n++ } catch { break } }"
    " return n"
)


class _GateStore(InMemoryJournalStore):
    """Stalls the first `get` or `set` of a key with the given suffix until `gate` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.op = ""
        self.suffix = ""

    async def _wait(self, op: str, key: str) -> None:
        gate = self.gate
        if gate is not None and op == self.op and key.endswith(self.suffix):
            self.gate = None
            await gate.wait()

    async def get(self, key: str) -> bytes | None:
        value = await super().get(key)
        await self._wait("get", key)
        return value

    async def set(self, key: str, value: bytes, *, ttl: float | None) -> None:
        await self._wait("set", key)
        await super().set(key, value, ttl=ttl)

    def stall(self, op: str, suffix: str) -> asyncio.Event:
        self.gate, self.op, self.suffix = asyncio.Event(), op, suffix
        return self.gate


class TestSessionPoolConsistency:
    """The pool's live sessions, leases and stored journals stay one session with one budget,
    also when calls on the same session overlap."""

    async def test_get_during_an_oversized_release_sees_the_spent_budget(self) -> None:
        ran: list[int] = []

        def big(i: int) -> str:
            ran.append(i)
            return "x" * 5000

        def small(i: int) -> int:
            ran.append(i)
            return i

        tools = {"big": big, "small": small}
        kw = dict(max_tool_calls=6, max_journal_bytes=8000, sandbox=MODE)
        store = InMemoryJournalStore()
        async with SessionPool(store, KEY, tools, **kw) as pool:
            async with pool.session("a", "s") as sb:
                await sb.execute("await small(0)")
            sb = await pool.get("a", "s")
            await sb.execute(_LOOP % (9, "big"))
            releasing = asyncio.ensure_future(pool.release("a", "s"))
            await asyncio.sleep(0)
            racer = await pool.get("a", "s")
            await racer.execute(_LOOP % (9, "small"))
            with contextlib.suppress(JournalTooLarge):
                await releasing
            await pool.release("a", "s")
        assert len(ran) == 6

    async def test_two_pools_sharing_a_store_share_one_budget(self) -> None:
        ran: list[int] = []

        def small(i: int) -> int:
            ran.append(i)
            return i

        store = InMemoryJournalStore()
        kw = dict(max_tool_calls=3, sandbox=MODE)
        async with (
            SessionPool(store, KEY, {"small": small}, **kw) as pa,
            SessionPool(store, KEY, {"small": small}, **kw) as pb,
        ):
            async with pa.session("o", "s") as sb:
                await sb.run("globalThis.who = 'a'")
            for pool in (pb, pa, pb, pa):
                async with pool.session("o", "s") as sb:
                    await sb.execute(_LOOP % (9, "small"))
            async with pa.session("o", "s") as sb:
                assert sb.calls_remaining == 0
        assert len(ran) == 3

    async def test_a_live_session_older_than_the_store_is_not_persisted(self) -> None:
        store = InMemoryJournalStore()
        async with (
            SessionPool(store, KEY, {}, sandbox=MODE) as pa,
            SessionPool(store, KEY, {}, sandbox=MODE) as pb,
        ):
            sa = await pa.get("o", "s")
            async with pb.session("o", "s") as sb:
                await sb.run("globalThis.v = 'b'")
            await sa.run("globalThis.v = 'a'")
            with pytest.raises(StaleJournal):
                await pa.release("o", "s")
            async with pa.session("o", "s") as again:
                assert await again.run("return v") == "b"

    async def test_get_restoring_while_drop_runs_starts_over(self) -> None:
        store = _GateStore()
        async with SessionPool(
            store, KEY, {}, idle_timeout=0.01, eviction_interval=1000, sandbox=MODE
        ) as pool:
            async with pool.session("o", "s") as sb:
                await sb.run("globalThis.secret = 'kept'")
            await asyncio.sleep(0.05)
            await pool.evict_idle()
            gate = store.stall("get", ":journal")
            getting = asyncio.ensure_future(pool.get("o", "s"))
            await asyncio.sleep(0.05)
            await pool.drop("o", "s")
            gate.set()
            sb = await getting
            assert await sb.run("return typeof secret") == "undefined"
            assert pool.stats()["live"] == 1 and pool.stats()["leased"] == 1
            await pool.release("o", "s")
            async with pool.session("o", "s") as again:
                assert again is sb

    async def test_drop_wins_against_a_slow_release(self) -> None:
        store = _GateStore()
        async with SessionPool(store, KEY, {}, sandbox=MODE) as pool:
            sb = await pool.get("o", "s")
            await sb.run("globalThis.secret = 'dropped'")
            gate = store.stall("set", ":journal")
            releasing = asyncio.ensure_future(pool.release("o", "s"))
            await asyncio.sleep(0.05)
            dropping = asyncio.ensure_future(pool.drop("o", "s"))
            await asyncio.sleep(0.1)
            gate.set()
            await asyncio.gather(releasing, dropping, return_exceptions=True)
            async with pool.session("o", "s") as fresh:
                assert await fresh.run("return typeof secret") == "undefined"

    async def test_a_failed_release_keeps_the_spent_budget(self) -> None:
        ran: list[int] = []

        async def slow(i: int) -> int:
            ran.append(i)
            await asyncio.sleep(0.05)
            return i

        store = InMemoryJournalStore()
        async with SessionPool(
            store,
            KEY,
            {"slow": slow},
            max_tool_calls=4,
            idle_timeout=0.01,
            eviction_interval=1000,
            sandbox=MODE,
        ) as pool:
            sb = await pool.get("o", "s")
            task = asyncio.ensure_future(sb.execute(_LOOP % (4, "slow")))
            await asyncio.sleep(0.02)
            with pytest.raises(RuntimeError, match="busy"):
                await pool.release(
                    "o", "s"
                )  # a run is in progress: not a valid release
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0.05)
            await pool.evict_idle()
            async with pool.session("o", "s") as again:
                await again.execute(_LOOP % (4, "slow"))
        assert len(ran) <= 4

    @pytest.mark.parametrize("bad", ["s\ud800", "\udfff"])
    async def test_ids_that_are_not_utf8_are_refused(self, bad: str) -> None:
        async with SessionPool(InMemoryJournalStore(), KEY, {}, sandbox=MODE) as pool:
            with pytest.raises(ValueError, match="session_id"):
                await pool.get("o", bad)


# ---------------------------------------------------------------------------
# text pydeno writes for the host
# ---------------------------------------------------------------------------

_HOSTILE = f"a\\x1b]0;owned\\x07{BIDI}\\u200bb\\u200dc"


class TestHostText:
    def test_captured_console_is_cleaned(self) -> None:
        with AgentSandbox({}, sandbox=MODE) as s:
            r = s.execute(
                f"console.log('{_HOSTILE}'); console.warn('x\\ty\\nz'); return 1"
            )
        assert r.stdout == "a?]0;owned????b\u200dc\n"
        assert r.stderr == "x\ty\nz\n"  # tab and newline stay

    def test_invisible_text_is_cleaned_but_scripts_and_emoji_stay(self) -> None:
        # A Unicode tag character (invisible to a reader, not to a language model), a soft hyphen
        # and a line separator go; Hebrew, ZWNJ and an emoji with its variation selector stay.
        with AgentSandbox({}, sandbox=MODE) as s:
            r = s.execute(
                "console.log('a\\u{e0041}\\u00ad\\u2028b \\u05d0\\u200c\\u2764\\ufe0f'); return 1"
            )
        assert r.stdout == "a???b \u05d0\u200c\u2764\ufe0f\n"

    def test_isolated_execute_is_cleaned(self) -> None:
        with IsolatedRuntime(sandbox=MODE, capture_console=True) as rt:
            r = rt.execute(f"console.log('{_HOSTILE}'); 1")
        assert "\x1b" not in r.stdout and not any(c in r.stdout for c in BIDI)

    def test_error_messages_are_cleaned(self) -> None:
        with AgentSandbox({}, sandbox=MODE) as s:
            with pytest.raises(Exception) as caught:  # noqa: PT011
                s.run(f"throw new Error('{_HOSTILE}')")
            r = s.execute(f"throw new Error('{_HOSTILE}')")
        for text in (str(caught.value), r.error or ""):
            assert "\x1b" not in text and not any(c in text for c in BIDI + "\u200b")

    def test_default_printer_is_cleaned(self) -> None:
        out = io.StringIO()
        with Pydeno(min_processes=1, sandbox=MODE) as pool, pool.checkout() as s:
            with contextlib.redirect_stdout(out):
                s.feed_run(f"console.log('{_HOSTILE}')")
        assert not any(c in out.getvalue() for c in BIDI + "\x1b\x07")

    def test_a_custom_print_callback_gets_the_raw_text(self) -> None:
        got: list[str] = []
        with Pydeno(min_processes=1, sandbox=MODE) as pool, pool.checkout() as s:
            s.feed_run(
                "console.log('\\u202e')", print_callback=lambda _s, t: got.append(t)
            )
        assert got == ["\u202e\n"]

    def test_cli_output_is_cleaned(self) -> None:
        for code in (f"'{_HOSTILE}'", f"throw new Error('{_HOSTILE}')"):
            done = subprocess.run(
                [sys.executable, "-m", "pydeno", "-c", code],
                capture_output=True,
                text=True,
                timeout=120,
            )
            text = done.stdout + done.stderr
            assert "owned" in text
            assert "\x1b" not in text and not any(c in text for c in BIDI)


def test_crash_message_names_the_tool_exception_class_only() -> None:
    with AgentSandbox({"stop": _stop}, sandbox=MODE) as s:
        with pytest.raises(
            WorkerCrashed, match=r"^a tool raised _Stop, which is not an answer"
        ):
            s.run("await stop()")
