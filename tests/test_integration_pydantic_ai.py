"""`pydeno.integrations.pydantic_ai`: JSCodeMode, its toolset, and the schema -> .d.ts converter.

Every agent here runs on pydantic-ai's `FunctionModel` (a scripted "model": each step is the
JavaScript it submits) or `TestModel`, so nothing needs an API key or the network. The sandbox is
a real `IsolatedRuntime`, so these are end-to-end: model -> run_javascript -> worker -> nested
ToolManager -> tool -> back into the worker -> back to the model.

pydantic-ai is imported lazily (inside fixtures and helpers), so where it is not installed this
module still collects and the `needs_pydantic_ai` marker deselects it.
"""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import threading
import time
import warnings
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = [pytest.mark.needs_pydantic_ai, pytest.mark.full_sandbox]

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

TOOL = "run_javascript"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pai() -> SimpleNamespace:
    """Everything the tests use from pydantic-ai and the integration, imported on demand."""
    import pydantic_ai
    import pydantic_ai.capabilities
    import pydantic_ai.exceptions as exc
    import pydantic_ai.messages as msg
    import pydantic_ai.models.function
    import pydantic_ai.models.test
    import pydantic_ai.tools
    import pydantic_ai.usage

    from pydeno.integrations import pydantic_ai as integration

    return SimpleNamespace(
        Agent=pydantic_ai.Agent,
        Tool=pydantic_ai.Tool,
        HandleDeferredToolCalls=pydantic_ai.capabilities.HandleDeferredToolCalls,
        ModelRetry=exc.ModelRetry,
        UnexpectedModelBehavior=exc.UnexpectedModelBehavior,
        UsageLimitExceeded=exc.UsageLimitExceeded,
        UserError=exc.UserError,
        ModelResponse=msg.ModelResponse,
        RetryPromptPart=msg.RetryPromptPart,
        TextPart=msg.TextPart,
        ToolCallPart=msg.ToolCallPart,
        ToolReturnPart=msg.ToolReturnPart,
        FunctionModel=pydantic_ai.models.function.FunctionModel,
        TestModel=pydantic_ai.models.test.TestModel,
        DeferredToolResults=pydantic_ai.tools.DeferredToolResults,
        ToolDenied=pydantic_ai.tools.ToolDenied,
        UsageLimits=pydantic_ai.usage.UsageLimits,
        integration=integration,
    )


def scripted(pai: SimpleNamespace, *steps: Any, seen: list[Any] | None = None) -> Any:
    """A FunctionModel that submits each step in turn (a code string, or run_javascript args),
    then answers "done". `seen` collects (messages, info) for every model request."""
    queue = list(steps)

    def respond(messages: list[Any], info: Any) -> Any:
        if seen is not None:
            seen.append((list(messages), info))
        if queue:
            step = queue.pop(0)
            if isinstance(step, str):
                step = {"code": step}
            if isinstance(step, dict):
                return pai.ModelResponse(parts=[pai.ToolCallPart(TOOL, step)])
            return step
        return pai.ModelResponse(parts=[pai.TextPart("done")])

    return pai.FunctionModel(respond)


def outcomes(pai: SimpleNamespace, result: Any) -> list[Any]:
    """Every run_javascript outcome in order: ToolReturnPart or RetryPromptPart."""
    found = []
    for message in result.all_messages():
        for part in message.parts:
            if (
                isinstance(part, (pai.ToolReturnPart, pai.RetryPromptPart))
                and part.tool_name == TOOL
            ):
                found.append(part)
    return found


def returns(pai: SimpleNamespace, result: Any) -> list[Any]:
    return [
        p.content for p in outcomes(pai, result) if isinstance(p, pai.ToolReturnPart)
    ]


def retries(pai: SimpleNamespace, result: Any) -> list[str]:
    return [
        p.content for p in outcomes(pai, result) if isinstance(p, pai.RetryPromptPart)
    ]


def weather_agent(pai: SimpleNamespace, model: Any, **options: Any) -> Any:
    agent = pai.Agent(model, capabilities=[pai.integration.JSCodeMode(**options)])

    @agent.tool_plain
    async def get_weather(city: str, units: str = "c") -> dict[str, Any]:
        """Current weather for a city."""
        return {"city": city, "temp": 21.5, "units": units}

    return agent


@pytest.fixture(autouse=True)
def _quiet_return_schema_warnings() -> Any:
    from pydeno.integrations.pydantic_ai import JSCodeModeReturnSchemaWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", JSCodeModeReturnSchemaWarning)
        yield


