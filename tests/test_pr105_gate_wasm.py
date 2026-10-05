"""Regressions for PR #105 gate ownership and retryable WASM unload."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from pydeno import AsyncIsolatedRuntime, GateContext, IsolatedRuntime, Verdict
from pydeno._wasm import AsyncWasmModule, WasmModule


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_sync_import_restores_gate_base_exception(error):
    stopped = error("gate stopped")

    def gate(source: str, context: GateContext) -> Verdict:
        if context.mode == "module_loader":
            raise stopped
        return Verdict(True, "allowed")

    with IsolatedRuntime(gate=gate, request_timeout=2) as runtime:
        runtime.set_module_resolver(lambda spec, ref: spec)
        runtime.set_module_loader(lambda spec: "export const value = 1;")
        with pytest.raises(error) as caught:
            runtime.eval_module("loaded:stop")
        assert caught.value is stopped
        assert runtime.eval("1 + 1") == 2


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
async def test_async_import_restores_gate_base_exception(error):
    stopped = error("gate stopped")

    async def gate(source: str, context: GateContext) -> Verdict:
        if context.mode == "module_loader":
            raise stopped
        return Verdict(True, "allowed")

    async with AsyncIsolatedRuntime(gate=gate, request_timeout=2) as runtime:
        await runtime.set_module_resolver(lambda spec, ref: spec)
        await runtime.set_module_loader(lambda spec: "export const value = 1;")
        with pytest.raises(error) as caught:
            await runtime.eval("import('loaded:stop').then(() => 1, () => 'caught')")
        assert caught.value is stopped
        if error is asyncio.CancelledError:
            assert runtime.is_closed  # cancellation retains the runtime's kill policy
        else:
            assert await runtime.eval("1 + 1") == 2


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
async def test_async_wasm_unload_can_retry_after_failure(error):
    attempts = 0

    async def unload():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error("unload interrupted")

    module = AsyncWasmModule({}, None, unload)
    with pytest.raises(error):
        await module.unload()
    assert not module._closed
    await module.unload()
    await module.unload()
    assert attempts == 2
    assert module._closed


def test_sync_wasm_unload_can_retry_after_failure():
    attempts = 0

    def unload():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("unload interrupted")

    module = WasmModule({}, None, unload)
    with pytest.raises(OSError):
        module.unload()
    assert not module._closed
    module.unload()
    module.unload()
    assert attempts == 2
    assert module._closed


if hasattr(os, "fork"):

    @pytest.mark.parametrize("configure_first", [False, True])
    def test_gate_submission_after_fork_with_inherited_held_lock(configure_first):
        # Alarm bounds the child; subprocess timeout bounds the whole fork probe.
        probe = """
import os
import signal
import threading
from pydeno import _gate
pool = _gate._GATE_POOL
held = threading.Event()
release = threading.Event()
def hold():
    with pool.lock:
        held.set()
        release.wait(8)
thread = threading.Thread(target=hold)
thread.start()
assert held.wait(2)
child = os.fork()
if child == 0:
    signal.alarm(3)
    if os.environ.get("CONFIGURE_GATE_FIRST") == "1":
        _gate.set_gate_threads(2)
    value = pool.submit(lambda: 42).result(timeout=2)
    os._exit(0 if value == 42 else 1)
try:
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0, status
finally:
    release.set()
    thread.join(2)
"""
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "CONFIGURE_GATE_FIRST": str(int(configure_first))},
        )
        assert result.returncode == 0, result.stderr
