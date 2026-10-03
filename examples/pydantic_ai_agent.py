"""
A pydantic-ai agent in "code mode" with JSCodeMode: instead of calling the
agent's tools one model turn at a time, the model writes one JavaScript
snippet that calls them (concurrently, with Promise.all) and returns the
answer. The snippet runs in a pydeno IsolatedRuntime: a V8 isolate in a
sandboxed worker process with a time limit and a memory ceiling.

Runs offline by default, with no API key: pydantic-ai's FunctionModel plays
the model, replying with scripted turns. The first snippet it writes is
TypeScript (a mistake real models make), so you also see the retry loop:
JSCodeMode reports the syntax error, the "model" fixes it, and the fixed
snippet runs.

To use a real model, name it in an environment variable (and set that
provider's API key as usual):

    PYDENO_EXAMPLE_MODEL=openai:gpt-5-mini python examples/pydantic_ai_agent.py

Run directly:
    python examples/pydantic_ai_agent.py
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # keep the output on the demo

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pydeno.integrations.pydantic_ai import JSCodeMode

CITIES = {"Zurich": 14.5, "Lisbon": 22.0, "Oslo": 6.5}


class Weather(BaseModel):
    city: str
    celsius: float


# A first attempt with a TypeScript annotation: not JavaScript, so it is rejected
# before anything runs, and the model is asked to try again.
TYPESCRIPT_ATTEMPT = """
const cities: string[] = ["Zurich", "Lisbon", "Oslo"];
return cities;
"""

# The corrected snippet: three tool calls in one model turn, run concurrently.
JAVASCRIPT = """
const cities = ["Zurich", "Lisbon", "Oslo"];
const reports = await Promise.all(cities.map((city) => tools.get_weather({ city })));
const warmest = reports.reduce((a, b) => (b.celsius > a.celsius ? b : a));
console.log(`checked ${reports.length} cities`);
return { warmest: warmest.city, celsius: warmest.celsius };
"""


def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A stand-in for a real LLM: answer each turn from what came back last."""
    if len(messages) == 1:
        run_js = next(t for t in info.function_tools if t.name == "run_javascript")
        declarations = run_js.description[run_js.description.index("```ts") :]
        print("The model is shown these tool declarations:\n")
        print(declarations, "\n")
        return ModelResponse(
            parts=[ToolCallPart("run_javascript", {"code": TYPESCRIPT_ATTEMPT})]
        )
    last = messages[-1].parts[-1]
    if isinstance(last, RetryPromptPart):
        print(f"JSCodeMode answered with a retry:\n  {last.content.splitlines()[0]}\n")
        return ModelResponse(
            parts=[ToolCallPart("run_javascript", {"code": JAVASCRIPT})]
        )
    assert isinstance(last, ToolReturnPart)
    print(f"The snippet returned: {last.content}\n")
    result = last.content["result"]
    return ModelResponse(
        parts=[TextPart(f"{result['warmest']} is warmest, at {result['celsius']} C.")]
    )


def build_agent() -> Agent:
    model = os.environ.get("PYDENO_EXAMPLE_MODEL") or FunctionModel(scripted_model)
    agent = Agent(
        model,
        instructions="Answer using the run_javascript tool.",
        capabilities=[JSCodeMode(timeout=10, max_tool_calls=20)],
    )

    @agent.tool_plain
    async def get_weather(city: str) -> Weather:
        """Current temperature in a city."""
        await asyncio.sleep(0.1)  # a slow API; the snippet's calls overlap
        return Weather(city=city, celsius=CITIES.get(city, 15.0))

    return agent


async def main() -> None:
    agent = build_agent()
    result = await agent.run("Which of Zurich, Lisbon and Oslo is warmest right now?")
    print(f"Agent output: {result.output}")
    print(f"Usage: {result.usage}")


if __name__ == "__main__":
    asyncio.run(main())
