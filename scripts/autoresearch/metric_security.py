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

import json
import re
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


_RESIZABLE_BUFFER_GROWTH = """
(() => {
  const out = [];
  for (const make of [
    () => new ArrayBuffer(%(big)d, {maxByteLength: %(big)d}),                 // born over the cap
    () => { const b = new ArrayBuffer(8, {maxByteLength: %(big)d}); b.resize(%(big)d); return b; },
    () => new ArrayBuffer(8).transfer(8).transfer(%(big)d),               // grown through transfer
  ]) {
    try { out.push(make().byteLength); } catch (e) { out.push(e.name); }
  }
  return out;
})()
"""


@probe
def resizable_array_buffers_respect_max_buffer_bytes() -> bool:
    """`max_buffer_bytes` promises a catchable RangeError for live ArrayBuffer bytes past the cap.
    A resizable buffer (or one grown through `transfer`) must not be a way around it: V8 does not
    route those backing stores through the embedder's allocator."""
    cap = 64 * 2**20
    code = _RESIZABLE_BUFFER_GROWTH % {"big": 4 * cap}
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT, max_buffer_bytes=cap),
        sandbox="require",
        request_timeout=10,
    ) as rt:
        if rt.eval(code) != ["RangeError"] * 3 or rt.eval("1 + 1") != 2:
            return True
    with Runtime(RuntimeConfig(max_buffer_bytes=cap)) as rt:
        return rt.eval(code) != ["RangeError"] * 3


@probe
def growable_shared_array_buffers_respect_max_buffer_bytes() -> bool:
    """The same hole for `SharedArrayBuffer(n, {maxByteLength}).grow()`. In-process only: the
    worker strips `SharedArrayBuffer`."""
    cap = 64 * 2**20
    with Runtime(RuntimeConfig(max_buffer_bytes=cap)) as rt:
        out = rt.eval(
            """
            (() => {
              const out = [];
              for (const make of [
                () => new SharedArrayBuffer(%(big)d, {maxByteLength: %(big)d}),
                () => { const b = new SharedArrayBuffer(8, {maxByteLength: %(big)d}); b.grow(%(big)d); return b; },
              ]) {
                try { out.push(make().byteLength); } catch (e) { out.push(e.name); }
              }
              return out;
            })()
            """
            % {"big": 4 * cap}
        )
        return out != ["RangeError"] * 2


_COLLECTED_RESIZABLE_THEN_FIXED = """
(() => {
  const out = [];
  try { { let a = new ArrayBuffer(200 * 2**20, {maxByteLength: 200 * 2**20}); a = null; }
        out.push(new Uint8Array(100 * 2**20).length); } catch (e) { out.push(e.name); }
  function build() { const b = new ArrayBuffer(0, {maxByteLength: 150 * 2**20}); b.resize(150 * 2**20); return b.byteLength; }
  try { build(); build(); out.push(new Float64Array(16 * 2**20).length); } catch (e) { out.push(e.name); }
  return out;
})()
"""


@probe
def collected_resizable_buffers_do_not_starve_fixed_allocations() -> bool:
    """Charging resizable buffers must not make legitimate code fail: a resizable buffer the guest
    dropped must give its bytes back before a fixed-length allocation (which V8 routes through the
    allocator, not the bridge) is refused. Worked before the charge existed; a regression probe."""
    expect = [100 * 2**20, 16 * 2**20]
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT, max_buffer_bytes=255 * 2**20),
        sandbox="require",
        request_timeout=20,
        max_memory=2**30,
    ) as rt:
        if rt.eval(_COLLECTED_RESIZABLE_THEN_FIXED) != expect:
            return True
    with Runtime(RuntimeConfig(max_buffer_bytes=255 * 2**20)) as rt:
        return rt.eval(_COLLECTED_RESIZABLE_THEN_FIXED) != expect


_TINY_RESIZABLE_LOOP_PROBE = """
import json, resource, sys
from pydeno import Runtime, RuntimeConfig
with Runtime(RuntimeConfig(max_buffer_bytes=255 * 2**20)) as rt:
    rt.eval("for (let i = 0; i < 100000; i++) new ArrayBuffer(1, {maxByteLength: 1});")  # warm
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rt.eval("for (let i = 0; i < 3000000; i++) new ArrayBuffer(1, {maxByteLength: 1});")
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss is bytes on macOS, KiB on Linux
print(json.dumps((after - before) * scale))
"""


