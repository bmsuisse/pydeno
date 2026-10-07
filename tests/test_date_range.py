"""#142: a guest Date Python cannot hold is one RuntimeError, and the far edge converts exactly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pydeno import AgentSandbox, IsolatedRuntime, Runtime

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS = -62_135_596_800_000  # 0001-01-01T00:00:00Z
MAX_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z

UNREPRESENTABLE = ["8.64e15", "-8.64e15", str(MIN_MS - 1), str(MAX_MS + 1), "NaN"]


@pytest.mark.parametrize(
    "ms", [MIN_MS, MIN_MS + 1, -1, 0, 1, 1_700_000_000_123, MAX_MS - 1, MAX_MS]
)
def test_conversion_is_exact_to_the_millisecond(ms: int) -> None:
    expected = EPOCH + timedelta(milliseconds=ms)
    with IsolatedRuntime() as rt:
        assert rt.eval(f"new Date({ms})") == expected
    with Runtime() as plain:
        assert plain.eval(f"new Date({ms})") == expected


def test_far_edge_has_no_microsecond_error() -> None:
    with IsolatedRuntime() as rt:
        got = rt.eval(f"new Date({MAX_MS})")
    assert got.microsecond == 999_000
    assert got == datetime(9999, 12, 31, 23, 59, 59, 999_000, tzinfo=timezone.utc)


@pytest.mark.parametrize("ms", UNREPRESENTABLE)
def test_isolated_runtime_raises_one_runtime_error(ms: str) -> None:
    with IsolatedRuntime() as rt:
        with pytest.raises(RuntimeError, match="Date value out of range") as ei:
            rt.eval(f"new Date({ms})")
        assert not isinstance(ei.value, ValueError)
        assert "year" not in str(ei.value)
        assert rt.eval("1 + 1") == 2  # the runtime is still healthy


@pytest.mark.parametrize("ms", UNREPRESENTABLE)
def test_plain_runtime_raises_the_same_error(ms: str) -> None:
    with Runtime() as rt:
        with pytest.raises(RuntimeError, match="Date value out of range"):
            rt.eval(f"new Date({ms})")
        assert rt.eval("2") == 2


def test_host_function_argument_does_not_leak_python_text() -> None:
    with IsolatedRuntime() as rt:
        rt.bind_function("f", lambda d: d)
        out = rt.eval(
            "try { f(new Date(8.64e15)); 'no error' } catch (e) { String(e) }"
        )
        assert "year" not in out and "1..9999" not in out
        assert "ValueError" not in out
        assert "out of range" in out


def test_agent_sandbox_reports_a_failed_result() -> None:
    with AgentSandbox({}) as s:
        r = s.execute("return new Date(8.64e15)")
        assert r.status == "Failed"
        assert r.error_type == "RuntimeError"
        assert "year must be" not in (r.error or "")
        assert s.execute("return 1").status == "Succeeded"


def test_classified_as_a_guest_error_not_unknown() -> None:
    from pydeno._errors import classify_error

    with IsolatedRuntime() as rt:
        with pytest.raises(RuntimeError) as ei:
            rt.eval("new Date(8.64e15)")
    assert not isinstance(ei.value, ValueError)
    assert classify_error(ei.value).kind != "invalid_input"
