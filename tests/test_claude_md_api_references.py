"""`CLAUDE.md` must only name API that exists.

The 0.4.0 review found its "Streaming between JavaScript and Python" example
calling `rt.create_js_stream_from_python(...)` and a guest global
`__pydeno_get_stream__(id)`. Neither has ever existed: `git log -S` puts both
in the initial 0.1.0 commit's `CLAUDE.md` and nowhere else in the tree, and
the 0.3.x `peno` -> `pydeno` rename dutifully renamed the guest global,
which is how a symbol that was never real survived a pass that touched it.

The example is the onboarding document for anyone (human or agent) working in
this repo, so a method name in it is read as a promise. This test is the
cheapest thing that would have caught it: every `rt.x` / `runtime.x` the file
mentions has to resolve on the real `Runtime`, and every `__x__()` guest
global it mentions has to be one the bridge actually installs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pydeno import Runtime, RuntimeConfig

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

# Prose refers to source files as `runtime.rs`; only Python attribute access
# is of interest here.
_NOT_AN_ATTRIBUTE = {"rs", "py"}


def _referenced_attributes() -> set[str]:
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    found = set(re.findall(r"\b(?:rt|runtime)\.([A-Za-z_][A-Za-z0-9_]*)", text))
    return found - _NOT_AN_ATTRIBUTE


def _referenced_guest_globals() -> set[str]:
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    return set(re.findall(r"(__[A-Za-z0-9_]+__)\(", text))


def test_claude_md_only_names_runtime_methods_that_exist() -> None:
    referenced = _referenced_attributes()
    assert referenced, "expected CLAUDE.md to show some Runtime usage"
    missing = sorted(name for name in referenced if not hasattr(Runtime, name))
    assert not missing, (
        f"CLAUDE.md documents Runtime attributes that do not exist: {missing}. "
        f"Either implement them or correct the example -- a name in CLAUDE.md "
        f"reads as a promise."
    )


def test_claude_md_only_names_guest_globals_that_exist() -> None:
    referenced = _referenced_guest_globals()
    assert referenced, "expected CLAUDE.md to show some guest-global usage"
    with Runtime(RuntimeConfig()) as rt:
        missing = sorted(
            name
            for name in referenced
            if rt.eval(f"typeof globalThis[{name!r}]") == "undefined"
        )
    assert not missing, (
        f"CLAUDE.md documents guest globals that a fresh runtime does not "
        f"expose: {missing}"
    )


@pytest.mark.asyncio
async def test_claude_md_streaming_example_runs() -> None:
    """The corrected streaming example, executed rather than just read."""
    import asyncio

    with Runtime() as rt:

        async def data_generator():
            for i in range(5):
                yield {"count": i}
                await asyncio.sleep(0)

        py_stream = rt.stream_from_async_iterable(data_generator())
        setter = rt.eval("(stream) => { globalThis.myStream = stream; }")
        setter(py_stream)

        result = await rt.eval_async(
            """
            (async () => {
                const reader = myStream.getReader();
                const chunks = [];
                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    chunks.push(value);
                }
                return chunks;
            })()
            """
        )
        assert result == [{"count": i} for i in range(5)]

        js_stream = await rt.eval_async(
            "(async () => new ReadableStream({start(c) { c.enqueue(1); c.close(); }}))()"
        )
        assert [chunk async for chunk in js_stream] == [1]
