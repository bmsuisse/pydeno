"""Consecutive async commands on one worker share the worker's event loop (issue #60).

The worker used to start a fresh `asyncio.run` per async command, so whatever a command left on
its loop died with it. It now keeps one loop and empties it after every command. These tests pin
that nothing of one command is held over on that loop to the next: a host call the guest started
and never awaited is cancelled with its command (as closing the loop used to do), a host call
that finished has its completion run within its own command, other threads cannot queue work on
the loop between commands (it refuses them, as the closed loop did), and the guest-visible
behaviour of microtasks, rejections and errors across commands is what it was.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import pydeno

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
        # The call was cancelled with command 1: the late value is not held over to a later one.
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


async def test_finished_host_calls_settle_within_their_command() -> None:
    """A host call that finishes in the command's last loop pass has its completion queued after
    that pass; it must run before the command ends, not at the start of the next async command
    (under that command's deadline and console budget). What this can catch is probabilistic
    (the window is narrow); what it asserts is not."""

    async def h(tag: int) -> int:
        return tag

    with _iso() as rt:
        rt.bind_function("h", h)
        rt.eval("globalThis.settled = 0")
        held_over = []
        for i in range(25):
            await rt.eval_async(
                "for (let i = 0; i < 50; i++) h(i).then(() => settled++, () => settled++); 0"
            )
            await asyncio.sleep(0.1)  # idle: anything owed settles now
            rt.eval("1")  # a sync command does not run the loop
            await asyncio.sleep(0.05)
            before = rt.eval("settled")
            await rt.eval_async("0")  # the next async command
            await asyncio.sleep(0.05)
            after = rt.eval("settled")
            if before < 50 * (i + 1) and after > before:
                held_over.append((i, 50 * (i + 1) - before))
        assert held_over == []


_IDLE_LOOP_CHECK = textwrap.dedent(
    """
    import asyncio, os, sys, threading
    sys.path.insert(0, sys.argv[1])
    from pydeno._worker import _Worker

    r, w = os.pipe()
    worker = _Worker(r, w)
    fired = []

    async def command():
        # Finishes in the loop's last pass: its done callback is queued after that pass, with no
        # task left pending.
        task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
        task.add_done_callback(lambda _: fired.append("done"))
        return 1

    assert worker._run_async(command) == 1
    assert fired == ["done"], fired

    late = []

    def other_thread():
        try:
            worker._loop.call_soon_threadsafe(late.append, "ran")
            late.append("accepted")
        except RuntimeError as exc:
            late.append(f"refused: {exc}")

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()

    async def nothing():
        return 0

    assert worker._run_async(nothing) == 0
    assert late == ["refused: Event loop is closed"], late
    print("ok")
    """
)


def test_idle_loop_runs_queued_completions_and_refuses_other_threads() -> None:
    # In a child process: importing the worker module changes process-wide state (it marks `ssl`
    # unimportable and starts the Seatbelt precompile), which must not leak into this one.
    root = str(Path(pydeno.__file__).resolve().parent.parent)
    out = subprocess.run(
        [sys.executable, "-c", _IDLE_LOOP_CHECK, root],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def _command_loop_probe(body: str) -> None:
    root = str(Path(pydeno.__file__).resolve().parent.parent)
    script = """
import asyncio, os, sys, threading
sys.path.insert(0, sys.argv[1])
from pydeno._worker import _Worker
r, w = os.pipe()
worker = _Worker(r, w)
""" + textwrap.dedent(body)
    out = subprocess.run(
        [sys.executable, "-c", script, root],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_idle_flag_allows_cancellation_cleanup_to_run() -> None:
    _command_loop_probe("""
        finished = []
        async def held():
            try:
                await asyncio.Event().wait()
            finally:
                finished.append("cancelled")
        async def command():
            asyncio.create_task(held())
            await asyncio.sleep(0)
            return 1
        assert worker._run_async(command) == 1
        assert worker._loop.is_closed()
        assert finished == ["cancelled"], finished
    """)


def test_submission_racing_with_idle_transition_settles_before_next_command() -> None:
    _command_loop_probe("""
        loop = worker._loop
        loop.idle = False
        entered, release, settled = threading.Event(), threading.Event(), threading.Event()
        phases = []
        phase = "settling"
        original = asyncio.SelectorEventLoop.call_soon_threadsafe
        def delayed(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(self, *args, **kwargs)
        asyncio.SelectorEventLoop.call_soon_threadsafe = delayed
        def submit():
            loop.call_soon_threadsafe(lambda: phases.append(phase))
        producer = threading.Thread(target=submit)
        producer.start()
        assert entered.wait(5)
        def settle():
            loop.idle = True
            worker._settle()
            settled.set()
        closer = threading.Thread(target=settle)
        closer.start()
        # With atomic submission, the idle transition waits for the producer. Before the fix,
        # settlement finishes while the producer is still paused before actually enqueueing.
        settled.wait(0.2)
        release.set()
        producer.join(5)
        closer.join(5)
        assert not producer.is_alive() and not closer.is_alive()
        asyncio.SelectorEventLoop.call_soon_threadsafe = original
        phase = "next command"
        async def nothing():
            return 0
        worker._run_async(nothing)
        assert phases == ["settling"], phases
    """)


def test_cancellation_created_tasks_are_settled_before_next_command() -> None:
    _command_loop_probe("""
        cancelled = []
        async def held(generation):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(generation)
                if generation < 3:
                    asyncio.create_task(held(generation + 1))
        async def command():
            asyncio.create_task(held(0))
            await asyncio.sleep(0)
            return 1
        assert worker._run_async(command) == 1
        pending = asyncio.all_tasks(worker._loop)
        assert not pending, pending
        assert cancelled == [0, 1, 2, 3], cancelled
    """)


def test_cancellation_scheduled_timers_do_not_run_in_next_command() -> None:
    _command_loop_probe("""
        fired = []
        cleanup = []
        async def held():
            try:
                await asyncio.Event().wait()
            finally:
                # Timers needed by cancellation cleanup must run before its leftover timer
                # is discarded. This cleanup finishes while that timer is still in the future.
                await asyncio.sleep(0.01)
                cleanup.append("finished")
                asyncio.get_running_loop().call_later(0.1, fired.append, "old command")
        async def command():
            asyncio.create_task(held())
            await asyncio.sleep(0)
            return 1
        assert worker._run_async(command) == 1
        assert cleanup == ["finished"], cleanup
        async def next_command():
            await asyncio.sleep(0.2)
            return 2
        assert worker._run_async(next_command) == 2
        assert fired == [], fired
    """)
