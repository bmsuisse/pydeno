"""`JavaScriptSession`, the tool's engine, without `llm` installed.

Every test starts a real sandboxed worker (``sandbox="require"`` by default).
"""

from __future__ import annotations

import json
import sys

import pytest

from llm_pydeno.session import RESET_NOTE, JavaScriptSession

FIELDS = {"status", "stdout", "stderr", "result", "error", "error_type", "truncated"}


@pytest.fixture
def session():
    s = JavaScriptSession(timeout=5)
    yield s
    s.close()


def test_result_and_console(session: JavaScriptSession) -> None:
    out = session.run("console.log('hi', 1); console.warn('careful'); return [1, 2]")
    assert set(out) == FIELDS
    assert out["status"] == "Succeeded"
    assert out["stdout"] == "hi 1\n"
    assert out["stderr"] == "careful\n"
    assert out["result"] == [1, 2]
    assert out["error"] is None
    json.dumps(out)  # what the model receives


def test_state_is_kept_between_calls(session: JavaScriptSession) -> None:
    code = "const xs = [3, 1, 2]\nfunction total(a) { return a.reduce((s, x) => s + x, 0); }"
    assert session.run(code)["status"] == "Succeeded"
    assert session.run("return total(xs.sort())")["result"] == 6
    assert session.run("return await Promise.resolve(xs)")["result"] == [1, 2, 3]


def test_javascript_error_is_a_failed_result(session: JavaScriptSession) -> None:
    out = session.run("null.x")
    assert out["status"] == "Failed"
    assert out["error_type"] == "TypeError"
    assert "Cannot read properties of null" in out["error"]
    # The session survives a thrown error.
    assert session.run("return 1")["result"] == 1


def test_output_cap() -> None:
    s = JavaScriptSession(max_output_bytes=100)
    try:
        out = s.run(
            "for (let i = 0; i < 1000; i++) console.log('x'.repeat(50)); return 1"
        )
        assert out["truncated"] is True
        assert out["stdout"].endswith("[truncated]\n")
        assert len(out["stdout"].encode()) < 200
        assert out["result"] == 1
    finally:
        s.close()


def test_result_cap_keeps_the_session() -> None:
    s = JavaScriptSession(max_result_bytes=1000)
    try:
        s.run("globalThis.kept = 'yes'")
        out = s.run("return 'x'.repeat(5000)")
        assert out["status"] == "Failed"
        assert out["error_type"] == "ResultTooLarge"
        assert s.run("return kept")["result"] == "yes"
    finally:
        s.close()


def test_timeout_restarts_the_session() -> None:
    s = JavaScriptSession(timeout=0.5)
    try:
        s.run("globalThis.before = 1")
        out = s.run("while (true) {}")
        assert out["status"] == "Failed"
        assert out["error"].endswith(RESET_NOTE)
        after = s.run("return typeof before")
        assert after["status"] == "Succeeded"
        assert after["result"] == "undefined"
    finally:
        s.close()


def test_no_host_access(session: JavaScriptSession) -> None:
    out = session.run(
        "return [typeof require, typeof process, typeof Deno, typeof fetch]"
    )
    assert out["result"] == ["undefined"] * 4


def test_reset_drops_state(session: JavaScriptSession) -> None:
    session.run("globalThis.v = 1")
    session.reset()
    assert session.run("return typeof v")["result"] == "undefined"


def test_worker_that_cannot_start_is_a_failed_result(monkeypatch) -> None:
    s = JavaScriptSession()

    def refuse() -> None:
        raise RuntimeError("worker failed to start: an OS sandbox is required but X")

    monkeypatch.setattr(s, "_open", refuse)
    out = s.run("return 1")
    assert set(out) == FIELDS
    assert out["status"] == "Failed"
    assert "OS sandbox" in out["error"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"max_memory_mb": 0},
        {"max_output_bytes": True},
        {"max_result_bytes": -1},
        {"sandbox": "off"},
    ],
    ids=str,
)
def test_bad_options(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        JavaScriptSession(**kwargs)


def test_importing_the_session_does_not_need_llm() -> None:
    import os
    import subprocess
    from pathlib import Path

    root = str(Path(__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [root, *filter(None, [os.environ.get("PYTHONPATH")])]
        ),
    }
    code = (
        "import sys; sys.modules['llm'] = None; "  # any `import llm` now fails
        "import llm_pydeno, llm_pydeno.session; print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert out.stdout.strip() == "ok", out.stderr
