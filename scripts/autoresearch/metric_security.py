"""One number for the autoresearch security loops (see docs/contributing/autoresearch.md).

    python scripts/autoresearch/metric_security.py

Runs a battery of hostile-guest probes against `IsolatedRuntime(sandbox="require")` (and the bridge
probes against the in-process `Runtime` too). Each probe returns True when the ATTACK SUCCEEDED, i.e.
the sandbox failed. Prints `METRIC: <number of violations>` on stdout (lower is better, 0 is the goal)
and the failing probes on stderr. An unexpected exception inside a probe counts as a violation: a
probe that cannot prove the sandbox held has not shown that it held.

The battery grows: when review or red-teaming finds a new class of attack, add a probe here first.
"""

from __future__ import annotations

import sys
import time
import traceback
from collections.abc import Callable

from pydeno import IsolatedRuntime, Runtime, RuntimeConfig

TIMEOUT = 3.0
Probe = Callable[[], bool]
PROBES: dict[str, Probe] = {}


def probe(fn: Probe) -> Probe:
    PROBES[fn.__name__] = fn
    return fn


def iso(**kw):  # type: ignore[no-untyped-def]
    return IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT), sandbox="require", request_timeout=10, **kw
    )


# --- the protocol channel and the guest's view of the host ------------------------------------------------
@probe
def console_noise_does_not_corrupt_replies() -> bool:
    with iso() as rt:
        rt.eval("console.log('hello'); 1 + 1")
        return rt.eval("1 + 2") != 3


@probe
def guest_sees_no_host_runtime_objects() -> bool:
    with iso() as rt:
        names = rt.eval(
            "['Deno','process','require','module','Buffer','fetch','XMLHttpRequest','WebSocket',"
            "'importScripts','Worker','SharedArrayBuffer','Atomics','WebAssembly']"
            ".filter(n => typeof globalThis[n] !== 'undefined')"
        )
        return bool(names)


@probe
def guest_cannot_forge_a_reply_frame() -> bool:
    with iso() as rt:
        try:
            rt.eval(
                'Deno.stdout.writeSync(new TextEncoder().encode(\'{"t":"reply","id":1,"v":"FORGED"}\\n\'))'
            )
        except Exception:  # noqa: BLE001
            return rt.eval("1 + 1") != 2
        return True


@probe
def guest_cannot_poison_the_serializer() -> bool:
    with iso() as rt:
        rt.eval('JSON.stringify = () => \'{"id":2,"result":"POISONED"}\'; 0')
        return rt.eval("1 + 1") != 2


@probe
def dynamic_import_and_module_loading_are_denied() -> bool:
    with iso() as rt:
        for expr in (
            "import('node:fs').then(() => 'loaded')",
            "import('file:///etc/passwd').then(() => 'loaded')",
        ):
            try:
                out = rt.eval(expr)
            except Exception:  # noqa: BLE001
                continue
            if out == "loaded" or (hasattr(out, "__await__")):
                return True
        return False


# --- limits ---------------------------------------------------------------------------------------------------
@probe
def infinite_loop_ends_at_the_deadline() -> bool:
    with iso() as rt:
        t = time.monotonic()
        try:
            rt.eval("for (;;) {}")
        except Exception:  # noqa: BLE001
            return time.monotonic() - t > TIMEOUT * 4
        return True


@probe
def memory_bomb_is_killed_and_the_next_sandbox_works() -> bool:
    t = time.monotonic()
    with IsolatedRuntime(
        RuntimeConfig(timeout=20),
        sandbox="require",
        max_memory=300 * 2**20,
        request_timeout=30,
    ) as rt:
        try:
            rt.eval("const a = []; for (;;) a.push(new Array(10000).fill(1))")
            return True
        except Exception:  # noqa: BLE001
            pass
    if time.monotonic() - t > 25:
        return True
    with iso() as rt:
        return rt.eval("1 + 1") != 2


@probe
def huge_result_is_refused_without_killing_the_parent() -> bool:
    with iso() as rt:
        try:
            out = rt.eval("'x'.repeat(2 ** 28)")
            return len(out) > 2**27  # returned an absurd value instead of refusing
        except Exception:  # noqa: BLE001
            return rt.eval("1 + 1") != 2 if not rt.is_closed() else False


