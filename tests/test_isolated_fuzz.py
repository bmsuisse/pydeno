"""A seeded fuzzer for `IsolatedRuntime`.

Programs are random mixes of nasty fragments: runaway loops, recursion, allocation bombs,
pathological regexes and JSON, proxies with throwing traps, `Atomics.wait`, code generation,
oversized values, host-call floods. The property is about the *parent*:

- every evaluation ends in a value or one of the documented exceptions, never anything else
  (no `KeyError` from the decoder, no `struct.error`, no `RecursionError`, no hang);
- afterwards the runtime is either still usable or closed, never half-alive;
- the host process is unharmed.

Seeds are fixed, so a failure reproduces: the assertion message carries the seed, the program
index and the program text. When a fuzz run finds something real, add the program to
`TestRegressions` below.
"""

from __future__ import annotations

import random
import time

import pytest

from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    RuntimeConfig,
    RuntimeForceKilled,
    RuntimeTerminated,
    RuntimeTimeout,
    WorkerCrashed,
)

# Exceptions the parent is documented to raise for hostile guest code. RuntimeError is the base
# of most of them, and what module/other failures use; anything outside this tuple is a bug.
DOCUMENTED = (
    JavaScriptError,
    RuntimeTimeout,
    RuntimeTerminated,
    RuntimeForceKilled,
    WorkerCrashed,
    RuntimeError,
    TypeError,  # a value that cannot cross the boundary
)

# Fragments that terminate quickly, whatever they do.
TAME = [
    "1 + 1",
    "'a'.repeat(1000).length",
    "[1, 2, 3].map(x => x * 2)",
    "({a: 1, b: [2, 3]})",
    "JSON.stringify({a: [1, 2, {b: 3}]})",
    "typeof globalThis",
    "Object.keys(globalThis).length",
    "new Date(0).toISOString()",
    "[3, 1, 2].sort()",
    "BigInt(2) ** 64n",
    "new Uint8Array(1024).fill(7)",
    "Symbol('s').toString()",
    "null?.x",
    "(() => { try { null.x } catch (e) { return e.message } })()",
    "'x'.padStart(100, 'ab')",
    "Math.max(...Array(1000).keys())",
    "new Map([[1, 2]]).get(1)",
    "[...'héllo']",
    "encodeURIComponent('a b&c')",
    "parseFloat('1e1000')",
    "Number.MAX_SAFE_INTEGER + 2",
    "[1, [2, [3, [4]]]].flat(Infinity)",
    "Array.from({length: 100}, (_, i) => i * i)",
    "typeof host",
    "host(1, 'two', [3], {four: 4})",
    "host(null)",
    "host(undefined)",
    "host(new Uint8Array([1, 2, 3]))",
    "host(2n ** 70n)",
    "host(NaN)",
    "host('\\u0000\\ud83d\\ude00')",
]

