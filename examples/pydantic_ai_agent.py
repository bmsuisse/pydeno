"""
A pydantic-ai agent whose tool-calling loop includes ONE "code mode" tool:
instead of the model making N separate tool calls (one per city, one per
sum...), it submits a single JS script that loops/branches over several
operations itself, executed safely in a pydeno sandbox.

The pydeno execution (`run_js_batch`) is the real, fully-working part of
this example and needs no API key. Driving the LLM side without a key uses
pydantic-ai's `FunctionModel` (https://ai.pydantic.dev/models/#function),
its built-in mechanism for scripting exactly what a "model" replies with -
here it deterministically: (1) submits a JS batch script as a tool call,
then (2) returns a final text answer from the tool's result. Swap
`FunctionModel(...)` for `"openai:gpt-5-mini"` (or any real model string) to
run this against a real LLM; the tool and sandbox code don't change.

Run directly (no API key needed):
    python examples/pydantic_ai_agent.py
"""

import asyncio
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # keep output focused on the demo, not the CLI banner

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pydeno import Runtime, RuntimeConfig

# A batch of numbers a "user" wants summary statistics for. In a real agent
# this would come from the conversation; here it's fixed so the offline
# FunctionModel below can submit a deterministic JS script for it.
NUMBERS = [4, 8, 15, 16, 23, 42]

JS_BATCH_SCRIPT = """
const numbers = INPUT.numbers;
const sum = numbers.reduce((a, b) => a + b, 0);
const mean = sum / numbers.length;
const max = Math.max(...numbers);
const min = Math.min(...numbers);
({ sum, mean, max, min });
"""


def fake_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Deterministic stand-in for a real LLM (see FunctionModel docs).

    Turn 1: "decide" to submit one JS batch script instead of four separate
    tool calls (sum, mean, max, min). Turn 2: read the tool's return value
    and answer in text - mirroring a real model's tool-calling loop.
    """
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="run_js_batch",
                    args={"code": JS_BATCH_SCRIPT, "input": {"numbers": NUMBERS}},
                )
            ]
        )
    tool_return = messages[-1].parts[-1].content
    return ModelResponse(parts=[TextPart(f"Stats: {tool_return}")])


agent = Agent(FunctionModel(fake_model))


@agent.tool
async def run_js_batch(ctx: RunContext, code: str, input: dict) -> dict:
    """Execute a batch of JS operations in one sandboxed script.

    This is the "code mode" primitive: the caller passes one script that
    does several computations itself (a loop, several reduces...) instead
    of the model issuing one tool call per operation. `input` is bound onto
    the sandbox as `INPUT` so the script can read caller-supplied data.
    """
    config = RuntimeConfig(max_heap_size=10 * 1024 * 1024)
    with Runtime(config) as runtime:
        runtime.bind_object("INPUT", input)
        # Real safety property: a runaway batch script is killed, not left
        # to hang the agent's tool-calling loop.
        result = await runtime.eval_async(code, timeout=3.0)
        return result


async def runaway_batch_is_killed() -> None:
    """Same safety property, exercised directly against the sandbox: a
    script that never resolves is killed by eval_async's timeout rather
    than hanging the agent loop.
    """
    print("=== Safety: a runaway batch script is killed ===\n")
    try:
        await run_js_batch(
            ctx=None,  # type: ignore[arg-type]
            code="new Promise(() => {});",
            input={},
        )
        print("unexpected: runaway batch did not time out")
    except (TimeoutError, RuntimeError) as e:
        print(f"Caught expected timeout: {type(e).__name__}: {e}")
    print()


async def main() -> None:
    print("=== Agent with a 'code mode' JS batch tool (offline FunctionModel) ===\n")
    result = await agent.run("Give me summary statistics for my numbers.")
    print(f"Agent output: {result.output}")
    print()

    await runaway_batch_is_killed()


if __name__ == "__main__":
    asyncio.run(main())