# ---------------------------------------------------------------------------
# 1. what the model sees
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_model_sees_run_javascript_plus_unselected_native_tools(
        self, pai: SimpleNamespace
    ) -> None:
        seen: list[Any] = []
        agent = pai.Agent(
            scripted(pai, seen=seen),
            capabilities=[pai.integration.JSCodeMode(tools=["get_weather"])],
        )

        @agent.tool_plain
        def get_weather(city: str) -> float:
            """Temperature in a city."""
            return 1.0

        @agent.tool_plain
        def send_email(to: str) -> str:
            return "sent"

        agent.run_sync("hi")
        info = seen[0][1]
        names = sorted(t.name for t in info.function_tools)
        assert names == [TOOL, "send_email"]
        run_js = next(t for t in info.function_tools if t.name == TOOL)
        assert "declare namespace tools {" in run_js.description
        assert (
            "function get_weather(args: {\n    city: string;\n  }): Promise<number>;"
            in (run_js.description)
        )
        assert "/** Temperature in a city. */" in run_js.description
        assert "send_email" not in run_js.description
        assert run_js.sequential is True
        assert run_js.metadata == {
            "code_arg_name": "code",
            "code_arg_language": "javascript",
        }
        assert set(run_js.parameters_json_schema["properties"]) == {"code", "restart"}
        assert run_js.parameters_json_schema["required"] == ["code"]

    def test_dynamic_catalog_moves_declarations_into_instructions(
        self, pai: SimpleNamespace
    ) -> None:
        seen: list[Any] = []
        agent = weather_agent(pai, scripted(pai, seen=seen), dynamic_catalog=True)
        agent.run_sync("hi")
        messages, info = seen[0]
        run_js = next(t for t in info.function_tools if t.name == TOOL)
        assert "declare namespace" not in run_js.description
        instructions = messages[0].instructions or ""
        assert "declare namespace tools" in instructions
        assert "function get_weather" in instructions

    def test_a_user_tool_cannot_take_the_reserved_name(
        self, pai: SimpleNamespace
    ) -> None:
        agent = pai.Agent(scripted(pai), capabilities=[pai.integration.JSCodeMode()])

        @agent.tool_plain(name=TOOL)
        def clash() -> str:
            return "x"

        with pytest.raises(pai.UserError, match="reserved"):
            agent.run_sync("hi")

    def test_missing_return_schema_warns_once(self, pai: SimpleNamespace) -> None:
        capability = pai.integration.JSCodeMode()
        agent = pai.Agent(scripted(pai), capabilities=[capability])

        @agent.tool_plain
        def untyped(x: int):  # noqa: ANN202 - the point: no return annotation
            return x

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter(
                "always", pai.integration.JSCodeModeReturnSchemaWarning
            )
            agent.run_sync("one")
            agent.run_sync("two")
        relevant = [
            w
            for w in caught
            if issubclass(w.category, pai.integration.JSCodeModeReturnSchemaWarning)
        ]
        assert len(relevant) == 1
        assert "'untyped'" in str(relevant[0].message)

    def test_defer_approvals_are_not_implemented(self, pai: SimpleNamespace) -> None:
        with pytest.raises(NotImplementedError, match="defer"):
            pai.integration.JSCodeMode(approvals="defer")
        with pytest.raises(ValueError, match="approvals"):
            pai.integration.JSCodeMode(approvals="maybe")  # type: ignore[arg-type]

    def test_owned_runtime_options_are_refused(self, pai: SimpleNamespace) -> None:
        with pytest.raises(pai.UserError, match="request_timeout"):
            pai.integration.JSCodeMode(runtime_options={"request_timeout": 5})


# ---------------------------------------------------------------------------
# 2. parallel calls, ids, usage
# ---------------------------------------------------------------------------


