"""The plugin driven through `llm`'s own tool machinery: registration and a tool-calling chain.

Needs `llm` installed (a dependency of the package). The model is a scripted stand-in that asks
for tool calls, so no API key or network is involved.
"""

from __future__ import annotations

import json

import pytest

llm = pytest.importorskip("llm")

from llm_pydeno import plugin  # noqa: E402


def _ensure_registered() -> None:
    from llm.plugins import pm

    llm.get_tools()  # loads installed plugins, ours among them when it is installed
    if not any(p is plugin for p in pm.get_plugins()):
        pm.register(plugin, name="llm_pydeno_test")


def test_registers_the_pydeno_toolbox() -> None:
    _ensure_registered()
    tools = llm.get_tools()
    assert tools["PyDeno"] is plugin.PyDeno
    names = [t.name for t in plugin.PyDeno().tools()]
    assert names == ["PyDeno_run_javascript"]


def test_tool_schema_takes_one_code_string() -> None:
    (tool,) = plugin.PyDeno().tools()
    assert tool.input_schema["properties"] == {"code": {"type": "string"}}
    assert tool.input_schema["required"] == ["code"]
    assert "sandboxed" in tool.description


class ScriptedModel(llm.Model):
    """Asks for one tool call per script entry, then answers with the tool outputs it saw."""

    model_id = "scripted-test"
    supports_tools = True
    can_stream = False

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.seen: list[dict] = []

    def execute(self, prompt, stream, response, conversation):  # noqa: ARG002
        for result in prompt.tool_results:
            self.seen.append(json.loads(result.output))
        if self.script:
            response.add_tool_call(
                llm.ToolCall(
                    name="PyDeno_run_javascript",
                    arguments={"code": self.script.pop(0)},
                )
            )
            yield ""
        else:
            yield "done"


def test_chain_runs_javascript_and_keeps_state() -> None:
    toolbox = plugin.PyDeno(timeout=5, max_output_bytes=200, max_result_bytes=1000)
    model = ScriptedModel(
        [
            "const prices = [3.5, 2, 4.5]; console.log('stored'); return prices.length",
            "return prices.reduce((a, b) => a + b, 0)",
            "null.x",
            "return 'x'.repeat(5000)",
            "for (let i = 0; i < 100; i++) console.log('y'.repeat(20)); return 1",
        ]
    )
    try:
        assert model.chain("go", tools=[toolbox]).text().endswith("done")
    finally:
        toolbox._close()
    first, second, error, too_big, noisy = model.seen
    assert first == {
        "status": "Succeeded",
        "stdout": "stored\n",
        "stderr": "",
        "result": 3,
        "error": None,
        "error_type": None,
        "truncated": False,
    }
    assert second["result"] == 10
    assert error["status"] == "Failed"
    assert error["error_type"] == "TypeError"
    assert too_big["error_type"] == "ResultTooLarge"
    assert noisy["truncated"] is True
    assert noisy["stdout"].endswith("[truncated]\n")


def test_dropping_toolboxes_reclaims_their_workers() -> None:
    import gc
    import time

    procs = []
    for _ in range(3):
        toolbox = plugin.PyDeno()
        (tool,) = toolbox.tools()
        assert tool.implementation(code="return 1")["result"] == 1
        procs.append(toolbox._session._sandbox._core.rt._proc)
        del toolbox, tool
    gc.collect()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(p.poll() is None for p in procs):
        time.sleep(0.05)
    assert all(p.poll() is not None for p in procs)


def test_options_from_an_llm_tool_spec() -> None:
    """A `-T 'PyDeno({...})'` spec builds the toolbox from JSON keyword arguments."""
    from llm.utils import instantiate_from_spec

    toolbox = instantiate_from_spec(
        {"PyDeno": plugin.PyDeno},
        'PyDeno({"timeout": 3, "max_output_bytes": 100, "fresh_session_per_call": true})',
    )
    try:
        session = toolbox._session
        assert (session.timeout, session.max_output_bytes) == (3.0, 100)
        (tool,) = toolbox.tools()
        tool.implementation(code="globalThis.kept = 1")
        assert tool.implementation(code="return typeof kept")["result"] == "undefined"
    finally:
        toolbox._close()
