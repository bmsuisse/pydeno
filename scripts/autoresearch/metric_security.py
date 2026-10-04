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