class TestParallelCalls:
    def test_promise_all_runs_tools_concurrently_and_records_them(
        self, pai: SimpleNamespace
    ) -> None:
        spans: dict[str, tuple[float, float]] = {}
        agent = pai.Agent(
            scripted(
                pai,
                "const [a, b] = await Promise.all([tools.slow({tag: 'a'}), tools.slow({tag: 'b'})]);"
                "\nreturn a + b",
            ),
            capabilities=[pai.integration.JSCodeMode()],
        )

        @agent.tool_plain
        async def slow(tag: str) -> str:
            start = time.monotonic()
            await asyncio.sleep(0.3)
            spans[tag] = (start, time.monotonic())
            return tag.upper()

        result = agent.run_sync("hi")
        (ret,) = [p for p in outcomes(pai, result) if isinstance(p, pai.ToolReturnPart)]
        assert ret.content == "AB"
        # Two nested calls plus run_javascript itself.
        assert result.usage.tool_calls == 3
        parent = ret.tool_call_id
        meta = ret.metadata
        assert meta["code_mode"] is True and meta["language"] == "javascript"
        assert sorted(meta["tool_calls"]) == [f"{parent}__1", f"{parent}__2"]
        assert {c.args["tag"] for c in meta["tool_calls"].values()} == {"a", "b"}
        assert {r.content for r in meta["tool_returns"].values()} == {"A", "B"}
        assert meta["duration_ms"] >= 0
        (a0, a1), (b0, b1) = spans["a"], spans["b"]
        assert a0 < b1 and b0 < a1, "the two async tools did not overlap"

    def test_sequential_tools_run_alone(self, pai: SimpleNamespace) -> None:
        active = 0
        peak = 0
        agent = pai.Agent(
            scripted(
                pai,
                "await Promise.all([1, 2, 3].map((n) => tools.one({n}))); return 'ok'",
            ),
            capabilities=[pai.integration.JSCodeMode()],
        )

        @agent.tool_plain(sequential=True)
        async def one(n: int) -> int:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return n

        result = agent.run_sync("hi")
        assert returns(pai, result) == ["ok"]
        assert peak == 1

    def test_hyphenated_tool_names_are_mapped_and_dispatched_back(
        self, pai: SimpleNamespace
    ) -> None:
        def weather(city: str) -> str:
            return f"sunny in {city}"

        seen: list[Any] = []
        agent = pai.Agent(
            scripted(pai, "return await tools.get_weather({city: 'Oslo'})", seen=seen),
            tools=[pai.Tool(weather, name="get-weather")],
            capabilities=[pai.integration.JSCodeMode()],
        )
        result = agent.run_sync("hi")
        assert returns(pai, result) == ["sunny in Oslo"]
        (ret,) = [p for p in outcomes(pai, result) if isinstance(p, pai.ToolReturnPart)]
        assert [c.tool_name for c in ret.metadata["tool_calls"].values()] == [
            "get-weather"
        ]
        description = next(
            t for t in seen[0][1].function_tools if t.name == TOOL
        ).description
        assert "function get_weather(" in description and "`get-weather`" in description


# ---------------------------------------------------------------------------
# 3-4. syntax errors, nested ModelRetry, retry exhaustion
# ---------------------------------------------------------------------------


class TestErrors:
    def test_syntax_error_is_a_retry_and_nothing_runs(
        self, pai: SimpleNamespace
    ) -> None:
        calls: list[str] = []
        agent = pai.Agent(
            scripted(
                pai,
                "await tools.note({text: 'side effect'});\nconst x: number = 1;\nreturn x",
                "await tools.note({text: 'ok'}); return 2",
            ),
            capabilities=[pai.integration.JSCodeMode()],
        )

        @agent.tool_plain
        def note(text: str) -> str:
            calls.append(text)
            return text

        result = agent.run_sync("hi")
        (retry,) = retries(pai, result)
        assert retry.startswith("Syntax error:")
        assert "Nothing ran" in retry
        assert returns(pai, result) == [2]
        assert calls == ["ok"], "a snippet with a syntax error must not run at all"

    def test_runtime_syntax_error_is_not_mistaken_for_a_compile_error(
        self, pai: SimpleNamespace
    ) -> None:
        result = weather_agent(
            pai, scripted(pai, "JSON.parse('{')", "return 1")
        ).run_sync("hi")
        (retry,) = retries(pai, result)
        assert retry.startswith("Runtime error: SyntaxError")

    def test_nested_model_retry_can_be_caught_in_js(self, pai: SimpleNamespace) -> None:
        agent = pai.Agent(
            scripted(
                pai,
                "try { await tools.picky({n: 1}) } catch (e) { return [e.name, e.message] }",
                "await tools.picky({n: 1}); return 'unreachable'",
                "return await tools.picky({n: 2})",
            ),
            capabilities=[pai.integration.JSCodeMode()],
        )

        @agent.tool_plain
        def picky(n: int) -> int:
            if n != 2:
                raise pai.ModelRetry("n must be 2")
            return n

        result = agent.run_sync("hi")
        assert returns(pai, result) == [["ModelRetry", "n must be 2"], 2]
        (retry,) = retries(pai, result)
        assert retry.startswith("Runtime error: ModelRetry: n must be 2")
        assert "1 tool call started before the code stopped" in retry
        assert "picky({'n': 1}) failed: 'n must be 2'" in retry

    def test_retries_exhausted_end_the_run(self, pai: SimpleNamespace) -> None:
        agent = weather_agent(
            pai, scripted(pai, *["throw new Error('again')"] * 5), max_retries=2
        )
        with pytest.raises(pai.UnexpectedModelBehavior, match="exceeded max retries"):
            agent.run_sync("hi")

    def test_unexpected_tool_errors_are_hidden_unless_the_tool_opts_in(
        self, pai: SimpleNamespace
    ) -> None:
        code = (
            "const out = [];"
            "for (const t of [tools.secretive, tools.chatty]) {"
            "  try { await t({}) } catch (e) { out.push([e.name, e.message]) } }"
            "return out"
        )
        agent = pai.Agent(
            scripted(pai, code), capabilities=[pai.integration.JSCodeMode()]
        )

        @agent.tool_plain
        def secretive() -> str:
            raise KeyError("/etc/secret/path")

        @agent.tool_plain(metadata={"expose_errors": True})
        def chatty() -> str:
            raise LookupError("nothing for that key")

        result = agent.run_sync("hi")
        ((hidden, shown),) = returns(pai, result)
        assert hidden == ["KeyError", "tool 'secretive' failed (details are not shown)"]
        assert shown == ["LookupError", "nothing for that key"]

    def test_unreturnable_result_is_a_retry_and_the_session_survives(
        self, pai: SimpleNamespace
    ) -> None:
        agent = weather_agent(
            pai, scripted(pai, "globalThis.kept = 7; return () => 1", "return kept")
        )
        result = agent.run_sync("hi")
        (retry,) = retries(pai, result)
        assert "cannot be returned" in retry
        assert returns(pai, result) == [7]


