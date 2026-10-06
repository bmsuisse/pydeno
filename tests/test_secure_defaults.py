"""Defaults for code you do not trust are pinned here (#127, #129, #132)."""

from __future__ import annotations

import inspect

import pytest

import pydeno
from pydeno import AsyncIsolatedRuntime, IsolatedRuntime, _isolated


@pytest.mark.parametrize("cls", [IsolatedRuntime, AsyncIsolatedRuntime])
def test_sandbox_defaults_to_require(cls: type) -> None:
    assert inspect.signature(cls).parameters["sandbox"].default == "require"


def test_the_default_runtime_helpers_inherit_require() -> None:
    pydeno.close_default_runtime()  # an earlier test may have left a plain default runtime open
    try:
        pydeno.configure_default_runtime(isolated=True)
        runtime = pydeno.get_default_runtime()
        assert runtime._options["sandbox"] == "require"  # noqa: SLF001
        assert runtime.sandbox_degraded is False
    finally:
        pydeno.close_default_runtime()
        pydeno.configure_default_runtime()


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
    if status.hardened:
        assert status.complete and status.empty_root.applied
    if status.complete and not status.empty_root.applied:
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
