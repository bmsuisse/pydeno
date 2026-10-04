"""A result the caller never receives leaves no function or stream handle behind.

Converting a JS value registers each function and `ReadableStream` it meets, so Python can call or
read it later. When the caller gets an error instead of the value (a later part cannot be
converted, on either side of the boundary, or the deadline passed meanwhile) or has stopped
waiting, it never sees those ids, so nothing else would release them: repeating the call would
grow the runtime's registries without bound.
"""

from __future__ import annotations

import asyncio
import gc
import random
import time

import pytest

from pydeno import Runtime, RuntimeConfig

FAILING = [
    "({f: () => 1, m: new Map()})",
    "[() => 1, {g() {}}, Symbol('x')]",
    "new Set([() => 1, new WeakMap()])",
    "({s: new ReadableStream(), e: new Error('x')})",
    "[new ReadableStream(), () => 1, new ReadableStream(), new Map()]",
    "({f() {}, get boom() { throw new Error('getter'); }})",
    "(() => { const o = {f() {}}; o.self = o; return o; })()",
]


def handles(rt: Runtime) -> tuple[int, int]:
    return rt._debug_function_handle_count(), rt.get_stats().active_js_streams


@pytest.mark.parametrize("code", FAILING)
def test_failed_eval_releases_handles(code: str) -> None:
    with Runtime() as rt:
        for _ in range(200):
            with pytest.raises(RuntimeError):
                rt.eval(code)
        assert handles(rt) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", FAILING)
async def test_failed_eval_async_releases_handles(code: str) -> None:
    with Runtime() as rt:
        for _ in range(50):
            with pytest.raises(RuntimeError):
                await rt.eval_async(f"(async () => {code})()")
        assert handles(rt) == (0, 0)


def test_byte_limit_failure_releases_handles() -> None:
    with Runtime(RuntimeConfig(max_serialization_bytes=1024)) as rt:
        for _ in range(50):
            with pytest.raises(RuntimeError, match="Serialization size"):
                rt.eval("[() => 1, new ReadableStream(), 'x'.repeat(4096)]")
        assert handles(rt) == (0, 0)


def test_successful_conversion_keeps_its_handles() -> None:
    with Runtime() as rt:
        with pytest.raises(RuntimeError):
            rt.eval("({f: () => 1, m: new Map()})")
        value = rt.eval("({f: () => 41, g: [() => 1]})")
        assert handles(rt) == (2, 0)
        assert value["f"]() == 41


def test_python_side_failure_releases_unwrapped_handles() -> None:
    # Converts on the runtime thread, then fails in Python (the date is past year 9999).
    with Runtime() as rt:
        for _ in range(50):
            # Windows reports an out-of-range date as OSError, other platforms as ValueError/OverflowError.
            with pytest.raises((ValueError, OverflowError, OSError, RuntimeError)):
                rt.eval("[() => 1, new Date(8.64e15), () => 2, new ReadableStream()]")
        gc.collect()
        rt.eval("0")  # finalizers of handles wrapped before the failure have run by now
        assert handles(rt) == (0, 0)


@pytest.mark.asyncio
async def test_deadline_during_a_successful_conversion_releases_handles() -> None:
    with Runtime() as rt:
        rt.eval("globalThis.big = Array.from({length: 300000}, (_, i) => () => i); 0")
        timed_out = 0
        # A fast machine can convert the result inside a millisecond, so tighten the deadline until it
        # fires; a result that does arrive is dropped and must release its handles too.
        for timeout in (1e-3, 5e-4, 2e-4, 1e-4, 5e-5, 2e-5, 1e-5, 1e-6) * 3:
            try:
                result = await rt.eval_async("Promise.resolve(big)", timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                assert "timed out" in str(exc)  # noqa: PT017
                timed_out += 1
            else:
                del result
            gc.collect()
            rt.eval("0")
            deadline = time.monotonic() + 5
            while handles(rt) != (0, 0) and time.monotonic() < deadline:
                gc.collect()
                rt.eval("0")
                await asyncio.sleep(0.01)
            assert handles(rt) == (0, 0)
        assert timed_out, "the deadline never fired during the conversion"


@pytest.mark.asyncio
async def test_result_of_an_abandoned_eval_async_is_released() -> None:
    async def later() -> None:
        await asyncio.sleep(0.03)

    with Runtime() as rt:
        rt.bind_function("later", later)
        for _ in range(10):
            task = asyncio.ensure_future(
                rt.eval_async(
                    "later().then(() => ({f: () => 1, s: new ReadableStream()}))"
                )
            )
            await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0.2)
        await rt.eval_async("later()")
        assert handles(rt) == (0, 0)


async def _settled(rt: Runtime, seconds: float = 1.0) -> tuple[int, int]:
    """Handle counts once releases already on their way have landed (or `seconds` passed)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        gc.collect()
        await rt.eval_async(
            "0"
        )  # one round trip: earlier release commands are processed
        counts = handles(rt)
        if counts == (0, 0) or loop.time() > end:
            return counts
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_cancelling_eval_async_at_any_moment_releases_its_result() -> None:
    # Cancel each call after a random delay around its latency, so some are cancelled while the
    # value is already on its way to Python: that value must be released too.
    code = "Promise.resolve({f: () => 1, s: new ReadableStream()})"
    with Runtime() as rt:
        start = time.perf_counter()
        for _ in range(20):
            await rt.eval_async(code)
        latency = (time.perf_counter() - start) / 20
        assert await _settled(rt) == (0, 0)
        loop = asyncio.get_running_loop()
        for i in range(2000):
            task = asyncio.ensure_future(rt.eval_async(code))
            loop.call_later(random.uniform(0, 2 * latency), task.cancel)
            try:
                await task
            except asyncio.CancelledError:
                pass
            task = None
            assert await _settled(rt) == (0, 0), f"iteration {i} left handles behind"
