"""`ExecutionResult`: bounded console capture, a JSON-able result and a stable `error_type`.

Issue #11. One result shape for `AgentSandbox.execute`, `Done`/`Failed` (`to_result()`), and
`IsolatedRuntime.execute`/`execute_async`: ``{status, stdout, stderr, result, error, error_type,
truncated}``. Everything in it is sized by the guest, so the caps are tested at their edges.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from pydeno import (
    ExecutionResult,
    IsolatedRuntime,
    ResultTooLarge,
    RuntimeConfig,
    RuntimeTimeout,
    undefined,
)
from pydeno._agent import AgentSandbox, Done, Failed, ToolCall
from pydeno._result import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    TRUNCATED_MARKER,
    OutputCapture,
    bounded_result,
    error_type,
    format_console_arg,
    to_jsonable,
)

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def session(tools: dict[str, Any] | None = None, **kwargs: Any) -> AgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 7)
    return AgentSandbox(
        tools if tools is not None else {"add": lambda a, b: a + b}, **kwargs
    )


# ---------------------------------------------------------------------------
# the capture itself (no worker)
# ---------------------------------------------------------------------------


class TestOutputCapture:
    def test_levels_split_into_stdout_and_stderr_in_call_order(self) -> None:
        c = OutputCapture()
        for level in ("log", "warn", "info", "error", "debug", "trace"):
            c(level, [level])
        assert c.stdout == "log\ninfo\ndebug\n"
        assert c.stderr == "warn\nerror\ntrace\n"
        assert not c.truncated

    def test_arguments_are_joined_like_a_javascript_console(self) -> None:
        c = OutputCapture()
        c(
            "log",
            [
                "a",
                1,
                1.5,
                2.0,
                True,
                None,
                undefined,
                float("nan"),
                float("-inf"),
                {"k": [1, "x"]},
            ],
        )
        assert c.stdout == 'a 1 1.5 2 true null undefined NaN -Infinity {"k":[1,"x"]}\n'

    def test_a_stream_is_capped_and_ends_with_a_marker(self) -> None:
        c = OutputCapture(max_output_bytes=10)
        c("log", ["12345"])  # 6 bytes with the newline
        c("log", ["67890"])  # only 4 of these 6 fit
        c("log", ["never formatted"])
        assert c.stdout == "12345\n6789\n" + TRUNCATED_MARKER + "\n"
        assert c.truncated
        assert c.stderr == ""  # the other stream has its own budget

    def test_the_streams_have_separate_budgets(self) -> None:
        c = OutputCapture(max_output_bytes=4)
        c("log", ["abcdefgh"])
        c("error", ["ok"])
        assert c.stdout.endswith(TRUNCATED_MARKER + "\n")
        assert c.stderr == "ok\n"

    def test_the_cap_is_in_utf8_bytes_and_never_splits_a_character(self) -> None:
        c = OutputCapture(max_output_bytes=5)
        c("log", ["ééé"])  # 6 bytes + newline
        text = c.stdout
        assert text == "éé\n" + TRUNCATED_MARKER + "\n"
        body = text.removesuffix(TRUNCATED_MARKER + "\n").removesuffix("\n")
        assert len(body.encode()) <= 5

    def test_after_the_cap_nothing_more_is_formatted(self) -> None:
        class Exploding:
            def __repr__(self) -> str:  # pragma: no cover - must not be reached
                raise AssertionError("formatted after the cap")

        c = OutputCapture(max_output_bytes=1)
        c("log", ["xx"])
        c("log", [Exploding()])
        assert c.truncated

    def test_an_unformattable_argument_does_not_break_the_capture(self) -> None:
        c = OutputCapture()
        c("log", [object()])
        assert c.stdout == '"<object>"\n'

    @pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10", None])
    def test_the_cap_must_be_a_positive_int(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="max_output_bytes"):
            OutputCapture(bad)

    def test_format_console_arg_handles_negative_zero_and_big_ints(self) -> None:
        assert format_console_arg(-0.0) == "-0"
        assert format_console_arg(0.0) == "0"
        assert format_console_arg(2**70) == str(2**70)


class TestJsonable:
    def test_values_become_plain_json(self) -> None:
        value = {
            "u": undefined,
            "b": b"\x00\x01",
            "d": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "naive": datetime(2026, 1, 2),
            "s": {3, 1, 2},
            "t": (1, 2),
            "n": float("inf"),
            1: "int key",
        }
        data = to_jsonable(value)
        assert data == {
            "u": None,
            "b": "AAE=",
            "d": "2026-01-02T00:00:00+00:00",
            "naive": "2026-01-02T00:00:00+00:00",
            "s": [1, 2, 3],
            "t": [1, 2],
            "n": None,
            "1": "int key",
        }
        json.dumps(data, allow_nan=False)

    def test_bounded_result_measures_compact_json(self) -> None:
        assert bounded_result("x" * 8, 10) == "x" * 8  # '"xxxxxxxx"' is 10 bytes
        with pytest.raises(ResultTooLarge, match="max_result_bytes=10"):
            bounded_result("x" * 9, 10)

    def test_a_too_deep_value_is_too_large_not_a_crash(self) -> None:
        deep: Any = []
        for _ in range(300):
            deep = [deep]
        with pytest.raises(ResultTooLarge):
            bounded_result(deep, DEFAULT_MAX_RESULT_BYTES)


# ---------------------------------------------------------------------------
# AgentSandbox
# ---------------------------------------------------------------------------


class TestAgentExecute:
    def test_a_successful_run(self) -> None:
        with session() as s:
            r = s.execute(
                "console.log('a', {x: 1}); console.warn('careful'); console.info('b');"
                "return await add(2, 3)"
            )
        assert r == ExecutionResult(
            status="Succeeded",
            stdout='a {"x":1}\nb\n',
            stderr="careful\n",
            result=5,
        )
        assert r.ok
        assert json.loads(json.dumps(r.to_dict())) == r.to_dict()

    def test_output_is_per_run(self) -> None:
        with session() as s:
            assert s.execute("console.log('one'); return 1").stdout == "one\n"
            assert s.execute("return 2").stdout == ""

    def test_a_run_that_returns_nothing_has_a_null_result(self) -> None:
        with session() as s:
            r = s.execute("console.log('hi')")
        assert r.status == "Succeeded" and r.result is None and r.stdout == "hi\n"

    def test_results_are_json_data(self) -> None:
        with session() as s:
            r = s.execute(
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
        ("code", "kind", "message"),
        [
            ("null.x", "TypeError", "TypeError: Cannot read properties of null"),
            ("nope", "ReferenceError", "ReferenceError: nope is not defined"),
            ("throw new RangeError('r')", "RangeError", "RangeError: r"),
            ("throw new SyntaxError('s')", "SyntaxError", "SyntaxError: s"),
            ("throw new Error('plain')", "Error", "Error: plain"),
            ("throw 5", "Error", "Error"),
            (
                "class QuotaError extends Error { constructor(m) { super(m); "
                "this.name = 'QuotaError'; } }; throw new QuotaError('q')",
                "QuotaError",
                "QuotaError: q",
            ),
        ],
        ids=["type", "reference", "range", "syntax", "plain", "non-error", "custom"],
    )
    def test_guest_errors_keep_their_javascript_name(
        self, code: str, kind: str, message: str
    ) -> None:
        with session() as s:
            r = s.execute(code)
            assert r.status == "Failed"
            assert r.error_type == kind
            assert r.error is not None and r.error.startswith(message)
            assert not r.error.startswith("Evaluation failed")
            assert r.result is None
            assert not s.is_closed()  # a guest error leaves the session usable

    def test_a_syntax_error_in_the_code_is_a_syntax_error(self) -> None:
        with session() as s:
            assert s.execute("return )(").error_type == "SyntaxError"

    @pytest.mark.parametrize(
        "name", ["RuntimeTimeout", "WorkerCrashed", "ResultTooLarge", "JournalError"]
    )
    def test_the_guest_cannot_claim_a_host_failure(self, name: str) -> None:
        with session() as s:
            r = s.execute(f"const e = new Error('fake'); e.name = {name!r}; throw e")
        assert r.error_type == "Error"

    def test_a_tool_error_the_guest_does_not_catch_keeps_its_name(self) -> None:
        def boom() -> None:
            raise LookupError("secret path /etc/x")

        with session({"boom": boom}) as s:
            r = s.execute("await boom()")
        assert r.error_type == "LookupError"
        assert "secret" not in (r.error or "")  # redacted by default

    def test_the_tool_budget_error_type(self) -> None:
        with session(max_tool_calls=1) as s:
            r = s.execute("await add(1, 1); await add(1, 1)")
        assert r.error_type == "ToolBudgetError"

    def test_console_output_before_a_failure_is_kept(self) -> None:
        with session() as s:
            r = s.execute("console.log('before'); console.error('oops'); null.x")
        assert (r.stdout, r.stderr, r.error_type) == ("before\n", "oops\n", "TypeError")

    def test_output_over_the_cap_is_truncated_with_a_marker(self) -> None:
        with session(max_output_bytes=100) as s:
            r = s.execute(
                "for (let i = 0; i < 1000; i++) console.log('line ' + i); return 'ok'"
            )
            assert r.status == "Succeeded" and r.result == "ok"
            assert r.truncated
            assert r.stdout.endswith("\n" + TRUNCATED_MARKER + "\n")
            assert len(r.stdout.encode()) <= 100 + len(TRUNCATED_MARKER) + 2
            assert r.stdout.startswith("line 0\nline 1\n")
            # The next run starts with a fresh budget.
            assert s.execute("console.log('fresh')").stdout == "fresh\n"

    def test_the_default_output_cap_is_64_kib(self) -> None:
        assert DEFAULT_MAX_OUTPUT_BYTES == 64 * 1024
        with session() as s:
            r = s.execute("console.log('x'.repeat(70000))")
        assert r.truncated
        body = r.stdout.removesuffix(TRUNCATED_MARKER + "\n").removesuffix("\n")
        assert len(body) == 64 * 1024

    def test_a_result_over_the_cap_fails_the_run_not_the_session(self) -> None:
        with session(max_result_bytes=1000) as s:
            r = s.execute("console.log('made it'); return 'x'.repeat(2000)")
            assert r.status == "Failed"
            assert r.error_type == "ResultTooLarge"
            assert "max_result_bytes=1000" in (r.error or "")
            assert r.stdout == "made it\n"
            assert not s.is_closed()
            # State from before is intact and the session keeps working.
            assert s.execute("return 'x'.repeat(900)").status == "Succeeded"
            with pytest.raises(ResultTooLarge):
                s.run("return 'x'.repeat(2000)")
            assert s.run("return await add(1, 2)") == 3

    def test_the_default_result_cap_is_1_mib(self) -> None:
        assert DEFAULT_MAX_RESULT_BYTES == 1024 * 1024
        with session() as s:
            assert s.execute("return 'x'.repeat(1024 * 1024 - 2)").ok
            assert s.execute("return 'x'.repeat(1024 * 1024)").error_type == (
                "ResultTooLarge"
            )

    def test_a_timeout_is_a_failed_result_with_a_host_error_type(self) -> None:
        with session(timeout=0.5) as s:
            r = s.execute("console.log('spin'); while (true) {}")
            assert r.status == "Failed"
            assert r.error_type == "RuntimeTimeout"
            assert r.stdout == "spin\n"
            assert s.is_closed()
            with pytest.raises(RuntimeError, match="gone"):
                s.execute("return 1")

    def test_limits_are_validated(self) -> None:
        with pytest.raises(ValueError, match="max_output_bytes"):
            AgentSandbox({}, max_output_bytes=0)
        with pytest.raises(ValueError, match="max_result_bytes"):
            AgentSandbox({}, max_result_bytes=-1)
        with pytest.raises(TypeError, match="capture_console"):
            AgentSandbox({}, capture_console=True)


class TestSteps:
    def test_done_carries_the_output_of_the_whole_run_across_pauses(self) -> None:
        with session() as s:
            step = s.start(
                "console.log('a'); const x = await add(1, 2); console.log('b', x); "
                "console.error('e'); return x"
            )
            assert isinstance(step, ToolCall)
            done = s.resume(step, 3)
        assert isinstance(done, Done)
        assert (done.stdout, done.stderr, done.truncated) == ("a\nb 3\n", "e\n", False)
        assert done.status == "Succeeded" and done.error_type is None
        assert done.result == 3
        assert done.to_result() == ExecutionResult("Succeeded", "a\nb 3\n", "e\n", 3)

    def test_failed_carries_output_and_an_error_type(self) -> None:
        with session() as s:
            step = s.start(
                "console.log('x'); await add(1, 2); throw new TypeError('t')"
            )
            failed = s.resume(step, 3)
        assert isinstance(failed, Failed)
        assert failed.stdout == "x\n"
        assert failed.status == "Failed" and failed.result is None
        assert failed.error_type == "TypeError"
        r = failed.to_result()
        assert (r.status, r.error, r.error_type) == (
            "Failed",
            "TypeError: t",
            "TypeError",
        )

    def test_a_too_large_result_is_a_failed_step(self) -> None:
        with session(max_result_bytes=10) as s:
            step = s.start("return 'abcdefghijklmnop'")
        assert isinstance(step, Failed)
        assert isinstance(step.error, ResultTooLarge)
        assert step.error_type == "ResultTooLarge"

    def test_output_does_not_take_part_in_equality(self) -> None:
        with session() as s:
            assert s.start("console.log('noise'); return 1") == Done(1)

    def test_the_callers_own_on_console_still_sees_everything(self) -> None:
        seen: list[tuple[str, list[Any]]] = []
        config = RuntimeConfig(
            on_console=lambda level, args: seen.append((level, args))
        )
        with session(config=config) as s:
            r = s.execute("console.log('one', 2); console.warn('three')")
        assert r.stdout == "one 2\n" and r.stderr == "three\n"
        assert seen == [("log", ["one", 2]), ("warn", ["three"])]

    def test_other_runtime_config_settings_survive(self) -> None:
        config = RuntimeConfig(bootstrap="globalThis.fromBootstrap = 41;")
        with session(config=config) as s:
            assert s.run("return fromBootstrap + 1") == 42

    def test_a_snapshot_is_still_refused(self) -> None:
        from pydeno import SnapshotBuilder

        snapshot = SnapshotBuilder().build()
        with pytest.raises(ValueError, match="snapshot"):
            AgentSandbox({}, config=RuntimeConfig(snapshot=snapshot))


class TestJournal:
    def test_console_output_does_not_affect_replay(self) -> None:
        with session() as s:
            s.run("console.log('a'); globalThis.v = await add(1, 2)")
            blob = s.dump(KEY)
        with AgentSandbox.load(blob, KEY, {"add": lambda a, b: a + b}) as t:
            assert t.execute("console.log(v); return v") == ExecutionResult(
                "Succeeded", "3\n", "", 3
            )

    def test_a_default_cap_writes_the_same_journal_as_before(self) -> None:
        from pydeno._agent import _open

        with session() as s:
            s.run("return 1")
            config = json.loads(_open(s.dump(KEY), KEY))["config"]
        assert "max_result_bytes" not in config
        assert "catalog" not in config

    def test_the_result_cap_is_recorded_and_replayed(self) -> None:
        with session(max_result_bytes=50) as s:
            assert s.execute("return 'x'.repeat(100)").error_type == "ResultTooLarge"
            s.run("globalThis.k = 1")
            blob = s.dump(KEY)
        with AgentSandbox.load(blob, KEY, {"add": lambda a, b: a + b}) as t:
            assert t.run("return k") == 1
            assert t.execute("return 'x'.repeat(100)").error_type == "ResultTooLarge"
        with pytest.raises(TypeError, match="journal"):
            AgentSandbox.load(
                blob, KEY, {"add": lambda a, b: a + b}, max_result_bytes=10**7
            )


# ---------------------------------------------------------------------------
# IsolatedRuntime
# ---------------------------------------------------------------------------


class TestIsolatedRuntime:
    def test_execute_captures_console_output_when_asked(self) -> None:
        with IsolatedRuntime(capture_console=True) as rt:
            r = rt.execute("console.log('hi'); console.error('err'); ({a: [1, 2]})")
            assert r == ExecutionResult("Succeeded", "hi\n", "err\n", {"a": [1, 2]})
            # Output outside execute() is not collected anywhere.
            rt.eval("console.log('ignored')")
            assert rt.execute("1").stdout == ""

    def test_without_capture_the_streams_are_empty(self) -> None:
        with IsolatedRuntime() as rt:
            r = rt.execute("console.log('hi'); 2")
        assert r == ExecutionResult("Succeeded", "", "", 2)

    def test_on_console_and_execute_compose(self) -> None:
        seen: list[Any] = []
        config = RuntimeConfig(on_console=lambda level, args: seen.append(args))
        with IsolatedRuntime(config) as rt:
            r = rt.execute("console.log('both'); 0")
            rt.eval("console.log('only the callback')")
        assert r.stdout == "both\n"
        assert seen == [["both"], ["only the callback"]]

    def test_errors_are_results(self) -> None:
        with IsolatedRuntime(capture_console=True) as rt:
            r = rt.execute("console.log('x'); undefinedThing")
            assert (r.status, r.error_type, r.stdout) == (
                "Failed",
                "ReferenceError",
                "x\n",
            )
            assert rt.execute("'x'.repeat(100)", max_result_bytes=50).error_type == (
                "ResultTooLarge"
            )
            r = rt.execute(
                "for (let i = 0; i < 100; i++) console.log(i); 1", max_output_bytes=20
            )
            assert r.truncated and r.result == 1

    def test_a_timeout_is_a_result(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=0.3)) as rt:
            r = rt.execute("while (true) {}")
        assert r.status == "Failed"
        assert r.error_type in ("RuntimeTimeout", "RuntimeTerminated")

    def test_execute_async_awaits_promises(self) -> None:
        async def main() -> ExecutionResult:
            with IsolatedRuntime(capture_console=True) as rt:
                return await rt.execute_async(
                    "(async () => { console.warn('w'); return await Promise.resolve(9); })()"
                )

        assert asyncio.run(main()) == ExecutionResult("Succeeded", "", "w\n", 9)

    def test_execute_async_reports_rejections(self) -> None:
        async def main() -> ExecutionResult:
            with IsolatedRuntime() as rt:
                return await rt.execute_async("Promise.reject(new TypeError('no'))")

        r = asyncio.run(main())
        assert (r.status, r.error_type) == ("Failed", "TypeError")

    def test_limits_are_validated(self) -> None:
        with IsolatedRuntime() as rt:
            with pytest.raises(ValueError, match="max_output_bytes"):
                rt.execute("1", max_output_bytes=0)
            with pytest.raises(ValueError, match="max_result_bytes"):
                rt.execute("1", max_result_bytes=0)


def test_error_type_of_host_exceptions_is_the_class_name() -> None:
    assert error_type(RuntimeTimeout("x")) == "RuntimeTimeout"
    assert error_type(ResultTooLarge("x")) == "ResultTooLarge"
    assert error_type(ValueError("x")) == "ValueError"