# --- strict_eval: no code generation from strings, however the guest reaches a compiler ------------------
# Run after the guest has tampered with the constructor chain (replaced `Function`, `eval` and
# `Function.prototype.constructor`, subclassed `Function`), so a compiler reached through any alias
# still has to refuse.
_STRICT_TAMPER = (
    "globalThis.__F = Function; globalThis.__real = {e: eval, ind: (0, eval), self: globalThis};"
    "globalThis.eval = function (s) { return 'shadow' };"
    "globalThis.Function = function () { return () => 'shadow' };"
    "Object.defineProperty(__F.prototype, 'constructor', {value: __F, writable: true})"
)
_STRICT_ATTACKS = (
    "new __F('return 1')()",
    "__F('return 1')()",
    "(function () {}).constructor('return 1')()",
    "(() => {}).constructor('return 1')()",
    "typeof Object.getPrototypeOf(async function () {}).constructor('return 1')",
    "typeof Object.getPrototypeOf(function* () {}).constructor('yield 1')",
    "typeof Object.getPrototypeOf(async function* () {}).constructor('yield 1')",
    "Reflect.construct(__F, ['return 1'])()",
    "Reflect.apply(__F, null, ['return 1'])()",
    "[].constructor.constructor('return 1')()",
    "({}).constructor.constructor('return 1')()",
    "class X extends __F {}; new X('return 1')()",
    "__F.prototype.call.call(__F, null, 'return 1')()",
    "Object.getOwnPropertyDescriptor(Object.getPrototypeOf(() => {}), 'constructor').value('return 1')()",
    # indirect eval through aliases taken before the guest shadowed `eval`
    "__real.e('1 + 1')",
    "(0, __real.ind)('1 + 1')",
    "__real.self.__real.e.call(null, '1 + 1')",
    "[__real.e][0]('1 + 1')",
)


@probe
def strict_eval_refuses_every_string_compiler() -> bool:
    with iso(strict_eval=True) as rt:
        rt.eval(_STRICT_TAMPER + "; 0")
        for code in _STRICT_ATTACKS:
            try:
                rt.eval(code)
            except Exception as exc:  # noqa: BLE001
                if "Code generation from strings disallowed" not in str(exc):
                    return True  # failed for another reason: not proof that strict mode held
                continue
            return True  # compiled and ran a string
        return False


@probe
def strict_eval_is_frozen_with_the_hardening_flags() -> bool:
    with iso(strict_eval=True) as rt:
        flags = rt.v8_flags
        return not (
            rt.strict_eval
            and flags[-1] == "--disallow-code-generation-from-strings"
            and "--freeze-flags-after-init" in flags
            and "--jitless" in flags
        )


# --- the host bridge: a guest must not interfere with a later host bind ----------------------------------
def _bind_probe(setup: str, kind: str, runtime) -> bool:  # type: ignore[no-untyped-def]
    """True if guest setup made a host bind silently do nothing (or run guest code) yet return normally."""
    with runtime() as rt:
        rt.eval(setup + "; 0")
        try:
            if kind == "object":
                rt.bind_object("tools", {"f": lambda: 1})
                visible = rt.eval(
                    "typeof tools !== 'undefined' && typeof tools.f === 'function'"
                )
            else:
                rt.bind_function("f", lambda: 1)
                visible = rt.eval("typeof f === 'function'")
            hits = rt.eval(
                "typeof globalThis.__hits === 'number' ? globalThis.__hits : 0"
            )
        except Exception:  # noqa: BLE001
            return False  # refused loudly: the sandbox held
        return (not visible) or hits > 0


_BIND_ATTACKS = {
    "proxy_namespace": (
        "globalThis.tools = new Proxy({}, {defineProperty: () => true})",
        "object",
    ),
    "getter_namespace": (
        "Object.defineProperty(globalThis, 'tools', {get() { return {} }, configurable: true})",
        "object",
    ),
    "iterator_hook": (
        "globalThis.__hits = 0; const o = Array.prototype[Symbol.iterator];"
        "Array.prototype[Symbol.iterator] = function () { globalThis.__hits++; return o.call(this) }",
        "object",
    ),
    "readonly_global": (
        "Object.defineProperty(globalThis, 'f', {value: 1, writable: false, configurable: false})",
        "function",
    ),
    "setter_global": (
        "globalThis.__hits = 0; Object.defineProperty(globalThis, 'f',"
        "{set(v) { globalThis.__hits++ }, get() { return 1 }, configurable: true})",
        "function",
    ),
    "intrinsic_namespace": ("globalThis.tools = Object.prototype", "object"),
}

