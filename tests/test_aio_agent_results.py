"""`AsyncAgentSandbox.execute()` and the console fields of `Done`/`Failed`.

The async port of `TestAgentExecute`/`TestSteps`/`TestJournal` in `test_agent_execution_result.py`
(the capture itself is tested there; it is shared). Console calls reach the async session as
synchronous host calls on the handler pool, so ordering and the per-run reset are checked here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    ExecutionResult,
    ResultTooLarge,
    RuntimeConfig,
)
from pydeno._agent import Done, Failed, ToolCall, _open
from pydeno._result import DEFAULT_MAX_OUTPUT_BYTES, TRUNCATED_MARKER

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def add(a: int, b: int) -> int:
    return a + b


def session(tools: dict[str, Any] | None = None, **kwargs: Any) -> AsyncAgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 7)
    return AsyncAgentSandbox(tools if tools is not None else {"add": add}, **kwargs)


class TestExecute:
    async def test_a_successful_run(self) -> None:
        async with session() as s:
            r = await s.execute(
                "console.log('a', {x: 1}); console.warn('careful'); console.info('b');"
                "return await add(2, 3)"
            )
        assert r == ExecutionResult(
            status="Succeeded", stdout='a {"x":1}\nb\n', stderr="careful\n", result=5
        )
        assert json.loads(json.dumps(r.to_dict())) == r.to_dict()

    async def test_output_is_per_run_and_ordered(self) -> None:
        async with session() as s:
            r = await s.execute(
                "for (let i = 0; i < 50; i++) { console.log(i); await add(i, 0); }"
            )
            assert r.stdout == "".join(f"{i}\n" for i in range(50))
            assert (await s.execute("return 2")).stdout == ""

    async def test_results_are_json_data(self) -> None:
        async with session() as s:
            r = await s.execute(
                "return {d: new Date(0), b: new Uint8Array([1, 2]), s: new Set([2, 1]), "
                "u: undefined, n: NaN}"
            )
        assert r.result == {
            "d": "1970-01-01T00:00:00+00:00",
            "b": "AQI=",
            "s": [1, 2],
            "u": None,
            "n": None,
        }

    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            ("null.x", "TypeError"),
            ("nope", "ReferenceError"),
            ("throw new RangeError('r')", "RangeError"),
            ("throw 5", "Error"),
            ("return )(", "SyntaxError"),
            (
                "const e = new Error('fake'); e.name = 'RuntimeTimeout'; throw e",
                "Error",
            ),
        ],
        ids=["type", "reference", "range", "non-error", "syntax", "host-name-claimed"],
    )
    async def test_guest_errors(self, code: str, kind: str) -> None:
        async with session() as s:
            r = await s.execute(code)
            assert (r.status, r.error_type, r.result) == ("Failed", kind, None)
            assert not s.is_closed()

    async def test_tool_errors_are_redacted_and_keep_their_name(self) -> None:
        def boom() -> None:
            raise LookupError("secret path /etc/x")

        async with session({"boom": boom}) as s:
            r = await s.execute("await boom()")
        assert r.error_type == "LookupError" and "secret" not in (r.error or "")

    async def test_the_tool_budget_error_type(self) -> None:
        async with session(max_tool_calls=1) as s:
            r = await s.execute("await add(1, 1); await add(1, 1)")
        assert r.error_type == "ToolBudgetError"

    async def test_output_before_a_failure_is_kept(self) -> None:
        async with session() as s:
            r = await s.execute("console.log('before'); console.error('oops'); null.x")
        assert (r.stdout, r.stderr, r.error_type) == ("before\n", "oops\n", "TypeError")

    async def test_output_over_the_cap_is_truncated(self) -> None:
        async with session(max_output_bytes=100) as s:
            r = await s.execute(
                "for (let i = 0; i < 1000; i++) console.log('line ' + i); return 'ok'"
            )
            assert (r.status, r.result, r.truncated) == ("Succeeded", "ok", True)
            assert r.stdout.endswith("\n" + TRUNCATED_MARKER + "\n")
            assert len(r.stdout.encode()) <= 100 + len(TRUNCATED_MARKER) + 2
            assert (await s.execute("console.log('fresh')")).stdout == "fresh\n"

    async def test_the_default_output_cap(self) -> None:
        async with session() as s:
            r = await s.execute("console.log('x'.repeat(70000))")
        body = r.stdout.removesuffix(TRUNCATED_MARKER + "\n").removesuffix("\n")
        assert r.truncated and len(body) == DEFAULT_MAX_OUTPUT_BYTES

    async def test_a_result_over_the_cap_fails_the_run_not_the_session(self) -> None:
        async with session(max_result_bytes=1000) as s:
            r = await s.execute("console.log('made it'); return 'x'.repeat(2000)")
            assert (r.status, r.error_type, r.stdout) == (
                "Failed",
                "ResultTooLarge",
                "made it\n",
            )
            assert "max_result_bytes=1000" in (r.error or "")
            assert not s.is_closed()
            with pytest.raises(ResultTooLarge):
                await s.run("return 'x'.repeat(2000)")
            assert await s.run("return await add(1, 2)") == 3

    async def test_a_timeout_is_a_failed_result(self) -> None:
        async with session(timeout=0.5) as s:
            r = await s.execute("console.log('spin'); while (true) {}")
            assert (r.status, r.error_type, r.stdout) == (
                "Failed",
                "RuntimeTimeout",
                "spin\n",
            )
            assert s.is_closed()
            with pytest.raises(RuntimeError, match="gone"):
                await s.execute("return 1")

    async def test_cancelling_execute_kills_the_worker(self) -> None:
        async with session() as s:
            task = asyncio.create_task(s.execute("while (true) {}"))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert s.is_closed()

    async def test_limits_are_validated(self) -> None:
        with pytest.raises(ValueError, match="max_output_bytes"):
            AsyncAgentSandbox({}, max_output_bytes=0)
        with pytest.raises(ValueError, match="max_result_bytes"):
            AsyncAgentSandbox({}, max_result_bytes=-1)
        with pytest.raises(TypeError, match="capture_console"):
            AsyncAgentSandbox({}, capture_console=True)


class TestSteps:
    async def test_done_carries_the_output_of_the_whole_run_across_pauses(self) -> None:
        async with session() as s:
            step = await s.start(
                "console.log('a'); const x = await add(1, 2); console.log('b', x); "
                "console.error('e'); return x"
            )
            assert isinstance(step, ToolCall)
            done = await s.resume(step, 3)
        assert isinstance(done, Done)
        assert (done.stdout, done.stderr, done.truncated) == ("a\nb 3\n", "e\n", False)
        assert done.to_result() == ExecutionResult("Succeeded", "a\nb 3\n", "e\n", 3)

    async def test_failed_carries_output_and_an_error_type(self) -> None:
        async with session() as s:
            step = await s.start(
                "console.log('x'); await add(1, 2); throw new TypeError('t')"
            )
            failed = await s.resume(step, 3)
        assert isinstance(failed, Failed)
        assert (failed.stdout, failed.error_type) == ("x\n", "TypeError")

    async def test_a_too_large_result_is_a_failed_step(self) -> None:
        async with session(max_result_bytes=10) as s:
            step = await s.start("return 'abcdefghijklmnop'")
        assert isinstance(step, Failed) and isinstance(step.error, ResultTooLarge)

    async def test_the_callers_own_on_console_still_sees_everything(self) -> None:
        seen: list[tuple[str, list[Any]]] = []
        config = RuntimeConfig(
            on_console=lambda level, args: seen.append((level, args))
        )
        async with session(config=config) as s:
            r = await s.execute("console.log('one', 2); console.warn('three')")
        assert r.stdout == "one 2\n" and r.stderr == "three\n"
        assert seen == [("log", ["one", 2]), ("warn", ["three"])]

    async def test_other_runtime_config_settings_survive(self) -> None:
        config = RuntimeConfig(bootstrap="globalThis.fromBootstrap = 41;")
        async with session(config=config) as s:
            assert await s.run("return fromBootstrap + 1") == 42

    def test_a_snapshot_is_refused(self) -> None:
        from pydeno import SnapshotBuilder

        with pytest.raises(ValueError, match="snapshot"):
            AsyncAgentSandbox(
                {}, config=RuntimeConfig(snapshot=SnapshotBuilder().build())
            )


class TestJournal:
    async def test_console_output_does_not_affect_replay(self) -> None:
        async with session() as s:
            await s.run("console.log('a'); globalThis.v = await add(1, 2)")
            blob = await s.dump(KEY)
        t = await AsyncAgentSandbox.load(blob, KEY, {"add": add})
        try:
            assert await t.execute("console.log(v); return v") == ExecutionResult(
                "Succeeded", "3\n", "", 3
            )
        finally:
            await t.close()

    async def test_the_result_cap_is_recorded_replayed_and_shared_with_sync(
        self,
    ) -> None:
        async with session(max_result_bytes=50) as s:
            r = await s.execute("return 'x'.repeat(100)")
            assert r.error_type == "ResultTooLarge"
            await s.run("globalThis.k = 1")
            blob = await s.dump(KEY)
        assert json.loads(_open(blob, KEY))["config"]["max_result_bytes"] == 50

        def sync_side() -> str | None:
            with AgentSandbox.load(blob, KEY, {"add": add}) as t:
                assert t.run("return k") == 1
                return t.execute("return 'x'.repeat(100)").error_type

        assert await asyncio.to_thread(sync_side) == "ResultTooLarge"
        with pytest.raises(TypeError, match="journal"):
            await AsyncAgentSandbox.load(
                blob, KEY, {"add": add}, max_result_bytes=10**7
            )