@probe
def churning_tiny_resizable_buffers_does_not_grow_the_host() -> bool:
    """Accounting for resizable buffers must not itself be a memory leak: three million tiny
    resizable buffers that never approach the cap must leave the host within a small constant,
    whatever bookkeeping the charge keeps per buffer. In a subprocess under a hard cap."""
    import subprocess

    try:
        out = subprocess.run(
            [sys.executable, "-c", _TINY_RESIZABLE_LOOP_PROBE],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return True
    if not out.stdout.strip():
        return True
    growth = int(float(out.stdout.strip().splitlines()[-1]))
    if growth > 64 * 2**20:
        print(f"    host grew by {growth / 2**20:.0f} MiB", file=sys.stderr)
        return True
    return False


_TOUCHED_TINY_RESIZABLES = """
(() => {
  const keep = [];
  for (let i = 0; i < %(n)d; i++) {
    try {
      const b = new ArrayBuffer(1, {maxByteLength: 1});
      new Uint8Array(b)[0] = 1;  // commit the page
      keep.push(b);
    } catch (e) { return [e.name, i]; }
  }
  return ["none", keep.length];
})()
"""
_TINY_RESIZABLES_IN_PROCESS = """
import json, sys
from pydeno import Runtime, RuntimeConfig
with Runtime(RuntimeConfig(max_buffer_bytes=32 * 2**20)) as rt:
    print(json.dumps(rt.eval(sys.argv[1])))
"""


@probe
def tiny_resizable_buffers_are_charged_by_committed_pages() -> bool:
    """V8 commits a resizable backing store in whole pages, so a one-byte resizable buffer costs a
    page of RSS. Charging `byteLength` lets 40,000 of them (40 KB charged) commit hundreds of MB
    under a 32 MiB cap. The cap must refuse them long before: at 4 KiB pages a 32 MiB cap holds
    about 8,000, at 16 KiB about 2,000."""
    import subprocess

    code = _TOUCHED_TINY_RESIZABLES % {"n": 40_000}
    with IsolatedRuntime(
        RuntimeConfig(timeout=10.0, max_buffer_bytes=32 * 2**20),
        sandbox="require",
        request_timeout=30,
        max_memory=512 * 2**20,
    ) as rt:
        try:
            name, count = rt.eval(code)
        except Exception:  # noqa: BLE001
            return True  # the memory kill, not the cap
        if name != "RangeError" or count > 10_000:
            return True
    try:
        out = subprocess.run(
            [sys.executable, "-c", _TINY_RESIZABLES_IN_PROCESS, code],
            capture_output=True,
            text=True,
            timeout=60,
        )
        name, count = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return True
    return name != "RangeError" or count > 10_000


_REFUSED_ALLOCATION_THEN_WORK = (
    "try { new ArrayBuffer(%(over)d) } catch (e) { globalThis.__refused = e.name }; 0"
)


@probe
def a_refused_allocation_does_not_leave_the_runtime_terminated() -> bool:
    """With `max_heap_size` set, a refused ArrayBuffer allocation must stay what it is to the
    guest (a RangeError) and leave the runtime usable: the next command must run."""
    cap = 32 * 2**20
    code = _REFUSED_ALLOCATION_THEN_WORK % {"over": cap + 1}
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT, max_buffer_bytes=cap, max_heap_size=256 * 2**20),
        sandbox="require",
        request_timeout=10,
    ) as rt:
        try:
            rt.eval(code)
            if rt.eval("[globalThis.__refused, 1 + 1]") != ["RangeError", 2]:
                return True
        except Exception:  # noqa: BLE001
            return True
    with Runtime(RuntimeConfig(max_buffer_bytes=cap, max_heap_size=256 * 2**20)) as rt:
        try:
            rt.eval(code)
            if rt.eval("[globalThis.__refused, 1 + 1]") != ["RangeError", 2]:
                return True
        except Exception:  # noqa: BLE001
            return True
    # Genuine heap exhaustion must still end in a termination, every time (a terminated runtime
    # stays terminated: that is `max_heap_size`'s documented effect, so each try is a new one).
    for _ in range(3):
        with Runtime(
            RuntimeConfig(max_buffer_bytes=cap, max_heap_size=256 * 2**20)
        ) as rt:
            try:
                rt.eval("const a = []; for (;;) a.push(new Array(100000).fill(1))")
                return True
            except Exception as exc:  # noqa: BLE001
                if "Terminated" not in type(exc).__name__ and "Heap" not in str(exc):
                    return True
    return False


@probe
def huge_result_is_refused_without_killing_the_parent() -> bool:
    with iso() as rt:
        try:
            out = rt.eval("'x'.repeat(2 ** 28)")
            return len(out) > 2**27  # returned an absurd value instead of refusing
        except Exception:  # noqa: BLE001
            return rt.eval("1 + 1") != 2 if not rt.is_closed() else False


