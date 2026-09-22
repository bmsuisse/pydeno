"""
Bridging FastMCP tools into a pydeno sandbox.

This is the real pattern behind "AI agent writes JS that calls tools safely":

1. Tools live in a normal FastMCP server (`fastmcp.FastMCP`) - the same
   server you'd expose over stdio/HTTP to any MCP client.
2. A single Python function, `call_tool`, is bound into the JS runtime via
   `bind_function`. It receives a tool name + JSON-safe args from JS and
   dispatches to the FastMCP server using its in-process `Client` - no
   network hop, no separate process, just an in-memory transport.
3. The model-generated JS calls `callTool(name, args)` (awaiting the
   Promise) instead of getting N separate host bindings, one per tool.
   This is the "code mode" pattern: the JS does its own
   looping/branching over tool calls in one sandboxed script.
4. `RuntimeConfig(timeout=...)` and `max_heap_size` bound a runaway or
   malicious script, and `TerminationHandle` shows how to kill one from a
   watchdog thread if `eval_async`'s own timeout isn't enough (e.g. the
   handler itself is what's hanging).

Run directly:
    python examples/fastmcp_tool_bridge.py
"""

import asyncio
import threading

from fastmcp import Client, FastMCP

from pydeno import Runtime, RuntimeConfig

# --- 1. A normal FastMCP server with a couple of simple tools ---

mcp = FastMCP("demo-tools")


@mcp.tool
def get_weather(city: str) -> str:
    """Mock weather lookup - no real API call needed for this example."""
    fake_temps = {"Zurich": 18, "Berlin": 14, "Lisbon": 24}
    temp = fake_temps.get(city, 20)
    return f"{temp}C and clear in {city}"


@mcp.tool
def add(a: int, b: int) -> int:
    return a + b


@mcp.tool
def to_upper(text: str) -> str:
    return text.upper()


# --- 2. Bridge: one Python function forwards any tool call from JS ---


async def build_tool_bridge(runtime: Runtime) -> None:
    """Bind a single `callTool(name, args)` function backed by an in-process
    FastMCP client, so JS never talks to Python tools one binding at a time.
    """
    client = Client(mcp)
    await client.__aenter__()  # keep the in-process session open for the runtime's lifetime

    async def call_tool(name: str, args: dict) -> object:
        result = await client.call_tool(name, args)
        return result.data

    runtime.bind_function("callTool", call_tool)
    return client


async def happy_path() -> None:
    print("=== Happy path: JS batches two tool calls into one script ===\n")

    config = RuntimeConfig(max_heap_size=20 * 1024 * 1024, timeout=5.0)
    with Runtime(config) as runtime:
        client = await build_tool_bridge(runtime)
        try:
            # One script does what would otherwise be 2+ separate tool-call
            # round trips: fetch weather for two cities, then combine them
            # with a plain Python-side tool call, all inside the sandbox.
            js_code = """
            async function main() {
                const zurich = await callTool("get_weather", { city: "Zurich" });
                const berlin = await callTool("get_weather", { city: "Berlin" });
                const sum = await callTool("add", { a: 18, b: 14 });
                return { zurich, berlin, combinedTemp: sum };
            }
            main();
            """
            result = await runtime.eval_async(js_code, timeout=5.0)
            print(f"Result from sandboxed JS: {result}")
        finally:
            await client.__aexit__(None, None, None)

    print()


async def runaway_script_is_killed() -> None:
    """The safety property: a misbehaving script doesn't hang the host."""
    print("=== Safety: runaway JS is killed via timeout ===\n")

    with Runtime() as runtime:
        handle = runtime.termination_handle()
        # Belt-and-suspenders: eval_async's own timeout usually suffices,
        # but a watchdog thread can force-terminate from outside too.
        threading.Timer(1.0, handle.terminate).start()
        try:
            await runtime.eval_async("while (true) {}", timeout=1.0)
            print("unexpected: runaway script did not stop")
        except (TimeoutError, RuntimeError) as e:
            print(f"Caught expected timeout/termination: {type(e).__name__}: {e}")

    print()


async def main() -> None:
    await happy_path()
    await runaway_script_is_killed()


if __name__ == "__main__":
    asyncio.run(main())
