"""The DevTools inspector server is the cargo feature `inspector`, on by default.

A build with `--no-default-features` drops the server and its network crates. `InspectorConfig`
still exists there, so code that constructs one keeps importing, but a runtime configured with it
fails with a clear error instead of starting without the debugger it asked for. CI builds both
(`.github/workflows/test.yml`, job `minimal-build`) and runs this file against each.
"""

from __future__ import annotations

import socket

import pytest

from pydeno import InspectorConfig, Runtime, RuntimeConfig, _pydeno


def test_the_build_reports_whether_it_has_the_inspector() -> None:
    assert isinstance(_pydeno._INSPECTOR_AVAILABLE, bool)


def test_a_runtime_without_an_inspector_works_in_either_build() -> None:
    with Runtime() as rt:
        assert rt.eval("1 + 1") == 2
        assert rt.inspector_endpoints() is None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_an_inspector_config_starts_the_server_or_fails_clearly() -> None:
    config = RuntimeConfig(
        inspector=InspectorConfig(host="127.0.0.1", port=_free_port())
    )
    if _pydeno._INSPECTOR_AVAILABLE:
        with Runtime(config) as rt:
            assert rt.inspector_endpoints() is not None
    else:
        with pytest.raises(RuntimeError, match="built without inspector support"):
            Runtime(config)
