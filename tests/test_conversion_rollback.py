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
            with pytest.raises((ValueError, OverflowError, RuntimeError)):
                rt.eval("[() => 1, new Date(8.64e15), () => 2, new ReadableStream()]")
        gc.collect()
        rt.eval("0")  # finalizers of handles wrapped before the failure have run by now
        assert handles(rt) == (0, 0)


@pytest.mark.asyncio
async def test_deadline_during_a_successful_conversion_releases_handles() -> None:
    with Runtime() as rt:
        rt.eval("globalThis.big = Array.from({length: 300000}, (_, i) => () => i); 0")
        for _ in range(3):
            with pytest.raises(Exception, match="timed out"):  # noqa: PT011
                await rt.eval_async("Promise.resolve(big)", timeout=0.001)
            assert handles(rt) == (0, 0)


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
