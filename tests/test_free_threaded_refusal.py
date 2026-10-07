"""#140: sandboxed runtimes refuse a free-threaded CPython unless `PYDENO_ALLOW_FREE_THREADED` is set."""

from __future__ import annotations

import pytest

from pydeno import AsyncIsolatedRuntime, IsolatedRuntime, _isolated
from pydeno._errors import classify_error


@pytest.fixture
def free_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_isolated, "_is_free_threaded_build", lambda: True)
    monkeypatch.delenv(_isolated.ALLOW_FREE_THREADED_ENV, raising=False)


def test_isolated_runtime_refuses(free_threaded: None) -> None:
    with pytest.raises(
        RuntimeError, match="free-threaded CPython is not supported"
    ) as ei:
        IsolatedRuntime()
    info = classify_error(ei.value)
    assert info.kind == "sandbox_unavailable"
    assert not info.retryable


def test_async_isolated_runtime_refuses(free_threaded: None) -> None:
    with pytest.raises(RuntimeError, match="free-threaded CPython is not supported"):
        AsyncIsolatedRuntime()


def test_refusal_names_the_opt_in(free_threaded: None) -> None:
    with pytest.raises(RuntimeError, match="PYDENO_ALLOW_FREE_THREADED"):
        IsolatedRuntime()


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_opt_in_allows_start(
    free_threaded: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PYDENO_ALLOW_FREE_THREADED", value)
    with IsolatedRuntime() as rt:
        assert rt.eval("1 + 1") == 2


@pytest.mark.parametrize("value", ["", "0", "no"])
def test_falsy_opt_in_still_refuses(
    free_threaded: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PYDENO_ALLOW_FREE_THREADED", value)
    with pytest.raises(RuntimeError, match="free-threaded"):
        IsolatedRuntime()


def test_gil_build_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_isolated, "_is_free_threaded_build", lambda: False)
    monkeypatch.delenv(_isolated.ALLOW_FREE_THREADED_ENV, raising=False)
    with IsolatedRuntime() as rt:
        assert rt.eval("2") == 2
