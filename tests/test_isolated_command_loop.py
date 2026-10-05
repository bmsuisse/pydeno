"""Consecutive async commands on one worker share the worker's event loop (issue #60).

The worker used to start a fresh `asyncio.run` per async command, so whatever a command left on
its loop died with it. It now keeps one loop and empties it after every command. These tests pin
that nothing of one command reaches the next through that loop: a host call the guest started and
never awaited is cancelled with its command (as closing the loop used to do), its late reply
changes nothing, and the guest-visible behaviour of microtasks, rejections and errors across
commands is what it was. (The first two tests fail if the worker stops emptying its loop.)
"""

from __future__ import annotations

import asyncio

import pytest

from pydeno import IsolatedRuntime, JavaScriptError, RuntimeConfig


def _iso() -> IsolatedRuntime:
    return IsolatedRuntime(RuntimeConfig(timeout=5.0))


async def test_unawaited_host_call_ends_with_its_command() -> None:
    gate = asyncio.Event()
    started: list[str] = []

    async def held(tag: str) -> str:
        started.append(tag)
        await gate.wait()
        return tag

    with _iso() as rt:
        rt.bind_function("held", held)
        # The command returns while its host call is still out.
        assert (
            await rt.eval_async(
                "globalThis.p = held('a').then(v => 'ok:' + v, e => 'err:' + e.name); 1"
            )
            == 1
        )
        # The next command does not wait for that call: the gate is still closed, so a command
        # that did would time out.
        assert await rt.eval_async("2") == 2
        gate.set()
        await asyncio.sleep(0.2)  # the host's late reply reaches the worker meanwhile
        # The call was cancelled with command 1: the late value is never delivered.
        result = await rt.eval_async("p")
        assert isinstance(result, str) and result.startswith("err:"), result
        assert started == ["a"]


async def test_leftover_calls_do_not_accumulate() -> None:
    gate = asyncio.Event()

    async def held(i: int) -> int:
        await gate.wait()
        return i

    with _iso() as rt:
        rt.bind_function("held", held)
        rt.eval("globalThis.settled = []")
        for i in range(30):
            code = f"held({i}).then(v => settled.push('ok' + v), () => settled.push('err')); {i}"
            assert await rt.eval_async(code) == i
            assert rt.eval(f"{i} + 1") == i + 1  # sync commands in between
        gate.set()
        await asyncio.sleep(0.2)
        assert await rt.eval_async("held(7)") == 7  # the loop still serves new calls
        await asyncio.sleep(0.1)
        settled = await rt.eval_async("Promise.resolve(settled)")
        assert settled == ["err"] * 30, settled


async def test_guest_semantics_across_commands() -> None:
    with _iso() as rt:
        # A microtask queued by a command runs within that command.
        await rt.eval_async(
            "queueMicrotask(() => { globalThis.m = (globalThis.m || 0) + 1 }); 1"
        )
        assert await rt.eval_async("globalThis.m") == 1
        # An unhandled rejection fails its own command, and only that one; so does a throw.
        for code in ("Promise.reject(new Error('x')); 1", "throw new Error('boom')"):
            with pytest.raises(JavaScriptError):
                await rt.eval_async(code)
            assert await rt.eval_async("Promise.resolve(3)") == 3
        rt.add_static_module("m", "export const a = 1;")
        for _ in range(3):
            assert (await rt.eval_module_async("m"))["a"] == 1
            assert await rt.eval_async("Promise.resolve(4)") == 4