# --- the deadline must survive a poisoned Error ---------------------------------------------------------------
# After `timeout=` terminates the guest, the host converts the termination into an error
# (deno_core's `JsError::from_v8_exception`), which reads properties of a guest-controlled error
# object: `constructor`, `name`, `message`, `cause`, `stack`, the registered symbol
# `errorAdditionalPropertyKeys`, and runs `Error.prepareStackTrace`. A getter planted on
# `Error.prototype` (or on the thrown instance) then runs *after* the termination was consumed,
# unbounded by the deadline. The same reads happen for a thrown error and a rejected promise.
_ERROR_POISONS = {
    "ctor": "Object.defineProperty(Error.prototype, 'constructor', {get() { for (;;) {} }, configurable: true});",
    "cause": "Object.defineProperty(Error.prototype, 'cause', {get() { for (;;) {} }, configurable: true});",
    "name": "Object.defineProperty(Error.prototype, 'name', {get() { for (;;) {} }, configurable: true});",
    "message": "Object.defineProperty(Error.prototype, 'message', {get() { for (;;) {} }, configurable: true});",
    "stack": "Object.defineProperty(Error.prototype, 'stack', {get() { for (;;) {} }, configurable: true});",
    "ctor_name": "Object.defineProperty(Error, 'name', {get() { for (;;) {} }, configurable: true});",
    "object_ctor": "Object.defineProperty(Object.prototype, 'constructor', {get() { for (;;) {} }, configurable: true});",
    "prepare_stack_trace": "Error.prepareStackTrace = () => { for (;;) {} };",
    "additional_keys": "Object.defineProperty(Error.prototype, Symbol.for('errorAdditionalPropertyKeys'), {get() { for (;;) {} }, configurable: true});",
    "instance_message": "globalThis.__e = new Error('m'); Object.defineProperty(__e, 'message', {get() { for (;;) {} }});",
    "instance_name": "globalThis.__e = new Error('m'); Object.defineProperty(__e, 'name', {get() { for (;;) {} }});",
}
_ERROR_TRIGGERS = {
    "spin": "for (;;) {}",
    "throw": "throw (globalThis.__e || new Error('x'))",
    "reject": "Promise.reject(globalThis.__e || new Error('r')); for (;;) {}",
}
_DEADLINE = 1.0
_IN_PROCESS_DEADLINE_PROBE = """
import asyncio, json, sys, time
from pydeno import Runtime, RuntimeConfig
deadline, poison, trigger, mode = float(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
t = time.monotonic()
try:
    with Runtime(RuntimeConfig(timeout=deadline)) as rt:
        if mode == "sync":
            rt.eval(poison + " " + trigger)
        else:
            asyncio.run(rt.eval_async(poison + " (async () => { " + trigger + " })()", timeout=deadline))
except Exception:
    pass
print(json.dumps(time.monotonic() - t))
"""


def _poison_cases():  # type: ignore[no-untyped-def]
    for pname, poison in _ERROR_POISONS.items():
        for tname, trigger in _ERROR_TRIGGERS.items():
            if pname.startswith("instance_") and tname == "spin":
                continue  # an instance getter needs the instance to reach the host
            yield f"{pname}/{tname}", poison, trigger


@probe
def poisoned_error_prototype_cannot_outlive_the_deadline_isolated() -> bool:
    """Every variant must end (timeout or error) within 4x the deadline, through the
    deadline, not through the hard kill of the worker."""
    late = []
    for name, poison, trigger in _poison_cases():
        with IsolatedRuntime(
            RuntimeConfig(timeout=_DEADLINE), sandbox="require", request_timeout=8
        ) as rt:
            t = time.monotonic()
            try:
                rt.eval(poison + " " + trigger)
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic() - t > 4 * _DEADLINE or rt.is_closed():
                late.append(name)
    if late:
        print(f"    late: {late}", file=sys.stderr)
    return bool(late)


@probe
def poisoned_error_prototype_cannot_hang_the_in_process_runtime() -> bool:
    """Same, on `Runtime`, each case in its own process under a hard cap so a hang cannot take
    the battery with it (`eval` and `eval_async`)."""
    import subprocess

    late = []
    for name, poison, trigger in _poison_cases():
        for mode in ("sync", "async"):
            try:
                out = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        _IN_PROCESS_DEADLINE_PROBE,
                        str(_DEADLINE),
                        poison,
                        trigger,
                        mode,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=6 * _DEADLINE,
                )
                elapsed = (
                    float(out.stdout.strip().splitlines()[-1])
                    if out.stdout.strip()
                    else 1e9
                )
            except subprocess.TimeoutExpired:
                elapsed = 1e9
            if elapsed > 4 * _DEADLINE:
                late.append(f"{name}/{mode}")
    if late:
        print(f"    late: {late}", file=sys.stderr)
    return bool(late)


