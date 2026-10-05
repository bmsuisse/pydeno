"""Opt-in caps count process lifetimes, including checked-out workers."""

import gc
import os
import signal
import threading
import time

import pytest

import pydeno


def test_sync_cap_waits_then_times_out():
    with pydeno.SandboxPool(size=1, max_workers=2, checkout_timeout=0.1) as pool:
        first = pool.checkout()
        second = pool.checkout()
        try:
            start = time.monotonic()
            with pytest.raises(pydeno.CheckoutTimeout):
                pool.checkout()
            assert time.monotonic() - start >= 0.09
        finally:
            first.close()
            second.close()


@pytest.mark.parametrize("release", ["close", "crash", "gc", "limit"])
def test_sync_slot_release(release):
    with pydeno.SandboxPool(size=1, max_workers=1, checkout_timeout=3) as pool:
        rt = pool.checkout(request_timeout=0.05)
        if release == "close":
            rt.close()
        elif release == "crash":
            os.kill(rt._proc.pid, signal.SIGKILL)
        elif release == "limit":
            with pytest.raises(pydeno.RuntimeTimeout):
                rt.eval("while(true) {}")
        else:
            del rt
            gc.collect()
        with pool.checkout() as replacement:
            assert replacement.eval("6*7") == 42


@pytest.mark.asyncio
async def test_async_cap_and_release():
    async with pydeno.AsyncSandboxPool(
        size=1, max_workers=2, checkout_timeout=0.1
    ) as pool:
        first = await pool.checkout()
        second = await pool.checkout()
        try:
            with pytest.raises(pydeno.CheckoutTimeout):
                await pool.checkout()
            await first.close()
            async with pool.checkout() as replacement:
                assert await replacement.eval("6*7") == 42
        finally:
            await first.close()
            await second.close()


@pytest.mark.parametrize("pool_type", [pydeno.SandboxPool, pydeno.AsyncSandboxPool])
@pytest.mark.parametrize(
    "options,exception",
    [
        ({"max_workers": True}, TypeError),
        ({"max_workers": 0}, ValueError),
        ({"checkout_timeout": None}, TypeError),
        ({"checkout_timeout": float("inf")}, ValueError),
        ({"checkout_timeout": 0}, ValueError),
    ],
)
def test_validation(pool_type, options, exception):
    with pytest.raises(exception):
        pool_type(**options)


def test_close_lets_an_existing_sync_waiter_through():
    with pydeno.SandboxPool(size=1, max_workers=1, checkout_timeout=3) as pool:
        held = pool.checkout()
        acquired = []

        def checkout():
            acquired.append(pool.checkout())

        thread = threading.Thread(target=checkout)
        thread.start()
        time.sleep(0.05)
        assert not acquired
        held.close()
        thread.join(5)
        assert not thread.is_alive()
        acquired[0].close()


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["crash", "gc", "limit"])
async def test_async_slot_release(release):
    async with pydeno.AsyncSandboxPool(
        size=1, max_workers=1, checkout_timeout=3
    ) as pool:
        rt = await pool.checkout(request_timeout=0.05)
        if release == "crash":
            os.kill(rt._proc.pid, signal.SIGKILL)
        elif release == "limit":
            with pytest.raises(pydeno.RuntimeTimeout):
                await rt.eval("while(true) {}")
        else:
            del rt
            gc.collect()
        async with pool.checkout() as replacement:
            assert await replacement.eval("6*7") == 42


def test_fork_resets_count_of_checked_out_processes():
    with pydeno.SandboxPool(size=1, max_workers=1, checkout_timeout=0.5) as pool:
        held = pool.checkout()
        pid = os.fork()
        if pid == 0:
            try:
                with pool.checkout() as rt:
                    assert rt.eval("42") == 42
                os._exit(0)
            except BaseException:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        held.close()
        assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.parametrize("custom", ["memory", "seed"])
def test_front_custom_runtime_respects_cap(custom):
    from dataclasses import replace

    with pydeno.Pydeno(min_processes=1, max_workers=1, checkout_timeout=0.1) as pool:
        held = pool._runtime(pool._limits)
        try:
            limits = (
                replace(pool._limits, max_memory=pool._limits.max_memory + 1024)
                if custom == "memory"
                else pool._limits
            )
            with pytest.raises(pydeno.CheckoutTimeout):
                pool._runtime(limits, seed=1 if custom == "seed" else None)
        finally:
            held.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", ["memory", "seed"])
