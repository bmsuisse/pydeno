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


# --- slice B (resources) ---------------------------------------------------------------------------------------
# Limits must be enforceable values and must hold end to end. Probes that run guest code under a limit run in a
# subprocess with a hard wall-clock cap, so a limit that fails cannot hang the battery.
import math as _math  # noqa: E402
import subprocess as _subprocess  # noqa: E402


def _accepted(make) -> bool:  # type: ignore[no-untyped-def]
    """True if a limit value that disables the limit was accepted (the attack succeeded)."""
    try:
        obj = make()
    except (ValueError, TypeError):
        return False
    close = getattr(obj, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001, S110
            pass
    return True


def _capped_run(code: str, cap: float) -> float | None:
    """Run `code` in a fresh interpreter; its wall time, or None if it hit `cap` (killed)."""
    t = time.monotonic()
    try:
        _subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=cap, check=False)
    except _subprocess.TimeoutExpired:
        return None
    return time.monotonic() - t


@probe
def non_finite_limits_are_refused() -> bool:
    from pydeno import AgentSandbox, AsyncIsolatedRuntime
    from pydeno._isolated import _session_options

    nan, inf = _math.nan, _math.inf
    makers = [
        lambda: _session_options(request_timeout=nan),
        lambda: _session_options(max_host_wait=inf),
        lambda: _session_options(timeout_grace=nan),
        lambda: _session_options(write_stall_timeout=nan),
        lambda: _session_options(max_host_calls=nan),
        lambda: _session_options(max_inflight_host_calls=nan),
        lambda: AsyncIsolatedRuntime(max_memory=nan, prewarm=False),
        lambda: AsyncIsolatedRuntime(request_timeout=nan, prewarm=False),
        lambda: AgentSandbox({}, timeout=nan, sandbox="require"),
    ]
    return any(_accepted(m) for m in makers)


@probe
def console_flood_does_not_stretch_the_hard_deadline() -> bool:
    # A 3 s hard deadline with a console handler that takes 5 ms per call. Console output is not a tool
    # call: it may pause the deadline only within an allowance of one deadline per command, so a flood
    # ends by about twice the deadline (it used to run until max_host_wait, 600 s by default).
    code = (
        "import time\n"
        "from pydeno import IsolatedRuntime, RuntimeConfig\n"
        "cfg = RuntimeConfig(on_console=lambda level, args: time.sleep(0.005))\n"
        "with IsolatedRuntime(cfg, request_timeout=3, sandbox='require') as rt:\n"
        "    try:\n"
        "        rt.eval(\"for (;;) console.log('x')\")\n"
        "    except Exception:\n"
        "        pass\n"
    )
    took = _capped_run(code, 40)
    return took is None or took > 2 * 3 + 6  # twice the deadline, plus start-up and generous slack


@probe
def default_printer_volume_is_capped_per_feed() -> bool:
    # Pydeno's default print_callback writes the guest's console to the host's stdout (often a log
    # pipeline). A feed may write at most about 1 MiB there; it used to be unbounded (~150 MB in 2 s).
    code = (
        "from pydeno import Pydeno\n"
        "with Pydeno(min_processes=1) as pool, pool.checkout(limits={'max_feed_duration_secs': 2}) as s:\n"
        "    try:\n"
        "        s.feed_run(\"const t = 'x'.repeat(1 << 16); for (;;) console.log(t)\")\n"
        "    except Exception:\n"
        "        pass\n"
    )
    try:
        out = _subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=40, check=False
        ).stdout
    except _subprocess.TimeoutExpired:
        return True
    return len(out) > 2 * 1024 * 1024


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