# --- a refused bind runs no guest code ------------------------------------------------------------------------
_BIND_GETTERS = [
    "Object.defineProperty(TypeError.prototype, 'constructor', {get() { globalThis.__hits++; return TypeError }, configurable: true})",
    "Object.defineProperty(Error.prototype, 'constructor', {get() { globalThis.__hits++; return Error }, configurable: true})",
    "Object.defineProperty(Object.prototype, 'constructor', {get() { globalThis.__hits++; return Object }, configurable: true})",
    "Object.defineProperty(TypeError, 'name', {get() { globalThis.__hits++; return 'TypeError' }, configurable: true})",
    "Object.defineProperty(Error, 'name', {get() { globalThis.__hits++; return 'Error' }, configurable: true})",
    "Object.defineProperty(Object, 'name', {get() { globalThis.__hits++; return 'Object' }, configurable: true})",
]
_REFUSED_BIND_LOOP_PROBE = """
import sys
from pydeno import Runtime, RuntimeConfig
with Runtime(RuntimeConfig(timeout=1.0)) as rt:
    rt.eval("globalThis.tools = new Proxy({}, {}); " + sys.argv[1] + "; 0")
    try:
        rt.bind_object("tools", {"f": lambda: 1})
    except Exception:
        pass
print("done")
"""


@probe
def a_refused_bind_runs_no_guest_getter() -> bool:
    """The error a refused `bind_object` hands the host is converted by the host with no deadline
    around it: a getter on `constructor` or `name` of the error's class chain must not fire, and a
    looping one must not hang the host."""
    import subprocess

    fired = []
    for setup in _BIND_GETTERS:
        with Runtime(RuntimeConfig(timeout=2.0)) as rt:
            rt.eval(
                "globalThis.__hits = 0; globalThis.tools = new Proxy({}, {}); "
                + setup
                + "; 0"
            )
            try:
                rt.bind_object("tools", {"f": lambda: 1})
                fired.append("bound onto a Proxy")
            except Exception:  # noqa: BLE001
                pass
            if rt.eval("globalThis.__hits"):
                fired.append(setup[22:60])
    for setup in _BIND_GETTERS:
        looping = setup.replace("globalThis.__hits++; return", "for (;;) {}; return")
        try:
            subprocess.run(
                [sys.executable, "-c", _REFUSED_BIND_LOOP_PROBE, looping],
                capture_output=True,
                timeout=8,
            )
        except subprocess.TimeoutExpired:
            fired.append("hang: " + setup[22:60])
    if fired:
        print(f"    fired: {fired}", file=sys.stderr)
    return bool(fired)


# --- the console channel ---------------------------------------------------------------------------------------
@probe
def console_echo_of_a_large_line_does_not_kill_the_worker() -> bool:
    """With `enable_console=True` the worker echoes console output to its own stdout/stderr, which
    the parent captures in a size-limited file. One large line must not end the session."""
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT, enable_console=True),
        sandbox="require",
        request_timeout=10,
    ) as rt:
        for level in ("log", "error"):
            try:
                rt.eval(f"console.{level}('x'.repeat(2 ** 20)); 0")
            except Exception:  # noqa: BLE001
                return True  # a console call must never fail the guest, let alone the worker
            if rt.is_closed():
                return True
        return rt.eval("1 + 1") != 2


@probe
def captured_console_output_carries_no_terminal_escapes() -> bool:
    """`execute()` returns console output as text a host will print or log. Like the error text
    (`_clean`), it must not be able to carry escape or control sequences into that terminal."""
    with iso(capture_console=True) as rt:
        result = rt.execute(
            "console.log('\\x1b[2J\\x1b]0;x\\x07', 'a\\rb', 'tab\\tok', 'bidi\\u202e\\u2066\\u200f\\u061c', "
            "'invisible\\u200b\\u200c\\u200d\\u2028\\u2029\\ufeff\\u180e\\ufff9\\u00ad\\u034f\\ufe0f\\u2060', "
            "'tags\\u{e0001}\\u{e0041}\\u{e007f}\\u{e0100}', 'visible \\u00e9\\u4e2d\\u{1f600} ok'); "
            "console.error('\\x9b1m'); 1"
        )
    text = result.stdout + result.stderr
    # Unicode bidirectional controls reorder what a terminal shows (a spoofed line), like ESC;
    # invisible format characters and TAG characters carry text a reader never sees (a payload for
    # whatever model reads the output).
    hostile = (
        r"[\x00-\x08\x0b-\x1f\x7f-\x9f­͏؜᠎​-‏ -‮⁠-⁩"
        r"︀-️﻿￹-￻\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
    )
    return (
        bool(re.search(hostile, text))
        or "tab\tok" not in text
        or "visible é中\U0001f600 ok" not in text
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