# ---------------------------------------------------------------------------
# 5. bad arguments
# ---------------------------------------------------------------------------


class TestArguments:
    def test_bad_args_reach_js_as_validation_error_naming_the_field(
        self, pai: SimpleNamespace
    ) -> None:
        code = (
            "const out = [];"
            "try { await tools.get_weather({town: 'Paris'}) } catch (e) { out.push([e.name, e.message]) }"
            "try { await tools.get_weather('Paris') } catch (e) { out.push([e.name, e.message]) }"
            "out.push(await tools.get_weather({city: 'Paris', units: undefined}));"
            "return out"
        )
        result = weather_agent(pai, scripted(pai, code)).run_sync("hi")
        ((missing, positional, ok),) = returns(pai, result)
        assert missing[0] == "ValidationError"
        assert "city" in missing[1] and "Field required" in missing[1]
        assert positional[0] == "TypeError"
        assert "one object argument" in positional[1]
        # `undefined` properties are dropped, so the default applies.
        assert ok == {"city": "Paris", "temp": 21.5, "units": "c"}


# ---------------------------------------------------------------------------
# 6-7. budgets and usage limits
# ---------------------------------------------------------------------------


class TestBudgets:
    def test_per_snippet_budget(self, pai: SimpleNamespace) -> None:
        code = (
            "const got = [];"
            "for (const city of ['A', 'B', 'C']) {"
            "  try { got.push((await tools.get_weather({city})).city) }"
            "  catch (e) { got.push(e.name) } }"
            "return got"
        )
        agent = weather_agent(
            pai,
            scripted(
                pai,
                code,
                code,
                "for (const c of 'ABC') await tools.get_weather({city: c})",
            ),
            max_tool_calls=2,
        )
        result = agent.run_sync("hi")
        # The budget is per snippet: the second snippet gets two calls again.
        assert returns(pai, result) == [["A", "B", "ToolBudgetError"]] * 2
        (retry,) = retries(pai, result)
        assert retry.startswith("Runtime error: ToolBudgetError")
        assert "split the work" in retry
        assert "2 tool calls started" in retry

    def test_session_budget_spans_snippets(self, pai: SimpleNamespace) -> None:
        code = "try { await tools.get_weather({city: 'A'}); return 'ok' } catch (e) { return e.name }"
        agent = weather_agent(
            pai, scripted(pai, code, code, code), max_session_tool_calls=2
        )
        result = agent.run_sync("hi")
        assert returns(pai, result) == ["ok", "ok", "ToolBudgetError"]

    def test_usage_limit_ends_the_run_even_if_js_catches_it(
        self, pai: SimpleNamespace
    ) -> None:
        code = (
            "for (const city of ['A', 'B', 'C']) {"
            "  try { await tools.get_weather({city}) } catch (e) {} }"
            "return 'swallowed'"
        )
        agent = weather_agent(pai, scripted(pai, code))
        with pytest.raises(pai.UsageLimitExceeded, match="tool_calls_limit of 1"):
            agent.run_sync("hi", usage_limits=pai.UsageLimits(tool_calls_limit=1))


# ---------------------------------------------------------------------------
# 8-9. timeouts, resets, state
# ---------------------------------------------------------------------------