for _name, (_setup, _kind) in _BIND_ATTACKS.items():
    for _rt_name, _factory in (("isolated", iso), ("inprocess", Runtime)):

        def _make(setup=_setup, kind=_kind, factory=_factory) -> Probe:  # type: ignore[no-untyped-def]
            return lambda: _bind_probe(setup, kind, factory)

        PROBES[f"bind_{_name}_{_rt_name}"] = _make()


# --- the platform the sandbox claims ---------------------------------------------------------------------------
@probe
def os_sandbox_is_complete_on_this_host() -> bool:
    from pydeno import sandbox_status

    return not sandbox_status().complete


# --- slice A (engine) ---------------------------------------------------------------------------------------
# Issue #75, slice A: the guest-visible engine surface and the values that cross the boundary. Each probe
# returns True when the guest got its way (a limit outrun, a value silently wrong, a restriction undone).


def _elapsed(fn: Callable[[], object]) -> tuple[float, BaseException | None]:
    started = time.monotonic()
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        return time.monotonic() - started, exc
    return time.monotonic() - started, None


# What a guest in the worker may see: ECMAScript built-ins this V8 ships, minus what the worker strips
# (SharedArrayBuffer, Atomics, WeakRef, FinalizationRegistry) and what --jitless removes (WebAssembly),
# plus the bridge. Anything new here is surface somebody has to justify.
_SLICE_A_GLOBALS = {
    *"AggregateError Array ArrayBuffer AsyncDisposableStack BigInt BigInt64Array BigUint64Array Boolean "
    "DataView Date DisposableStack Error EvalError Float16Array Float32Array Float64Array Function Infinity "
    "Int16Array Int32Array Int8Array Intl Iterator JSON Map Math NaN Number Object Promise Proxy RangeError "
    "ReadableStream ReferenceError Reflect RegExp Set String SuppressedError Symbol SyntaxError Temporal "
    "TypeError URIError Uint16Array Uint32Array Uint8Array Uint8ClampedArray WeakMap WeakSet console "
    "decodeURI decodeURIComponent encodeURI encodeURIComponent escape eval globalThis isFinite isNaN "
    "parseFloat parseInt queueMicrotask undefined unescape".split(),
    "__host_op_async__",
    "__host_op_sync__",
    "__pydenoCallAsync",
    "__pydenoCallSync",
    "__pydeno_bind_function",
    "__pydeno_bind_object",
    "__pydeno_from_py_stream",
}


@probe
def slice_a_worker_global_surface_grew() -> bool:
    with iso() as rt:
        names = set(rt.eval("Reflect.ownKeys(globalThis).map(String)"))
        hidden = rt.eval(
            "['ShadowRealm','SharedStructType','SharedArray','WebAssembly','SharedArrayBuffer','Atomics',"
            "'WeakRef','FinalizationRegistry'].filter(n => typeof globalThis[n] !== 'undefined')"
        )
        return bool(names - _SLICE_A_GLOBALS) or bool(hidden)


@probe
def slice_a_requested_engine_restriction_is_silently_undone() -> bool:
    # deno_core's start-up re-enables Temporal (and others) after the worker's flags; a caller asking
    # for it off must be told, not shown a flag list that claims it applied.
    # Only the specific refusal counts as holding: any other failure has not shown it.
    try:
        with IsolatedRuntime(
            RuntimeConfig(timeout=TIMEOUT),
            sandbox="require",
            request_timeout=10,
            v8_flags=["--no-harmony-temporal"],
        ) as rt:
            return rt.eval("typeof Temporal") != "undefined"
    except ValueError as exc:
        return "cannot take effect" not in str(exc)


@probe
def slice_a_typed_array_result_outruns_the_deadline() -> bool:
    # Non-Uint8 typed arrays listed every virtual index key before the size budget was charged.
    with iso() as rt:
        took, exc = _elapsed(lambda: rt.eval("new Float64Array(2 ** 24)"))
        if exc is None or took > TIMEOUT:
            return True
        return rt.is_closed() or rt.eval("1 + 1") != 2


@probe
def slice_a_boxed_string_result_outruns_the_deadline_inprocess() -> bool:
    with Runtime(RuntimeConfig(timeout=TIMEOUT)) as rt:
        took, exc = _elapsed(lambda: rt.eval("new String('x'.repeat(2 ** 24))"))
        return exc is None or took > TIMEOUT * 1.5


@probe
def slice_a_boxed_string_argument_outruns_the_deadline() -> bool:
    with iso() as rt:
        rt.bind_function("f", lambda *a: 1)
        took, exc = _elapsed(lambda: rt.eval("f(new String('x'.repeat(2 ** 24)))"))
        return exc is None or took > TIMEOUT