async def test_async_front_custom_runtime_respects_cap(custom):
    from dataclasses import replace

    async with pydeno.AsyncPydeno(
        min_processes=1, max_workers=1, checkout_timeout=0.1
    ) as pool:
        held = await pool._runtime(pool._limits)
        try:
            limits = (
                replace(pool._limits, max_memory=pool._limits.max_memory + 1024)
                if custom == "memory"
                else pool._limits
            )
            with pytest.raises(pydeno.CheckoutTimeout):
                await pool._runtime(limits, seed=1 if custom == "seed" else None)
        finally:
            await held.close()


def test_custom_runtime_can_replace_ready_capacity():
    with pydeno.Pydeno(min_processes=2, max_workers=1, checkout_timeout=3) as pool:
        rt = pool._runtime(pool._limits, seed=123)
        try:
            assert rt.eval("42") == 42
        finally:
            rt.close()


@pytest.mark.asyncio
async def test_async_custom_runtime_can_replace_ready_capacity():
    async with pydeno.AsyncPydeno(
        min_processes=2, max_workers=1, checkout_timeout=3
    ) as pool:
        rt = await pool._runtime(pool._limits, seed=123)
        try:
            assert await rt.eval("42") == 42
        finally:
            await rt.close()


def test_async_fork_can_restart_with_zero_counted_workers():
    import subprocess
    import sys

    script = """
import asyncio, os
from pydeno import AsyncSandboxPool
async def parent():
    pool = await AsyncSandboxPool(size=1, max_workers=1, checkout_timeout=2).start()
    held = await pool.checkout()
    pid = os.fork()
    if pid == 0:
        async def child():
            await pool.start()
            async with pool.checkout() as rt:
                assert await rt.eval('42') == 42
            await pool.close()
        try:
            asyncio.set_event_loop(None)
            asyncio.events._set_running_loop(None)
            asyncio.run(child())
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    await held.close()
    await pool.close()
    assert os.waitstatus_to_exitcode(status) == 0
asyncio.run(parent())
"""
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr


def test_closed_flag_does_not_release_a_live_process():
    with pydeno.SandboxPool(size=1, max_workers=1, checkout_timeout=0.1) as pool:
        rt = pool.checkout()
        rt._closed = True  # front-door reaper marks closed before process exit
        try:
            assert rt._proc.poll() is None
            with pytest.raises(pydeno.CheckoutTimeout):
                pool.checkout()
        finally:
            rt._closed = False
            rt.close()


@pytest.mark.asyncio
async def test_cancelled_async_waiter_leaves_capacity_available():
    import asyncio

    async with pydeno.AsyncSandboxPool(
        size=1, max_workers=1, checkout_timeout=3
    ) as pool:
        rt = await pool.checkout()
        waiter = asyncio.ensure_future(pool.checkout())
        await asyncio.sleep(0.03)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await rt.close()
        async with pool.checkout() as replacement:
            assert await replacement.eval("42") == 42


@pytest.mark.asyncio
async def test_cancelled_async_start_keeps_its_reservation(monkeypatch):
    import asyncio
    import pydeno._aio as aio

    async with pydeno.AsyncSandboxPool(
        size=1, max_workers=2, checkout_timeout=0.1
    ) as pool:
        held = await pool.checkout()
        for task in pool._tasks:
            task.cancel()
        await asyncio.gather(*pool._tasks, return_exceptions=True)
        entered = threading.Event()
        release = threading.Event()
        real_spawn = aio._spawn

        def delayed_spawn(*args):
            entered.set()
            release.wait(5)
            return real_spawn(*args)

        monkeypatch.setattr(aio, "_spawn", delayed_spawn)
        starting = asyncio.ensure_future(pool.checkout())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting
            with pytest.raises(pydeno.CheckoutTimeout):
                await pool.checkout()
        finally:
            release.set()
            await held.close()
            await asyncio.sleep(0.3)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_custom_front_runtime_does_not_refill_global_spare(asynchronous):
    import subprocess
    import sys

    script = """
import asyncio
from pydeno import Pydeno, AsyncPydeno
from pydeno import _isolated
if ASYNC:
    async def run():
        async with AsyncPydeno(min_processes=1, max_workers=1) as pool:
            rt = await pool._runtime(pool._limits, seed=7)
            await asyncio.sleep(.2)
            assert _isolated._SPARE is None
            await rt.close()
    asyncio.run(run())
else:
    import time
    with Pydeno(min_processes=1, max_workers=1) as pool:
        rt = pool._runtime(pool._limits, seed=7)
        time.sleep(.2)
        assert _isolated._SPARE is None
        rt.close()
""".replace("ASYNC", str(asynchronous))
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