class TestSession:
    def test_runaway_code_is_stopped_and_the_next_snippet_gets_a_fresh_sandbox(
        self, pai: SimpleNamespace
    ) -> None:
        calls: list[str] = []
        agent = pai.Agent(
            scripted(
                pai,
                "const before = 1; return before",
                "await tools.mark({}); while (true) {}",
                "return [typeof before, await tools.mark({})]",
            ),
            capabilities=[pai.integration.JSCodeMode(timeout=1.0)],
        )

        @agent.tool_plain
        def mark() -> str:
            calls.append("mark")
            return "marked"

        started = time.monotonic()
        result = agent.run_sync("hi")
        assert time.monotonic() - started < 15
        (retry,) = retries(pai, result)
        assert "ran longer than the 1s limit" in retry
        assert "sandbox was reset" in retry
        assert "1 tool call started" in retry and "mark({}) returned 'marked'" in retry
        assert returns(pai, result) == [1, ["undefined", "marked"]]

    def test_time_spent_in_tools_does_not_count(self, pai: SimpleNamespace) -> None:
        agent = pai.Agent(
            scripted(pai, "return await tools.slow({})"),
            capabilities=[pai.integration.JSCodeMode(timeout=0.5)],
        )

        @agent.tool_plain
        async def slow() -> str:
            await asyncio.sleep(1.2)
            return "late but fine"

        assert returns(pai, agent.run_sync("hi")) == ["late but fine"]

    def test_memory_blowup_resets_the_sandbox(self, pai: SimpleNamespace) -> None:
        agent = weather_agent(
            pai,
            scripted(
                pai,
                "const a = []; for (;;) a.push('x'.repeat(1 << 16) + a.length)",
                "return 'alive'",
            ),
            max_memory=192 << 20,
        )
        result = agent.run_sync("hi")
        (retry,) = retries(pai, result)
        assert "sandbox was reset" in retry
        assert returns(pai, result) == ["alive"]

    def test_state_persists_and_restart_clears_it(self, pai: SimpleNamespace) -> None:
        agent = weather_agent(
            pai,
            scripted(
                pai,
                "const n = 41;\nfunction inc(x) { return x + 1 }\nglobalThis.extra = 'g';\nreturn n",
                "return [inc(n), extra]",
                {
                    "code": "return [typeof n, typeof inc, typeof extra]",
                    "restart": True,
                },
            ),
        )
        result = agent.run_sync("hi")
        assert returns(pai, result) == [
            41,
            [42, "g"],
            ["undefined", "undefined", "undefined"],
        ]

    def test_tools_appearing_and_disappearing_between_steps(
        self, pai: SimpleNamespace
    ) -> None:
        """The catalog is per step (a `prepare` hides a tool, tool search reveals one): new tools
        are installed without resetting the session, and a tool that is no longer offered cannot
        be called, not even through a reference the guest kept."""
        agent = pai.Agent(
            scripted(
                pai,
                "const kept = 1;\nglobalThis.f = tools.early;\n"
                "return [typeof tools.early, typeof tools.late, await tools.early({})]",
                "let r; try { await f({}) } catch (e) { r = [e.name, e.message] }\n"
                "return [kept, typeof tools.early, await tools.late({}), r]",
            ),
            capabilities=[pai.integration.JSCodeMode()],
        )

        async def only_first(ctx: Any, tool_def: Any) -> Any:
            return tool_def if ctx.run_step <= 1 else None

        async def only_later(ctx: Any, tool_def: Any) -> Any:
            return tool_def if ctx.run_step > 1 else None

        @agent.tool_plain(prepare=only_first)
        def early() -> str:
            return "early"

        @agent.tool_plain(prepare=only_later)
        def late() -> str:
            return "late"

        result = agent.run_sync("hi")
        first, second = returns(pai, result)
        assert first == ["function", "undefined", "early"]
        assert second[:3] == [1, "undefined", "late"]
        assert second[3][0] == "ToolUnavailable"

    def test_returning_nothing_says_so(self, pai: SimpleNamespace) -> None:
        result = weather_agent(pai, scripted(pai, "1 + 1")).run_sync("hi")
        (value,) = returns(pai, result)
        assert value["result"] is None and "return" in value["note"]


# ---------------------------------------------------------------------------
# 10. approvals, inline
# ---------------------------------------------------------------------------


