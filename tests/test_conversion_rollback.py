"""A result that fails to convert leaves no function or stream handle behind.

Converting a JS value registers each function and `ReadableStream` it meets, so Python can call or
read it later. When a later part of the same value cannot be converted, the caller gets an error
and never sees those ids, so nothing would ever release them: repeating the failing call would
grow the runtime's registries without bound. The conversion is all or nothing.
"""

from __future__ import annotations

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