@probe
def slice_a_number_outside_int64_comes_back_wrong() -> bool:
    with iso() as rt:
        return rt.eval("[2 ** 63, -(2 ** 63), 2 ** 64]") != [2.0**63, -(2**63), 2.0**64]


# A Proxy around the value used to skip the up-front charge on every path; as a host-call argument in
# plain `Runtime` it ran V8 out of memory, which aborts the process. The in-process cases therefore run
# in a child process with a hard timeout, so a regression shows up as a violation, not a dead battery.
_SLICE_A_PROXIED = (
    "new Proxy(new Int8Array(2 ** 24), {})",
    "new Proxy(new String('x'.repeat(2 ** 24)), {})",
    "new Proxy(new Proxy(new Float64Array(2 ** 24), {}), {})",
)

_SLICE_A_CHILD = r"""
import asyncio, sys
from pydeno import Runtime, RuntimeConfig

mode, expr, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
config = {"timeout": %r}
if budget:
    config["max_serialization_bytes"] = budget
with Runtime(RuntimeConfig(**config)) as rt:
    rt.bind_function("f", lambda *a: 1)
    try:
        if mode == "eval":
            rt.eval(expr)
        elif mode == "async":

            async def run():
                return await rt.eval_async(expr, timeout=%r)

            asyncio.run(run())
        else:

            async def read():
                stream = await rt.eval_async(
                    "(async () => new ReadableStream({start(c) { c.enqueue(" + expr + "); c.close() }}))()"
                )
                return [chunk async for chunk in stream]

            asyncio.run(read())
        print("ACCEPTED")
    except Exception:
        print("REFUSED")
""" % (TIMEOUT, TIMEOUT)


def _slice_a_child_fails_open(mode: str, expr: str, budget: int = 0) -> bool:
    """True if the in-process conversion died, hung, ran past the deadline, or accepted the value."""
    import subprocess

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SLICE_A_CHILD, mode, expr, str(budget)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT * 10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True
    took = time.monotonic() - started
    return proc.returncode != 0 or "REFUSED" not in proc.stdout or took > TIMEOUT * 3


@probe
def slice_a_proxied_indexed_value_outruns_the_deadline_isolated() -> bool:
    for expr in _SLICE_A_PROXIED:
        for source in (expr, f"f({expr})"):
            with iso() as rt:
                rt.bind_function("f", lambda *a: 1)
                took, exc = _elapsed(lambda s=source: rt.eval(s))
                if exc is None or took > TIMEOUT:
                    return True
                if rt.is_closed() or rt.eval("1 + 1") != 2:
                    return True
    return False


@probe
def slice_a_proxied_indexed_value_fails_open_inprocess() -> bool:
    for expr in _SLICE_A_PROXIED:
        for mode, source in (
            ("eval", expr),
            ("eval", f"f({expr})"),
            ("async", f"Promise.resolve({expr})"),
            ("stream", expr),
        ):
            if _slice_a_child_fails_open(mode, source):
                return True
    return False


@probe
def slice_a_raised_budget_brings_back_the_unbounded_listing() -> bool:
    # The element cap must hold whatever `max_serialization_bytes` is.
    return _slice_a_child_fails_open(
        "eval", "new Float64Array(2 ** 24)", budget=2**31
    ) or _slice_a_child_fails_open("eval", f"f({_SLICE_A_PROXIED[0]})", budget=2**31)


_SLICE_A_UNCONVERTIBLE = (
    "({get x() { throw new Error('getter') }})",
    "new Proxy({}, {ownKeys() { throw new Error('trap') }})",
    "(() => { const r = Proxy.revocable({}, {}); r.revoke(); return r.proxy })()",
    "(() => { const a = {}; a.self = a; return a })()",
    "(() => { let o = 1; for (let i = 0; i < 100000; i++) o = [o]; return o })()",
    "Symbol('s')",
    "new Map([[1, 2]])",
    "new Error('x')",
    "new Date(NaN)",
    "2n ** 20000n",
    "({then(resolve) { resolve(1) }})",
)


@probe
def slice_a_unconvertible_result_crosses_silently() -> bool:
    for factory in (iso, Runtime):
        with factory() as rt:
            for expr in _SLICE_A_UNCONVERTIBLE:
                try:
                    out = rt.eval(expr)
                except Exception:  # noqa: BLE001
                    continue
                # In-process these have a faithful Python form: a function-valued `then` is a
                # callable handle, and a big BigInt is a Python int (only the worker's wire refuses it).
                if factory is Runtime and (
                    (expr.startswith("({then") and isinstance(out, dict))
                    or (expr.startswith("2n") and out == 2**20000)
                ):
                    continue
                return True
    return False