class TestApprovals:
    @staticmethod
    def _agent(pai: SimpleNamespace, decide: Any, code: str) -> Any:
        capabilities: list[Any] = [pai.integration.JSCodeMode()]
        if decide is not None:

            def handler(ctx: Any, requests: Any) -> Any:
                return pai.DeferredToolResults(
                    approvals={
                        call.tool_call_id: decide(call) for call in requests.approvals
                    }
                )

            capabilities.append(pai.HandleDeferredToolCalls(handler=handler))
        agent = pai.Agent(scripted(pai, code), capabilities=capabilities)

        @agent.tool_plain(requires_approval=True)
        def wire_money(amount: int) -> str:
            return f"sent {amount}"

        return agent

    CODE = "try { return await tools.wire_money({amount: 5}) } catch (e) { return [e.name, e.message] }"

    def test_approved_call_returns_its_value(self, pai: SimpleNamespace) -> None:
        result = self._agent(pai, lambda call: True, self.CODE).run_sync("hi")
        assert returns(pai, result) == ["sent 5"]

    def test_denied_call_is_an_error_in_js(self, pai: SimpleNamespace) -> None:
        result = self._agent(
            pai, lambda call: pai.ToolDenied("too much"), self.CODE
        ).run_sync("hi")
        ((name, message),) = returns(pai, result)
        assert name == "ToolDenied" and "too much" in message
        (ret,) = [p for p in outcomes(pai, result) if isinstance(p, pai.ToolReturnPart)]
        (nested,) = ret.metadata["tool_returns"].values()
        assert nested.outcome == "denied"

    def test_without_a_handler_the_model_is_told_why(
        self, pai: SimpleNamespace
    ) -> None:
        agent = self._agent(pai, None, "return await tools.wire_money({amount: 5})")
        result = agent.run_sync("hi")
        (retry,) = retries(pai, result)
        assert retry.startswith("Runtime error: ApprovalRequired")
        assert "HandleDeferredToolCalls" in retry


# ---------------------------------------------------------------------------
# 11-12. console output, sandbox surface
# ---------------------------------------------------------------------------


class TestOutput:
    def test_console_output_and_result(self, pai: SimpleNamespace) -> None:
        code = "console.log('hello', {a: 1}); console.warn('careful'); return 42"
        result = weather_agent(
            pai, scripted(pai, code, "console.log('only')")
        ).run_sync("hi")
        first, second = returns(pai, result)
        assert first == {"output": 'hello {"a": 1}\n[warn] careful', "result": 42}
        assert second == {"output": "only"}
        ret = [p for p in outcomes(pai, result) if isinstance(p, pai.ToolReturnPart)][0]
        assert ret.metadata["console"] == [
            {"level": "log", "text": 'hello {"a": 1}'},
            {"level": "warn", "text": "careful"},
        ]

    def test_js_values_become_model_safe_data(self, pai: SimpleNamespace) -> None:
        code = (
            "return {u: undefined, n: NaN, big: 10n, d: new Date(0), s: new Set([1]),"
            " b: new Uint8Array([1, 2])}"
        )
        ((value,),) = [
            returns(pai, weather_agent(pai, scripted(pai, code)).run_sync("hi"))
        ]
        assert value == {
            "u": None,
            "n": "NaN",
            "big": 10,
            "d": "1970-01-01T00:00:00+00:00",
            "s": [1],
            "b": {"bytes_base64": "AQI="},
        }

    def test_sandbox_has_no_host_apis(self, pai: SimpleNamespace) -> None:
        code = (
            "return [typeof require, typeof Deno, typeof fetch, typeof process,"
            " typeof setTimeout, typeof importScripts, Date.now() === Date.now()]"
        )
        result = weather_agent(pai, scripted(pai, code)).run_sync("hi")
        assert returns(pai, result) == [["undefined"] * 6 + [True]]


# ---------------------------------------------------------------------------
# 13-14. concurrent runs, junk from TestModel
# ---------------------------------------------------------------------------


class TestRuns:
    def test_concurrent_runs_have_separate_sandboxes(
        self, pai: SimpleNamespace
    ) -> None:
        arrived = 0
        both = asyncio.Event()

        def make(tag: str) -> Any:
            return scripted(
                pai,
                f"globalThis.who = '{tag}'; return 'set'",
                "await tools.rendezvous({}); return who",
            )

        capability = pai.integration.JSCodeMode()
        agent = pai.Agent(make("A"), capabilities=[capability])

        @agent.tool_plain
        async def rendezvous() -> str:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 10)
            return "met"

        async def go() -> list[Any]:
            return list(
                await asyncio.gather(
                    agent.run("a", model=make("A")), agent.run("b", model=make("B"))
                )
            )

        first, second = asyncio.run(go())
        assert returns(pai, first) == ["set", "A"]
        assert returns(pai, second) == ["set", "B"]

    def test_test_model_junk_code_ends_cleanly(self, pai: SimpleNamespace) -> None:
        """TestModel fills `code` with junk (`"a"`, a ReferenceError) and repeats it after every
        retry prompt, so the run must end the way pydantic-ai ends any run whose model never
        corrects itself: `UnexpectedModelBehavior` once `max_retries` is spent. No hang, no other
        exception, no sandbox left running."""
        from pydantic_ai import capture_run_messages

        from pydeno import _isolated

        agent = weather_agent(pai, pai.TestModel(call_tools=[TOOL]), max_retries=2)
        with (
            capture_run_messages() as messages,
            pytest.raises(pai.UnexpectedModelBehavior),
        ):
            agent.run_sync("hi")
        prompts = [
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, pai.RetryPromptPart) and part.tool_name == TOOL
        ]
        assert len(prompts) == 2
        assert all(p.startswith("Runtime error: ReferenceError") for p in prompts)
        gc.collect()
        assert not [rt for rt in _isolated._LIVE if not rt.is_closed()]  # noqa: SLF001


