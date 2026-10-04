"""`JavaScriptSession`, the tool's engine, without `llm` installed.

Every test starts a real sandboxed worker (``sandbox="require"`` by default).
"""

from __future__ import annotations

import gc
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
        {"timeout": float("inf")},
        {"timeout": float("nan")},
        {"timeout": 86401},
        {"max_memory_mb": 2**40},
        {"max_output_bytes": 2**53},
        {"fresh_session_per_call": "false"},
        {"fresh_session_per_call": 1},
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


# ---------------------------------------------------------------- lifetime and sharing


def _worker(session: JavaScriptSession):
    """The session's worker process (internal: AgentSandbox -> its runtime -> the Popen)."""
    return session._sandbox._core.rt._proc


def _wait_exited(proc, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def test_dropping_a_session_reclaims_its_worker() -> None:
    procs = []
    for _ in range(3):
        s = JavaScriptSession()
        assert s.run("return 1")["result"] == 1
        procs.append(_worker(s))
        del s
    gc.collect()
    assert all(_wait_exited(p) for p in procs)


def test_default_session_runs_under_the_complete_os_sandbox(
    session: JavaScriptSession,
) -> None:
    assert session.run("return 1")["result"] == 1
    expected = "seatbelt" if sys.platform == "darwin" else "landlock+seccomp"
    assert session._sandbox._core.rt.sandbox == expected


def test_close_reclaims_the_worker() -> None:
    s = JavaScriptSession()
    s.run("return 1")
    proc = _worker(s)
    s.close()
    assert _wait_exited(proc)


def test_a_shared_session_serialises_calls_and_shares_state() -> None:
    """One session is one JavaScript global scope: concurrent callers run one at a time, and each
    sees what the others left behind (why the README says never to share one across users)."""
    s = JavaScriptSession()
    try:
        code = "globalThis.n = (globalThis.n || 0) + 1; return n"
        with ThreadPoolExecutor(8) as pool:
            outs = list(pool.map(lambda _: s.run(code), range(16)))
        assert all(o["status"] == "Succeeded" for o in outs), outs
        assert sorted(o["result"] for o in outs) == list(range(1, 17))
    finally:
        s.close()


def test_fresh_session_per_call_keeps_nothing() -> None:
    s = JavaScriptSession(fresh_session_per_call=True)
    try:
        assert s.run("globalThis.secret = 'a'; return 1")["result"] == 1
        assert s.run("return typeof secret")["result"] == "undefined"
        assert s._sandbox is None  # no worker held between calls
    finally:
        s.close()


def test_the_package_ships_its_license() -> None:
    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject
    assert "MIT License" in (root / "LICENSE").read_text(encoding="utf-8")
