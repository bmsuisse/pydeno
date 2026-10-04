"""Converting a guest result must be charged before it is expanded (issue #75, slice A).

Typed arrays other than `Uint8Array` (and `DataView`s, and boxed strings) have no dedicated branch
in the V8 -> JSValue converter, so they go through the generic object branch, which first asks V8
for every own property name. Those names are virtual: an `Int8Array` of 2**24 elements is 16 MB of
storage and 16 million index strings once listed, hundreds of megabytes of heap. The listing is
one native call that termination cannot interrupt, so a 2-second deadline ran for 7 to 12 seconds.
The serialization byte budget was only charged per key afterwards, too late to help.

Arrays already charge their length before walking; these values now do the same.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from pydeno import IsolatedRuntime, Runtime, RuntimeConfig

TIMEOUT = 10.0
# Far below the deadline, far above what refusing up front costs.
QUICK = 3.0

_INDEXED = [
    "new Int8Array(2 ** 24)",
    "new Float64Array(2 ** 24)",
    "new Uint16Array(2 ** 24)",
    "new Uint8ClampedArray(2 ** 24)",
    "new String('x'.repeat(2 ** 24))",
]


@pytest.mark.parametrize("expr", _INDEXED)
def test_a_huge_indexed_result_is_refused_before_it_is_listed(expr: str) -> None:
    with Runtime(RuntimeConfig(timeout=TIMEOUT)) as rt:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval(expr)
        assert time.monotonic() - started < QUICK


def test_a_huge_typed_array_result_is_refused_by_the_isolated_worker() -> None:
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT), request_timeout=TIMEOUT * 2
    ) as rt:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval("new Float64Array(2 ** 24)")
        assert time.monotonic() - started < QUICK
        assert rt.eval("1 + 1") == 2  # the session survives the refusal


def test_small_typed_arrays_and_boxed_strings_convert_as_before() -> None:
    with Runtime() as rt:
        assert rt.eval("new Float64Array([1.5, 2])") == {"0": 1.5, "1": 2}
        assert rt.eval("new Int16Array([-1])") == {"0": -1}
        assert rt.eval("new String('ab')") == {"0": "a", "1": "b"}
        assert rt.eval("new Uint8Array([1, 2])") == b"\x01\x02"


@pytest.mark.parametrize("expr", ["2 ** 63", "2 ** 64", "-(2 ** 64)", "1e300"])
def test_a_number_outside_int64_is_not_silently_clamped(expr: str) -> None:
    """`2 ** 63` used to come back as `2 ** 63 - 1`: the float-to-int cast saturates, and the
    round-trip check compared the saturated value after converting it back to a float, where
    `i64::MAX as f64` rounds up to `2 ** 63` again."""
    with Runtime() as rt:
        out = rt.eval(expr)
        assert out == float(rt.eval(f"String({expr})"))
        assert out != 2**63 - 1


def test_int64_boundaries_still_come_back_as_ints() -> None:
    with Runtime() as rt:
        assert rt.eval("-(2 ** 63)") == -(2**63)
        assert rt.eval("2 ** 53") == 2**53
        assert isinstance(rt.eval("2 ** 62"), int)


@pytest.mark.parametrize("factory", ["inprocess", "isolated"])
def test_a_huge_boxed_string_argument_is_refused_before_it_is_listed(
    factory: str,
) -> None:
    """The bridge copies a host-call argument with `Object.entries`, which lists a boxed string's
    characters in one native call; the node cap was only checked per entry afterwards."""
    if factory == "inprocess":
        rt = Runtime(RuntimeConfig(timeout=TIMEOUT))
    else:
        rt = IsolatedRuntime(
            RuntimeConfig(timeout=TIMEOUT), request_timeout=TIMEOUT * 2
        )
    with rt:
        rt.bind_function("f", lambda *a: len(a))
        started = time.monotonic()
        with pytest.raises(Exception, match="too large"):
            rt.eval("f(new String('x'.repeat(2 ** 24)))")
        assert time.monotonic() - started < QUICK
        assert rt.eval("f(new String('ab'))") == 1  # small ones still cross


# --- a Proxy around the value, and the budget/cap arithmetic (review of #80) -------------------------------
# The in-process cases run in a child process with a hard timeout: before the fix, a Proxy around a huge
# typed array passed to a host function ran V8 out of memory, which aborts the whole process.

_CHILD = r"""
import asyncio, sys, time
from pydeno import Runtime, RuntimeConfig

mode, expr, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
kwargs = {"timeout": 10.0}
if budget:
    kwargs["max_serialization_bytes"] = budget