# ---------------------------------------------------------------------------
# 15. JSON schema -> .d.ts golden tests
# ---------------------------------------------------------------------------


class TestDts:
    @staticmethod
    def dts(pai: SimpleNamespace, *tools: dict[str, Any]) -> str:
        return pai.integration.schema_tools_to_dts(tools)

    def test_recursive_model_becomes_a_named_interface(
        self, pai: SimpleNamespace
    ) -> None:
        from pydantic import BaseModel, TypeAdapter

        class Node(BaseModel):
            """A tree node."""

            name: str
            children: list[Node] = []  # noqa: RUF012

        Node.model_rebuild()
        out = self.dts(
            pai,
            {
                "name": "walk",
                "description": "Walk a tree.",
                "parameters_json_schema": {
                    "type": "object",
                    "properties": {"root": {"$ref": "#/$defs/Node"}},
                    "required": ["root"],
                    "$defs": TypeAdapter(Node).json_schema()["$defs"],
                },
                "return_schema": TypeAdapter(list[Node]).json_schema(),
            },
        )
        assert out == (
            "declare namespace tools {\n"
            "  /** A tree node. */\n"
            "  interface Node {\n"
            "    name: string;\n"
            "    /** @default [] */\n"
            "    children?: Node[];\n"
            "  }\n"
            "\n"
            "  /** Walk a tree. */\n"
            "  function walk(args: {\n"
            "    root: Node;\n"
            "  }): Promise<Node[]>;\n"
            "}\n"
        )

    def test_unions_literals_tuples_records_and_quoted_keys(
        self, pai: SimpleNamespace
    ) -> None:
        out = self.dts(
            pai,
            {
                "name": "get-weather",
                "parameters_json_schema": {
                    "type": "object",
                    "properties": {
                        "first-name": {"type": "string", "description": "Who."},
                        "mode": {"enum": ["fast", "slow"]},
                        "maybe": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "default": None,
                        },
                        "pair": {
                            "type": "array",
                            "prefixItems": [{"type": "integer"}, {"type": "string"}],
                        },
                        "rest": {
                            "type": "array",
                            "prefixItems": [{"type": "boolean"}],
                            "items": {"type": "number"},
                        },
                        "tags": {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        },
                        "mixed": {
                            "type": "object",
                            "properties": {"a": {"type": ["string", "null"]}},
                            "additionalProperties": {"type": "boolean"},
                        },
                        "closed": {"type": "object", "additionalProperties": False},
                        "k": {"const": 3},
                        "when": {"type": "string", "format": "date-time"},
                        "list": {
                            "type": "array",
                            "items": {
                                "oneOf": [{"type": "string"}, {"type": "number"}]
                            },
                        },
                        "both": {
                            "allOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]
                        },
                        "anything": {},
                    },
                    "required": ["first-name", "mode"],
                    "$defs": {
                        "A": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                        },
                        "B": {"type": "string", "description": "Just a string."},
                    },
                },
                "sequential": True,
            },
        )
        assert out == (
            "declare namespace tools {\n"
            "  interface A {\n"
            "    a?: string;\n"
            "  }\n"
            "\n"
            "  /** Just a string. */\n"
            "  type B = string;\n"
            "\n"
            "  /**\n"
            "   * (The tool `get-weather`.)\n"
            "   * Runs alone: other tool calls wait while it runs.\n"
            "   */\n"
            "  function get_weather(args: {\n"
            "    /** Who. */\n"
            '    "first-name": string;\n'
            '    mode: "fast" | "slow";\n'
            "    /** @default null */\n"
            "    maybe?: number | null;\n"
            "    pair?: [number, string];\n"
            "    rest?: [boolean, ...number[]];\n"
            "    tags?: Record<string, number>;\n"
            "    mixed?: {\n"
            "      a?: string | null;\n"
            "      [key: string]: boolean;\n"
            "    };\n"
            "    closed?: {};\n"
            "    k?: 3;\n"
            "    /** @format date-time */\n"
            "    when?: string;\n"
            "    list?: (string | number)[];\n"
            "    both?: A & B;\n"
            "    anything?: unknown;\n"
            "  }): Promise<unknown>;\n"
            "}\n"
        )

    def test_names_that_are_not_identifiers_or_collide(
        self, pai: SimpleNamespace
    ) -> None:
        mapping = pai.integration.js_tool_names(
            ["get-weather", "get_weather", "delete", "9lives", "a.b"]
        )
        assert mapping == {
            "get-weather": "get_weather",
            "get_weather": "get_weather_2",
            "delete": "delete_",
            "9lives": "_9lives",
            "a.b": "a_b",
        }
        out = self.dts(
            pai,
            {
                "name": "delete",
                "parameters_json_schema": {"type": "object", "properties": {}},
            },
        )
        assert (
            "function delete_(args?: Record<string, unknown>): Promise<unknown>;" in out
        )

    def test_same_def_name_with_different_shapes_gets_distinct_names(
        self, pai: SimpleNamespace
    ) -> None:
        def tool(name: str, inner: dict[str, Any]) -> dict[str, Any]:
            return {
                "name": name,
                "parameters_json_schema": {
                    "type": "object",
                    "properties": {"x": {"$ref": "#/$defs/Item"}},
                    "required": ["x"],
                    "$defs": {"Item": inner},
                },
            }

        out = self.dts(
            pai,
            tool("one", {"type": "string"}),
            tool("two", {"type": "number"}),
            tool("three", {"type": "string"}),
        )
        assert "type Item = string;" in out and "type Item_2 = number;" in out
        assert "function one(args: {\n    x: Item;\n  })" in out
        assert "function two(args: {\n    x: Item_2;\n  })" in out
        assert "function three(args: {\n    x: Item;\n  })" in out

    def test_unresolvable_refs_and_empty_toolsets(self, pai: SimpleNamespace) -> None:
        out = self.dts(
            pai,
            {
                "name": "f",
                "parameters_json_schema": {
                    "type": "object",
                    "properties": {
                        "x": {"$ref": "https://example.com/schema"},
                        "y": {"$ref": "#"},
                    },
                },
                "return_schema": {"type": "boolean"},
            },
        )
        assert "x?: unknown;" in out and "y?: unknown;" in out
        assert "): Promise<boolean>;" in out
        assert pai.integration.schema_tools_to_dts([]) == "declare namespace tools {}\n"

    def test_jsdoc_cannot_be_closed_by_a_description(
        self, pai: SimpleNamespace
    ) -> None:
        out = self.dts(
            pai,
            {
                "name": "f",
                "description": "evil */ declare const x: 1; /*",
                "parameters_json_schema": {},
            },
        )
        assert "*/ declare" not in out
        assert "evil *\\/ declare const x: 1; /*" in out