@probe
def slice_a_looping_getter_or_trap_in_a_result_outlives_the_deadline() -> bool:
    for expr in (
        "({get x() { for (;;) {} }})",
        "new Proxy({a: 1}, {get() { for (;;) {} }})",
        "new Proxy({}, {ownKeys() { for (;;) {} }})",
    ):
        with iso() as rt:
            took, exc = _elapsed(lambda e=expr: rt.eval(e))
            if exc is None or took > TIMEOUT * 2:
                return True
    return False


@probe
def slice_a_prototype_pollution_changes_what_the_host_receives() -> bool:
    got: list[object] = []
    with iso() as rt:
        rt.bind_function("f", lambda *a: got.append(a))
        rt.eval(
            "Object.defineProperty(Object.prototype, '__pydeno_type', {get() { return 'Date' }});"
            "Object.defineProperty(Array.prototype, 1, {set(v) {}, get() { return 'X' }});"
            "Object.prototype.toJSON = () => 'POISON';"
            "Symbol.prototype.toString = () => 'POISON'; 0"
        )
        rt.eval("f({a: 1}, [1, 2, 3], new Set([1]), new Date(0)); 0")
    if not got:
        return True
    args = got[0]
    return args[0] != {"a": 1} or args[1] != [1, 2, 3] or args[2] != {1}


@probe
def slice_a_native_builtin_outlives_the_hard_deadline() -> bool:
    # Sparse-array natives ignore V8 termination (in-process they cannot be bounded at all, see the
    # strict xfails in the parity security tests); the worker's hard kill must still end them.
    sparse = "const a = []; a.length = 2 ** 32 - 1; a[0] = 1; "
    for body in ("a.sort()", "a.join()", "a.lastIndexOf(2)"):
        with IsolatedRuntime(
            RuntimeConfig(timeout=1), sandbox="require", request_timeout=4
        ) as rt:
            took, exc = _elapsed(lambda b=body: rt.eval(sparse + b))
            if exc is None or took > 8:
                return True
    with iso() as rt:
        return rt.eval("1 + 1") != 2


@probe
def slice_a_heavy_builtin_outlives_the_deadline() -> bool:
    for expr in (
        "/^(a*)*\\1$/.test('a'.repeat(40) + 'b')",  # backreference: no linear-time fallback
        "(7n ** (2n ** 24n)).toString().length",
        "[...new Intl.Segmenter('en', {granularity: 'word'}).segment('a b '.repeat(2 ** 22))].length",
        "for (;;) new Intl.DateTimeFormat('en', {timeZone: 'UTC'})",
        "(function f() { return f() })()",
    ):
        with iso() as rt:
            took, exc = _elapsed(lambda e=expr: rt.eval(e))
            if exc is None or took > TIMEOUT * 2:
                return True
    return False


@probe
def slice_a_async_storm_outlives_the_deadline() -> bool:
    import asyncio

    async def run() -> bool:
        for expr in (
            "(() => { const f = () => queueMicrotask(f); f(); return new Promise(() => {}) })()",
            "(async () => { const ps = []; for (let i = 0; i < 5e6; i++) ps.push(Promise.resolve(i));"
            " return (await Promise.all(ps)).length })()",
            "(async () => ({then(r) { for (;;) {} }}))()",
            "(async function f() { await null; return f() })()",
        ):
            with iso() as rt:
                started = time.monotonic()
                try:
                    await rt.eval_async(expr, timeout=TIMEOUT)
                    return True
                except Exception:  # noqa: BLE001
                    pass
                if time.monotonic() - started > TIMEOUT * 2:
                    return True
                if rt.is_closed() or rt.eval("1 + 1") != 2:
                    return True
        return False

    return asyncio.run(run())


def main() -> None:
    violations = []
    for name, fn in PROBES.items():
        try:
            if fn():
                violations.append(name)
        except Exception:  # noqa: BLE001
            violations.append(
                f"{name} (probe error: {traceback.format_exc().splitlines()[-1][:100]})"
            )
    print(f"probes={len(PROBES)} violations={len(violations)}", file=sys.stderr)
    for v in violations:
        print(f"  VIOLATION {v}", file=sys.stderr)
    print(f"METRIC: {len(violations)}")


if __name__ == "__main__":
    main()
