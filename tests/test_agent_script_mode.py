"""`AgentSandbox(mode="script")` (#144): the code is a script, its last expression is the result."""

from __future__ import annotations

import json

import pytest

from pydeno import AgentSandbox, AsyncAgentSandbox, Done, Failed, JournalError
from pydeno._agent import _open, _seal, describe_tools

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


TOOLS = {"add": add}


def test_default_is_still_an_async_function_body() -> None:
    with AgentSandbox(TOOLS) as s:
        assert s.run("return 1 + 1") == 2
        assert s.execute("1 + 1").result is None  # no `return`: undefined, as before


def test_last_expression_is_the_result() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        assert s.run("1 + 2") == 3
        assert s.run("const a = 4; a * 2") == 8


def test_a_promise_result_is_awaited_and_tools_work() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        assert s.run("(async () => await add(1, 2))()") == 3
        assert s.run("add(2, 3)") == 5  # a Promise from a tool


def test_an_unawaited_tool_call_still_finishes_before_the_run_ends() -> None:
    calls: list[int] = []

    def note(n: int) -> None:
        calls.append(n)

    with AgentSandbox({"note": note}, mode="script") as s:
        assert s.run("note(1); 7") == 7
        assert calls == [1]


def test_var_and_function_persist_but_const_does_not() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        s.run("var kept = 5; function twice(x) { return x * 2 } let gone = 1; 0")
        assert s.run("twice(kept)") == 10
        assert s.run("typeof gone") == "undefined"


def test_top_level_return_is_a_syntax_error() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        result = s.execute("return 1")
        assert result.status == "Failed"
        assert "SyntaxError" in str(result.error)
        assert s.run("1") == 1  # the session survives


def test_start_resume_pausing_works() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        step = s.start("(async () => (await add(1, 2)) + 1)()")
        assert not isinstance(step, (Done, Failed))
        assert s.resume(step, 10) == Done(11)


def test_a_thrown_error_is_a_failed_run() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        assert isinstance(s.start("throw new Error('x')"), Failed)


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        AgentSandbox(TOOLS, mode="module")


def test_script_mode_needs_eval() -> None:
    with pytest.raises(ValueError, match="strict_eval"):
        AgentSandbox(TOOLS, mode="script", strict_eval=True)


def test_prompt_block_describes_the_mode() -> None:
    assert "async function" in describe_tools(TOOLS)
    script = describe_tools(TOOLS, mode="script")
    assert "as a script" in script
    assert "body of an async function" not in script
    with AgentSandbox(TOOLS, mode="script") as s:
        assert s.describe_tools() == script


def test_journal_records_the_mode_and_load_takes_it_from_there() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        s.run("var x = 41; x")
        blob = s.dump(KEY)
    with AgentSandbox.load(blob, KEY, TOOLS) as loaded:
        assert loaded.run("x + 1") == 42  # replayed as a script, and still a script
        with pytest.raises(TypeError, match="comes from the journal"):
            AgentSandbox.load(blob, KEY, TOOLS, mode="function")


def test_a_default_session_journal_is_unchanged_and_loads_as_function() -> None:
    with AgentSandbox(TOOLS) as s:
        s.run("return 1")
        blob = s.dump(KEY)
    with AgentSandbox.load(blob, KEY, TOOLS) as loaded:
        assert loaded.run("return 2") == 2


def test_a_journal_with_a_bad_mode_is_refused() -> None:
    with AgentSandbox(TOOLS, mode="script") as s:
        blob = s.dump(KEY)
    journal = json.loads(_open(blob, KEY))
    journal["config"]["mode"] = "bogus"
    forged = _seal(json.dumps(journal).encode(), KEY)
    with pytest.raises(JournalError, match="mode"):
        AgentSandbox.load(forged, KEY, TOOLS)


async def test_async_variant() -> None:
    async with AsyncAgentSandbox(TOOLS, mode="script") as s:
        assert await s.run("1 + 2") == 3
        assert await s.run("(async () => await add(2, 2))()") == 4
        blob = await s.dump(KEY)
    async with await AsyncAgentSandbox.load(blob, KEY, TOOLS) as loaded:
        assert await loaded.run("1 + 1") == 2


async def test_async_default_unchanged() -> None:
    async with AsyncAgentSandbox(TOOLS) as s:
        assert await s.run("return 5") == 5