started = time.monotonic()
try:
    with Runtime(RuntimeConfig(**kwargs)) as rt:
        rt.bind_function("f", lambda *a: len(a))
        if mode == "eval":
            out = rt.eval(expr)
        elif mode == "async":

            async def run():
                return await rt.eval_async(expr, timeout=10.0)

            out = asyncio.run(run())
        else:  # stream: a JS ReadableStream whose one chunk is `expr`

            async def read():
                stream = await rt.eval_async(
                    "(async () => new ReadableStream({start(c) { c.enqueue("
                    + expr
                    + "); c.close(); }}))()"
                )
                return [chunk async for chunk in stream]

            out = asyncio.run(read())
    print("OK", type(out).__name__, len(repr(out)))
except Exception as exc:
    print("ERR", type(exc).__name__, str(exc)[:200].replace("\n", " "))
print("TOOK", round(time.monotonic() - started, 2))
# What is checked is the conversion, not interpreter teardown (an in-process Runtime that an asyncio
# worker thread drops after a failed call can abort at exit): leave without running it.
sys.stdout.flush()
import os

os._exit(0)
"""


def _child(mode: str, expr: str, budget: int = 0) -> tuple[str, float]:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, mode, expr, str(budget)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"child died ({proc.returncode}): {proc.stderr[-500:]}"
    lines = proc.stdout.strip().splitlines()
    return lines[-2], float(lines[-1].split()[1])


_PROXIED = [
    "new Proxy(new Int8Array(2 ** 24), {})",
    "new Proxy(new String('x'.repeat(2 ** 24)), {})",
    "new Proxy(new Proxy(new Float64Array(2 ** 24), {}), {})",
]


@pytest.mark.parametrize("mode", ["eval", "async", "stream"])
@pytest.mark.parametrize("expr", _PROXIED)
def test_a_proxy_around_a_huge_indexed_result_is_refused_up_front(
    mode: str, expr: str
) -> None:
    outcome, took = _child(
        mode, f"Promise.resolve({expr})" if mode == "async" else expr
    )
    assert outcome.startswith("ERR"), outcome
    assert "Serialization size" in outcome, outcome
    assert took < QUICK, (took, outcome)


@pytest.mark.parametrize("expr", _PROXIED)
def test_a_proxy_around_a_huge_indexed_argument_is_refused_up_front(expr: str) -> None:
    outcome, took = _child("eval", f"f({expr})")
    assert outcome.startswith("ERR"), outcome
    # The bridge's argument cap, or (a typed array reaching the host as bytes) the host's byte limit.
    assert "too large" in outcome or "Serialization size" in outcome, outcome
    assert took < QUICK, (took, outcome)


# --- round 2 of the review: traps that run during conversion ----------------------------------------------
# Conversion now unwraps a Proxy natively to its innermost target and converts that, running no trap:
# an `ownKeys` trap that grows a resizable buffer after any check, a Proxy that hides an Array from the
# array branch, or one that answers `[]` while V8 lists its target to check the invariants, all had a
# trap or a native listing the budget did not see.

_GROWING = (
    "(() => { const b = new ArrayBuffer(0, {maxByteLength: 2 ** 24}); const ta = new Int8Array(b);"
    " return new Proxy(ta, {ownKeys(t) { b.resize(2 ** 24); return [] }}) })()"
)
_ROUND2 = {
    "trap_grows_buffer": _GROWING,
    "nested_trap_grows_buffer": f"new Proxy({_GROWING}, {{}})",
    "proxy_around_array": "new Proxy(new Array(2 ** 22).fill(0), {})",
    "empty_ownkeys_repeated": (
        "(() => { const p = new Proxy(new Int8Array(2 ** 20 - 1), {ownKeys() { return [] }});"
        " return Array(200).fill(p) })()"
    ),
}


def _small_or_refused(outcome: str) -> bool:
    if outcome.startswith("ERR"):
        return True
    return int(outcome.split()[2]) < 10_000


@pytest.mark.parametrize("mode", ["eval", "async", "stream", "arg"])
@pytest.mark.parametrize("name", sorted(_ROUND2))
def test_a_proxy_trap_cannot_slip_work_past_the_budget(mode: str, name: str) -> None:
    expr = _ROUND2[name]
    source = {
        "eval": expr,
        "async": f"Promise.resolve({expr})",
        "stream": expr,
        "arg": f"f({expr})",
    }[mode]
    outcome, took = _child(
        "stream" if mode == "stream" else "eval" if mode == "arg" else mode, source
    )
    assert took < QUICK, (took, outcome)
    assert _small_or_refused(outcome), outcome


def test_conversion_runs_no_proxy_trap() -> None:
    with Runtime(RuntimeConfig(timeout=TIMEOUT)) as rt:
        rt.bind_function("f", lambda *a: a[0])
        rt.eval(
            "globalThis.hits = 0; globalThis.trap = () => { hits++; return 'TRAP' }; 0"
        )
        handler = "{get: trap, ownKeys() { hits++; return ['a'] }, getOwnPropertyDescriptor() { hits++ }}"
        assert rt.eval(f"new Proxy({{a: 1}}, {handler})") == {"a": 1}
        assert rt.eval(f"f(new Proxy({{a: 1}}, {handler}))") == {"a": 1}
        assert rt.eval(f"new Proxy([1, 2], {handler})") == [1, 2]
        assert rt.eval("hits") == 0


def test_a_revoked_proxy_and_a_deep_proxy_chain_are_refused_by_name() -> None:
    with Runtime() as rt:
        rt.bind_function("f", lambda *a: a[0])
        revoked = "(() => { const r = Proxy.revocable({}, {}); r.revoke(); return r.proxy })()"
        deep = "(() => { let p = {}; for (let i = 0; i < 100; i++) p = new Proxy(p, {}); return p })()"
        for source in (revoked, f"f({revoked})"):
            with pytest.raises(Exception, match="revoked Proxy"):
                rt.eval(source)
        for source in (deep, f"f({deep})"):
            with pytest.raises(Exception, match="Proxy chain too deep"):
                rt.eval(source)


def test_a_proxy_around_a_huge_typed_array_is_refused_by_the_isolated_worker() -> None:
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT), request_timeout=TIMEOUT * 2
    ) as rt:
        rt.bind_function("f", lambda *a: len(a))
        for expr in (_PROXIED[0], f"f({_PROXIED[0]})"):
            started = time.monotonic()
            with pytest.raises(Exception, match="Serialization size|too large"):
                rt.eval(expr)
            assert time.monotonic() - started < QUICK
        assert rt.eval("1 + 1") == 2


def test_small_proxied_values_still_convert() -> None:
    with Runtime() as rt:
        rt.bind_function("f", lambda *a: a[0])
        assert rt.eval("new Proxy(new Float64Array([1.5]), {})") == {"0": 1.5}
        assert rt.eval("new Proxy(new String('ab'), {})") == {"0": "a", "1": "b"}
        assert rt.eval("f(new Proxy(new String('ab'), {}))") == {"0": "a", "1": "b"}
        assert rt.eval("f(new Proxy({a: 1}, {}))") == {"a": 1}
        assert rt.eval("new Proxy([1, 2], {})") == [1, 2]  # its target, an array


def _largest_accepted(rt: Runtime, template: str, high: int) -> int:
    low = 0
    while low < high:
        mid = (low + high + 1) // 2
        try:
            rt.eval(template % mid)
            low = mid
        except RuntimeError:
            high = mid - 1
    return low


@pytest.mark.parametrize(
    ("indexed", "plain"),
    [
        ("new Int8Array(%d)", "Object.assign({}, new Int8Array(%d))"),
        (
            "new String('x'.repeat(%d))",
            "Object.assign({}, new String('x'.repeat(%d)))",
        ),
    ],
)
def test_the_up_front_check_does_not_lower_the_documented_budget(
    indexed: str, plain: str
) -> None:
    """The up-front charge is a check, not an extra cost: an indexed value is accepted exactly
    when a plain object with the same keys and values is."""
    with Runtime(RuntimeConfig(max_serialization_bytes=1_000_000)) as rt:
        n = _largest_accepted(rt, plain, 400_000)
        assert n > 0
        rt.eval(indexed % n)  # must not raise
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval(indexed % (n + 1))


def test_a_raised_budget_does_not_bring_back_the_unbounded_listing() -> None:
    outcome, took = _child("eval", "new Float64Array(2 ** 24)", budget=2**31)
    assert outcome.startswith("ERR"), outcome
    assert "elements" in outcome, outcome
    assert took < QUICK, (took, outcome)
    # (A proxied typed array argument now crosses as its target's bytes, a copy within the raised
    # budget; a boxed string is still listed, so it must still meet the fixed argument cap.)
    outcome, took = _child(
        "eval", "f(new Proxy(new String('x'.repeat(2 ** 24)), {}))", budget=2**31
    )
    assert outcome.startswith("ERR"), outcome
    assert "too large" in outcome, outcome
    assert took < QUICK, (took, outcome)
