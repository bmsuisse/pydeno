"""`configure_default_runtime`: make the module-level `pydeno.eval()` path the safe one."""

from __future__ import annotations

import asyncio
import threading

import pytest

import pydeno
from pydeno import IsolatedRuntime, Runtime, RuntimeConfig, WorkerCrashed


@pytest.fixture(autouse=True)
def _restore_default() -> None:
    """Every test starts and ends on the library's own default, with nothing left running."""
    pydeno.close_default_runtime()
    pydeno.configure_default_runtime()
    yield
    pydeno.close_default_runtime()
    pydeno.configure_default_runtime()


def test_the_default_is_still_an_in_process_runtime() -> None:
    assert type(pydeno.get_default_runtime()) is Runtime
    assert pydeno.eval("1 + 1") == 2


def test_isolated_mode_runs_the_module_level_functions_in_a_worker() -> None:
    pydeno.configure_default_runtime(isolated=True)
    rt = pydeno.get_default_runtime()
    assert isinstance(rt, IsolatedRuntime)
    assert pydeno.eval("6 * 7") == 42
    assert rt._proc.poll() is None  # noqa: SLF001 - a live worker process is doing the work


def test_a_crashing_guest_no_longer_takes_the_host_down_through_pydeno_eval() -> None:
    pydeno.configure_default_runtime(isolated=True, request_timeout=2.0)
    with pytest.raises((WorkerCrashed, RuntimeError)):
        pydeno.eval("const a = []; a[2 ** 32 - 2] = 1; a.sort()")
    # the default runtime was closed by the crash; the next call transparently gets a fresh one
    assert pydeno.eval("1 + 1") == 2


def test_isolated_options_are_forwarded() -> None:
    pydeno.configure_default_runtime(isolated=True, sandbox="off", jitless=False)
    rt = pydeno.get_default_runtime()
    assert rt.sandbox == "none"
    assert rt.v8_flags == []
    assert pydeno.eval("typeof WebAssembly") == "object"


def test_a_runtime_config_is_forwarded_to_the_isolated_runtime() -> None:
    pydeno.configure_default_runtime(
        RuntimeConfig(bootstrap="globalThis.boot = 'yes';"), isolated=True
    )
    assert pydeno.eval("boot") == "yes"


def test_a_runtime_config_is_used_for_the_plain_runtime_too() -> None:
    pydeno.configure_default_runtime(
        RuntimeConfig(bootstrap="globalThis.boot = 'plain';")
    )
    assert type(pydeno.get_default_runtime()) is Runtime
    assert pydeno.eval("boot") == "plain"


def test_isolated_only_options_are_refused_without_isolated() -> None:
    with pytest.raises(TypeError, match="isolated=True"):
        pydeno.configure_default_runtime(sandbox="require")


def test_bind_function_goes_through_the_isolated_default() -> None:
    pydeno.configure_default_runtime(isolated=True)
    pydeno.bind_function("add", lambda a, b: a + b)
    assert pydeno.eval("add(40, 2)") == 42


def test_bind_object_goes_through_the_isolated_default() -> None:
    pydeno.configure_default_runtime(isolated=True)
    pydeno.bind_object("api", {"version": "1.0", "double": lambda n: n * 2})
    assert pydeno.eval("api.double(21)") == 42
    assert pydeno.eval("api.version") == "1.0"


def test_eval_async_goes_through_the_isolated_default() -> None:
    pydeno.configure_default_runtime(isolated=True)

    async def go() -> object:
        return await pydeno.eval_async("Promise.resolve(7).then(x => x * 6)")

    assert asyncio.run(go()) == 42


def test_each_thread_gets_its_own_worker() -> None:
    pydeno.configure_default_runtime(isolated=True)
    pids: dict[str, int] = {}

    def work(name: str) -> None:
        pydeno.eval(f"globalThis.me = {name!r}")
        pids[name] = pydeno.get_default_runtime()._proc.pid  # noqa: SLF001
        assert pydeno.eval("me") == name
        pydeno.close_default_runtime()

    threads = [threading.Thread(target=work, args=(n,)) for n in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(set(pids.values())) == 3


def test_close_default_runtime_stops_the_worker() -> None:
    pydeno.configure_default_runtime(isolated=True)
    rt = pydeno.get_default_runtime()
    proc = rt._proc  # noqa: SLF001
    pydeno.close_default_runtime()
    assert proc.poll() is not None


def test_reconfiguring_does_not_disturb_a_runtime_that_already_exists() -> None:
    first = pydeno.get_default_runtime()
    pydeno.configure_default_runtime(isolated=True)
    assert (
        pydeno.get_default_runtime() is first
    )  # still the in-process one, until it is closed
    pydeno.close_default_runtime()
    assert isinstance(pydeno.get_default_runtime(), IsolatedRuntime)


def test_switching_back_restores_the_plain_runtime() -> None:
    pydeno.configure_default_runtime(isolated=True)
    pydeno.close_default_runtime()
    pydeno.configure_default_runtime()
    assert type(pydeno.get_default_runtime()) is Runtime
