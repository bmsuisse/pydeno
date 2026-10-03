"""`AgentSandbox.dump()` after the worker died: the journal as of the last good run.

Issue #14. A run that crashes, times out, is killed for memory or overstays ``max_pause`` takes the
worker with it. The session keeps a checkpoint at the end of every run that left the worker alive;
`dump()` on a dead session returns the journal up to that checkpoint plus a ``lost`` record for the
run that died (its code is never replayed; only the tool calls it spent are carried over, so a
crash cannot refund the budget). Signing, `associated_data`, release binding and the size cap apply
exactly as before.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from pydeno import JavaScriptError, ResultTooLarge, RuntimeTimeout, WorkerCrashed
from pydeno._agent import (
    AgentSandbox,
    Done,
    Failed,
    JournalError,
    ToolCall,
    _open,
    _seal,
)

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def tools() -> dict[str, Any]:
    return {"add": lambda a, b: a + b, "echo": lambda x: x}


def session(**kwargs: Any) -> AgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 7)
    return AgentSandbox(tools(), **kwargs)


def load(blob: bytes, **kwargs: Any) -> AgentSandbox:
    return AgentSandbox.load(blob, KEY, tools(), **kwargs)


def kill_worker(s: AgentSandbox) -> None:
    os.kill(s._core.rt._proc.pid, signal.SIGKILL)  # noqa: SLF001 - a worker this test started


def journal(blob: bytes) -> dict[str, Any]:
    return json.loads(_open(blob, KEY))


def reseal(blob: bytes, edit: Any) -> bytes:
    data = journal(blob)
    edit(data)
    return _seal(json.dumps(data).encode(), KEY)


def crash_while_paused(s: AgentSandbox, code: str) -> Failed:
    step = s.start(code)
    assert isinstance(step, ToolCall)
    kill_worker(s)
    after = s.resume(step, 1)
    assert isinstance(after, Failed)
    assert isinstance(after.error, WorkerCrashed)
    assert s.is_closed()
    return after


class TestDumpAfterACrash:
    def test_the_journal_covers_the_runs_before_the_crash(self) -> None:
        s = session()
        s.run("globalThis.a = await add(1, 2)")
        s.run("let b = a * 10")
        crash_while_paused(s, "globalThis.a = 'clobbered'; await add(0, 0)")
        blob = s.dump(KEY)
        s.close()

        records = journal(blob)["records"]
        codes = [r[1] for r in records if r[0] == "run"]
        assert codes == ["globalThis.a = await add(1, 2)", "let b = a * 10"]
        assert records[-1] == ["lost", 1, "WorkerCrashed"]
        with load(blob) as t:
            # The state after run 2, not anything the crashed run did.
            assert t.run("return [a, b]") == [3, 30]
            assert t.lost_runs == 1
            assert t.pending is None
            assert not t.is_closed()

    def test_load_replays_to_the_same_state_and_keeps_going(self) -> None:
        s = session()
        s.run("globalThis.log = []; for (const x of [1, 2, 3]) log.push(await echo(x))")
        s.run("globalThis.r = Math.random()")  # seeded: replay must reproduce it
        expected = s.run("return [log, r]")
        crash_while_paused(s, "await echo('lost')")
        blob = s.dump(KEY)
        s.close()
        with load(blob) as t:
            assert t.run("return [log, r]") == expected
            t.run("log.push(await echo(4))")
            again = t.dump(KEY)
        with load(again) as u:
            assert u.run("return log") == [1, 2, 3, 4]
            assert u.lost_runs == 1

    def test_a_crash_in_the_first_run_leaves_an_empty_history(self) -> None:
        s = session()
        crash_while_paused(s, "globalThis.x = 1; await add(1, 1)")
        blob = s.dump(KEY)
        s.close()
        assert journal(blob)["records"] == [["lost", 1, "WorkerCrashed"]]
        with load(blob) as t:
            assert t.run("return typeof x") == "undefined"

    def test_dumping_a_dead_session_is_repeatable(self) -> None:
        s = session()
        s.run("globalThis.n = 1")
        crash_while_paused(s, "await add(1, 1)")
        assert s.dump(KEY) == s.dump(KEY)
        s.close()

    def test_a_crash_while_running_not_paused(self) -> None:
        s = session(timeout=0.5)
        s.run("globalThis.kept = await add(20, 22)")
        step = s.start("globalThis.kept = 0; while (true) {}")
        assert isinstance(step, Failed)
        assert isinstance(step.error, RuntimeTimeout)
        blob = s.dump(KEY)
        s.close()
        assert journal(blob)["records"][-1] == ["lost", 0, "RuntimeTimeout"]
        with load(blob, timeout=5) as t:
            assert t.run("return kept") == 42

    def test_a_session_nobody_resumed(self) -> None:
        s = session(max_pause=0.5)
        s.run("globalThis.v = 'before'")
        step = s.start("return await add(1, 2)")
        assert isinstance(step, ToolCall)
        deadline = time.monotonic() + 10
        while not s._core.rt.is_closed() and time.monotonic() < deadline:  # noqa: SLF001
            time.sleep(0.05)
        assert isinstance(s.resume(step, 3), Failed)
        blob = s.dump(KEY)
        s.close()
        with load(blob) as t:
            assert t.run("return v") == "before"

    def test_a_memory_kill(self) -> None:
        s = session(max_memory=256 * 1024 * 1024)
        s.run("globalThis.small = [1, 2, 3]")
        step = s.start(
            "const chunks = []; while (true) chunks.push(new Array(1 << 20).fill(1.5));"
        )
        assert isinstance(step, Failed)
        assert s.is_closed()
        blob = s.dump(KEY)
        s.close()
        with load(blob) as t:
            assert t.run("return small") == [1, 2, 3]


class TestTheFailedRunIsNeverReplayed:
    def test_its_tool_calls_are_not_answered_again(self) -> None:
        calls: list[Any] = []

        def record(x: Any) -> Any:
            calls.append(x)
            return x

        s = AgentSandbox({"record": record}, clock=CLOCK, random_seed=1)
        s.run("await record('good')")
        step = s.start("await record('doomed-1'); await record('doomed-2')")
        s.resume(step, s.call(step))
        step = s.pending
        assert isinstance(step, ToolCall)
        kill_worker(s)
        s.resume(step, "x")
        blob = s.dump(KEY)
        s.close()
        assert b"doomed" not in blob
        with AgentSandbox.load(blob, KEY, {"record": record}) as t:
            assert t.run("return 1") == 1
        assert calls == ["good", "doomed-1"]  # replay calls no real tools at all

    def test_a_lost_record_inside_a_run_is_refused(self) -> None:
        s = session()
        step = s.start("return await add(1, 2)")
        assert isinstance(step, ToolCall)
        blob = s.dump(KEY)  # paused: the journal ends with the call's outcome
        s.close()

        def insert(data: dict[str, Any]) -> None:
            data["records"].append(["lost", 0, "WorkerCrashed"])

        with pytest.raises(JournalError, match="malformed"):
            load(reseal(blob, insert))


class TestTheBudgetIsNotRefunded:
    def test_calls_made_by_the_crashed_run_stay_spent(self) -> None:
        s = session(max_tool_calls=6)
        s.run("await add(1, 1)")  # 1
        step = s.start("await add(1, 1); await add(1, 1); await add(1, 1)")
        step = s.resume(step, 2)
        step = s.resume(step, 2)
        assert isinstance(step, ToolCall)  # the third call of this run: 4 so far
        kill_worker(s)
        s.resume(step, 2)
        assert s.calls_made == 4
        blob = s.dump(KEY)
        s.close()
        assert journal(blob)["records"][-1] == ["lost", 3, "WorkerCrashed"]
        with load(blob) as t:
            assert t.calls_made == 4
            assert t.calls_remaining == 2
            t.run("await add(1, 1); await add(1, 1)")
            with pytest.raises(JavaScriptError, match="ToolBudgetError"):
                t.run("await add(1, 1)")

    def test_repeated_crash_and_load_cannot_mint_calls(self) -> None:
        blob = None
        for _ in range(3):
            s = load(blob) if blob is not None else session(max_tool_calls=5)
            crash_while_paused(s, "await add(1, 1)")
            blob = s.dump(KEY)
            s.close()
        assert blob is not None
        with load(blob) as t:
            assert t.calls_made == 3
            assert t.lost_runs == 3


class TestCompletedFailuresStay:
    def test_a_javascript_error_is_a_completed_run(self) -> None:
        s = session()
        with pytest.raises(JavaScriptError):
            s.run("globalThis.partial = 1; throw new Error('x')")
        crash_while_paused(s, "await add(1, 1)")
        blob = s.dump(KEY)
        s.close()
        with load(blob) as t:
            assert t.run("return partial") == 1

    def test_a_too_large_result_is_a_completed_run(self) -> None:
        s = session(max_result_bytes=20)
        with pytest.raises(ResultTooLarge):
            s.run("globalThis.big = 'y'.repeat(100); return big")
        crash_while_paused(s, "await add(1, 1)")
        blob = s.dump(KEY)
        s.close()
        with load(blob) as t:
            assert t.run("return big.length") == 100


class TestUnchangedRules:
    def _dead_blob(self, **dump: Any) -> bytes:
        s = session()
        s.run("globalThis.q = 1")
        crash_while_paused(s, "await add(1, 1)")
        try:
            return s.dump(KEY, **dump)
        finally:
            s.close()

    def test_tampering_is_detected(self) -> None:
        blob = self._dead_blob()
        forged = blob.replace(b'["lost",1,', b'["lost",0,')
        assert forged != blob
        with pytest.raises(JournalError, match="authentication"):
            load(forged)

    def test_a_wrong_key_is_refused(self) -> None:
        blob = self._dead_blob()
        with pytest.raises(JournalError, match="authentication"):
            AgentSandbox.load(blob, b"another key, 32 bytes long......", tools())

    def test_associated_data_is_bound(self) -> None:
        blob = self._dead_blob(associated_data=b"tenant-1")
        with pytest.raises(JournalError):
            load(blob)
        with pytest.raises(JournalError):
            load(blob, associated_data=b"tenant-2")
        with load(blob, associated_data=b"tenant-1") as t:
            assert t.run("return q") == 1

    def test_the_release_is_bound(self) -> None:
        blob = self._dead_blob()

        def other_release(data: dict[str, Any]) -> None:
            data["config"]["release"] = "pydeno 0.0.1"

        with pytest.raises(JournalError, match="recorded by pydeno"):
            load(reseal(blob, other_release))

    def test_the_size_cap_still_applies(self) -> None:
        s = session(max_journal_bytes=200)
        s.run("globalThis.x = " + json.dumps("z" * 300))
        crash_while_paused(s, "await add(1, 1)")
        with pytest.raises(JournalError, match="max_journal_bytes"):
            s.dump(KEY)
        s.close()

    @pytest.mark.parametrize(
        "record",
        [
            ["lost"],
            ["lost", -1, "WorkerCrashed"],
            ["lost", True, "WorkerCrashed"],
            ["lost", 2**60, "WorkerCrashed"],
            ["lost", 1, "not a name!"],
            ["lost", 1, 5],
            ["lost", 1, "WorkerCrashed", "extra"],
        ],
        ids=["short", "negative", "bool", "huge", "bad-name", "non-str", "long"],
    )
    def test_malformed_lost_records_are_refused(self, record: list[Any]) -> None:
        blob = self._dead_blob()

        def swap(data: dict[str, Any]) -> None:
            data["records"][-1] = record

        with pytest.raises(JournalError, match="malformed"):
            load(reseal(blob, swap))

    def test_a_live_session_dumps_everything_as_before(self) -> None:
        with session() as s:
            s.run("globalThis.w = 1")
            step = s.start("return await add(w, 1)")
            assert isinstance(step, ToolCall)
            blob = s.dump(KEY)
        assert all(r[0] != "lost" for r in journal(blob)["records"])
        with load(blob) as t:
            assert t.pending is not None
            assert t.resume(t.pending, 2) == Done(2)
