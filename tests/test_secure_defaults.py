"""Defaults for code you do not trust are pinned here (#127, #129, #132)."""

from __future__ import annotations


import pytest

import pydeno
from pydeno import IsolatedRuntime, _isolated


def test_sandbox_defaults_to_require(original_sandbox_defaults: dict[str, str]) -> None:
    assert original_sandbox_defaults == {
        "IsolatedRuntime": "require",
        "AsyncIsolatedRuntime": "require",
    }


def test_the_default_runtime_helpers_forward_no_sandbox_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`configure_default_runtime(isolated=True)` must leave `sandbox` to the constructor's
    default ("require"), not name one itself."""
    seen: list[dict[str, object]] = []

    class Recorder:
        def __init__(self, **options: object) -> None:
            seen.append(options)

    monkeypatch.setattr(_isolated, "IsolatedRuntime", Recorder)
    monkeypatch.setattr(pydeno, "_default_factory", pydeno._default_factory)  # noqa: SLF001
    pydeno.configure_default_runtime(isolated=True)
    try:
        pydeno._default_factory()  # noqa: SLF001
    finally:
        pydeno.configure_default_runtime()
    assert seen == [{}]


def test_a_finite_host_call_budget_is_the_default() -> None:
    with IsolatedRuntime() as rt:
        assert rt._max_host_calls == _isolated.DEFAULT_MAX_HOST_CALLS == 10_000  # noqa: SLF001
        assert rt._max_host_wait == _isolated.DEFAULT_MAX_HOST_WAIT == 60.0  # noqa: SLF001


def test_the_host_call_budget_can_be_removed_or_lowered() -> None:
    with IsolatedRuntime(max_host_calls=None) as rt:
        assert rt._max_host_calls is None  # noqa: SLF001
    with IsolatedRuntime(max_host_calls=3) as rt:
        rt.bind_function("host", lambda v: v)
        with pytest.raises(Exception, match="max_host_calls|host calls"):
            rt.eval("for (let i = 0; i < 10; i++) host(i)")


def test_a_pool_checkout_gets_the_same_defaults() -> None:
    opts = _isolated._session_options()  # noqa: SLF001
    assert opts["_max_host_calls"] == 10_000
    assert opts["_max_host_wait"] == 60.0
    assert _isolated._session_options(max_host_calls=None)["_max_host_calls"] is None  # noqa: SLF001


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (True, "auto"),
        (False, "off"),
        ("auto", "auto"),
        ("require", "require"),
        ("off", "off"),
    ],
)
def test_empty_root_modes(value: object, mode: str) -> None:
    assert _isolated._empty_root_mode(value, "require") == mode  # noqa: SLF001


def test_empty_root_rejects_nonsense_and_a_sandbox_that_is_off() -> None:
    with pytest.raises(ValueError, match="empty_root"):
        _isolated._empty_root_mode("sometimes", "require")  # noqa: SLF001
    with pytest.raises(ValueError, match="sandbox='off'"):
        IsolatedRuntime(sandbox="off", empty_root="require")


def test_status_separates_complete_from_hardened() -> None:
    status = pydeno.sandbox_status()
    assert status.to_dict()["hardened"] == status.hardened
    linux = status.platform == "linux"
    if status.hardened:
        assert status.complete and (not linux or status.empty_root.applied)
    if linux and status.complete and not status.empty_root.applied:
        assert not status.hardened
        assert "not hardened" in status.explain()


def test_empty_root_require_refuses_when_the_layer_is_missing() -> None:
    status = pydeno.sandbox_status()
    if status.empty_root.applied:
        with IsolatedRuntime(empty_root="require") as rt:
            assert "emptyroot" in rt.sandbox_extras
    else:
        with pytest.raises(pydeno.WorkerCrashed, match="empty_root='require'"):
            IsolatedRuntime(empty_root="require")