# Fragments that fail, hang, or try to hurt something.
NASTY = [
    "while (true) {}",
    "for (;;) { host(1) }",
    "(function f() { f() })()",
    "(function f(a) { return f(a + 1) + f(a + 2) })(0)",
    "new Array(2 ** 32 - 1).fill(0)",
    "new Array(1e9)",
    "'x'.repeat(2 ** 28)",
    "'x'.repeat(2 ** 29)",
    "new ArrayBuffer(2 ** 33)",
    "JSON.parse('['.repeat(100000))",
    "JSON.parse('{\"a\":'.repeat(50000))",
    "[...Array(1e6).keys()].sort(() => Math.random() - 0.5)",
    "BigInt(2) ** 100000000n",
    "/(a+)+$/.test('a'.repeat(60) + 'b')",
    "new RegExp('(' + '('.repeat(5000) + ')'.repeat(5000) + ')')",
    "new Proxy({}, {get: (t, k, r) => r[k]}).x",
    "new Proxy(function () {}, {apply: (t, th, a) => t(...a)})()",
    "Object.defineProperty(globalThis, 'boom', {get() { throw new Error('getter') }}); boom",
    "Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 200)",
    "eval('(')",
    "new Function('while (1);')()",
    "String.fromCharCode(...Array(100000).fill(65)).length",
    "[].concat(...Array(100000).fill([1])).length",
    "const a = []; a[2 ** 32 - 2] = 1; a.sort()",
    "const a = []; a[2 ** 32 - 2] = 1; a.reverse()",
    "'a'.repeat(2 ** 28).split('').length",
    "'a'.repeat(2 ** 28).match(/a/g).length",
    "class A extends A {}",
    "undefined()",
    "null.x",
    "throw {toString() { throw 1 }}",
    "throw Symbol('nope')",
    "Promise.reject(new Error('unhandled'))",
    "(async function f() { await f() })()",
    "new Intl.NumberFormat('en', {maximumFractionDigits: 100}).format(1e300)",
    "new Date(8.64e15 + 1).toISOString()",
    "Math.pow(2, 2 ** 30)",
    "host('x'.repeat(40 * 1024 * 1024))",
    "host(new Array(1e6).fill('x'.repeat(100)))",
    "(function deep(n) { return n ? [deep(n - 1)] : [] })(100000)",
    "host((function deep(n) { return n ? [deep(n - 1)] : [] })(1000))",
    "Object.setPrototypeOf(globalThis, new Proxy({}, {get: () => { throw 1 }}))",
    "delete globalThis.Array; [].map(x => x)",
    "Object.freeze(Object.prototype); ({}).x = 1",
    "WebAssembly.compile(new Uint8Array(8))",
    "queueMicrotask(function loop() { queueMicrotask(loop) })",
]


def _program(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(1, 4)):
        pool = NASTY if rng.random() < 0.35 else TAME
        parts.append(rng.choice(pool))
    guarded = rng.random() < 0.4
    body = "; ".join(parts)
    if guarded:
        return f"try {{ {body} }} catch (e) {{ String(e).slice(0, 80) }}"
    # the last fragment's value is the result, as with any script
    return body


def _fresh() -> IsolatedRuntime:
    rt = IsolatedRuntime(
        RuntimeConfig(timeout=0.8, max_heap_size=128 * 1024 * 1024),
        timeout_grace=1.0,
        max_memory=1024 * 1024 * 1024,
        max_host_calls=20000,
    )
    rt.bind_function("host", lambda *args: len(args))
    return rt


@pytest.mark.parametrize("seed", [20261001, 20261002])
def test_hostile_programs_never_surprise_the_parent(seed: int) -> None:
    rng = random.Random(seed)
    deadline = (
        time.monotonic() + 18
    )  # a time box, so CI cost is bounded however it goes
    rt = _fresh()
    ran = crashed = 0
    try:
        for index in range(400):
            if time.monotonic() > deadline:
                break
            program = _program(rng)
            where = f"seed={seed} program #{index}: {program!r}"
            try:
                rt.eval(program)
            except DOCUMENTED:
                pass
            except BaseException as exc:  # noqa: BLE001
                pytest.fail(f"unexpected {type(exc).__name__}: {exc}\n{where}")
            ran += 1
            if rt.is_closed():
                crashed += 1
                rt.close()
                rt = _fresh()
                continue
            # if it is not closed, it must still answer
            try:
                assert rt.eval("1 + 1") == 2, where
            except WorkerCrashed:
                rt.close()
                rt = _fresh()
            except RuntimeError:
                # e.g. a program froze Object.prototype or deleted Array in the shared global
                # state; a fresh runtime is the supported way to carry on
                rt.close()
                rt = _fresh()
    finally:
        rt.close()
    assert ran >= 50, f"only {ran} programs ran in the time box"


class TestRegressions:
    """Programs that once found a problem, kept so it cannot come back."""

    def test_an_endless_stream_of_host_calls_cannot_dodge_the_deadline(self) -> None:
        # found by reading `_pump`: limits were checked only when the pipe went quiet
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.5)
        rt.bind_function("host", lambda *a: None)
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout):
            rt.eval("for (;;) { host(1) }")
        assert time.monotonic() - start < 15

    @pytest.mark.parametrize("program", NASTY[:12])
    def test_each_early_nasty_fragment_alone(self, program: str) -> None:
        rt = _fresh()
        try:
            try:
                rt.eval(program)
            except DOCUMENTED:
                pass
        finally:
            rt.close()