# ---------------------------------------------------------------------------
# packaging, and nothing left behind
# ---------------------------------------------------------------------------


def test_importing_pydeno_does_not_import_pydantic_ai() -> None:
    code = (
        "import sys, pydeno, pydeno.integrations;"
        "assert 'pydantic_ai' not in sys.modules, 'pydantic_ai was imported';"
        "assert 'pydeno.integrations.pydantic_ai' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


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
    from pydeno import _isolated

    gc.collect()
    time.sleep(0.4)  # let a spare worker finish starting, and killed ones be reaped
    return {
        "children": len(_child_pids()),
        "fds": len(os.listdir("/dev/fd")),
        "threads": threading.active_count(),
        "live_runtimes": len(_isolated._LIVE),  # noqa: SLF001
    }


def test_many_runs_leave_nothing_behind(pai: SimpleNamespace) -> None:
    def run(*steps: Any, **options: Any) -> None:
        weather_agent(pai, scripted(pai, *steps), **options).run_sync("hi")

    run("return 1")  # warm up: the prewarmed spare worker and lazy threads now exist
    before = _snapshot()
    for i in range(6):
        run(f"return await tools.get_weather({{city: 'c{i}'}})", "return 2")
    run("while (true) {}", "return 3", timeout=0.5)  # killed worker
    run("throw new Error('x')", "return 4")
    run(
        "await Promise.all([1, 2, 3].map((n) => tools.get_weather({city: String(n)}))); return 5"
    )
    with pytest.raises(pai.UsageLimitExceeded):
        weather_agent(
            pai,
            scripted(
                pai,
                "await tools.get_weather({city: 'a'}); await tools.get_weather({city: 'b'})",
            ),
        ).run_sync("hi", usage_limits=pai.UsageLimits(tool_calls_limit=1))
    after = _snapshot()
    assert after["live_runtimes"] == 0, (before, after)
    assert after["children"] <= before["children"], (before, after)
    assert after["fds"] <= before["fds"] + 3, (before, after)
    assert after["threads"] <= before["threads"] + 1, (before, after)
