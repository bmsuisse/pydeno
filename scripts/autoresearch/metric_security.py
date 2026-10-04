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


# --- slice C (boundary) ---------------------------------------------------------------------------------------
# Host boundary and state (#75 slice C): tools and external functions, error redaction, journals and
# replay, `SessionPool` and its store, the `Pydeno` front door, the CLI and the text pydeno itself writes.
# Every probe runs in a fresh interpreter with a hard time cap (`_C_CAP`): a probe that hangs, crashes
# or raises counts as a violation, and no probe can leave threads or workers behind for the next one.
import asyncio as _c_asyncio  # noqa: E402
import os as _c_os  # noqa: E402
import subprocess as _c_subprocess  # noqa: E402

_C_CAP = 240.0
_C_KEY = b"slice-c-journal-key-0123456789"
_C_PROBES: dict[str, Probe] = {}
_C_BIDI = "\u202e\u2066"
# Invisible to a reader, not to a program or a language model: a Unicode tag character (the
# ASCII-smuggling range) and a word joiner.
_C_INVISIBLE = "\U000e0041\u2060"


def _c_probe(fn: Probe) -> Probe:
    _C_PROBES[fn.__name__] = fn

    def capped() -> bool:
        try:
            done = _c_subprocess.run(
                [
                    sys.executable,
                    _c_os.path.abspath(__file__),
                    "--slice-c-probe",
                    fn.__name__,
                ],
                timeout=_C_CAP,
                capture_output=True,
                text=True,
            )
        except _c_subprocess.TimeoutExpired:
            raise RuntimeError(f"timed out after {_C_CAP:g}s") from None
        if done.returncode not in (0, 1):
            last = (done.stderr.strip().splitlines() or ["no output"])[-1]
            raise RuntimeError(last[:160])
        return done.returncode == 1

    PROBES[f"c_{fn.__name__}"] = capped
    return fn


def _c_child(name: str) -> int:
    try:
        return 1 if _C_PROBES[name]() else 0
    except BaseException:  # noqa: BLE001 - reported to the parent as a probe error
        traceback.print_exc()
        return 3


class _CStop(BaseException):
    """What a host tool might raise that is not an `Exception` (SystemExit, a cancellation)."""


def _c_reloads(blob: bytes, tools, **kw) -> bool:  # type: ignore[no-untyped-def]
    from pydeno import AgentSandbox, JournalError, ReplayDivergence

    try:
        AgentSandbox.load(blob, _C_KEY, tools, sandbox="require", **kw).close()
    except (JournalError, ReplayDivergence):
        return False
    return True


# 1. tools and external functions ------------------------------------------------------------------------------
@_c_probe
def tool_base_exception_leaves_an_unloadable_journal() -> bool:
    """A tool raising a BaseException (SystemExit, a cancellation) must not leave a journal that
    `dump()` returns but `load()` refuses (an unrestorable session; `SessionPool` users are told to
    `drop` it, which also resets its tool budget)."""
    from pydeno import AgentSandbox

    def stop(x):  # type: ignore[no-untyped-def]
        raise _CStop("tool text")

    tools = {"stop": stop, "ok": lambda: 1}
    with AgentSandbox(tools, sandbox="require") as s:
        s.execute("return 1")
        s.execute("try { await stop(1) } catch (e) {} return await ok()")
        blob = s.dump(_C_KEY)
    return not _c_reloads(blob, tools)


@_c_probe
def front_external_base_exception_leaves_an_unloadable_state() -> bool:
    from pydeno import Pydeno, PydenoError

    def stop(x):  # type: ignore[no-untyped-def]
        raise _CStop("tool text")

    with Pydeno(min_processes=1) as pool:
        with pool.checkout() as s:
            s.feed_run("const kept = 41")
            try:
                s.feed_run(
                    "try { await stop(1) } catch (e) {} 1",
                    external_lookup={"stop": stop},
                )
            except PydenoError:
                pass
            state = s.dump()
        with pool.checkout() as s2:
            try:
                s2.load_session(state)
            except PydenoError:
                return True
            return s2.feed_run("kept + 1") != 42


def _c_storm(n: int = 200) -> str:
    return (
        f"const rs = await Promise.allSettled(Array.from({{length: {n}}}, (_, i) => t(i)));"
        "return rs.filter(r => r.status === 'rejected').length"
    )


def _c_slow(i):  # type: ignore[no-untyped-def]
    time.sleep(0.0005 * (i % 4))
    return i


@_c_probe
def tool_call_storm_makes_replay_diverge() -> bool:
    """Concurrent calls past `max_inflight_host_calls` were refused depending on how fast the host
    answered: nothing records that, so replay diverged (an unrestorable session, chosen by the guest)."""
    from pydeno import AgentSandbox

    tools = {"t": _c_slow}
    with AgentSandbox(tools, max_tool_calls=10_000, sandbox="require") as s:
        r = s.execute(_c_storm())
        if not r.ok or r.result != 0:
            return True
        blob = s.dump(_C_KEY)
    return not _c_reloads(blob, tools)


@_c_probe
def async_tool_call_storm_makes_replay_diverge() -> bool:
    from pydeno import AsyncAgentSandbox, JournalError, ReplayDivergence

    async def t(i):  # type: ignore[no-untyped-def]
        await _c_asyncio.sleep(0.0005 * (i % 4))
        return i

    async def go() -> bool:
        async with AsyncAgentSandbox(
            {"t": t}, max_tool_calls=10_000, sandbox="require"
        ) as s:
            r = await s.execute(_c_storm())
            if not r.ok or r.result != 0:
                return True
            blob = await s.dump(_C_KEY)
        try:
            restored = await AsyncAgentSandbox.load(
                blob, _C_KEY, {"t": t}, sandbox="require"
            )
        except (JournalError, ReplayDivergence):
            return True
        await restored.close()
        return False

    return _c_asyncio.run(go())


@_c_probe
def tool_budget_exceeded_with_refused_or_concurrent_calls() -> bool:
    from pydeno import AgentSandbox

    ran = []

    def t(i):  # type: ignore[no-untyped-def]
        ran.append(i)
        return i

    with AgentSandbox({"t": t}, max_tool_calls=3, sandbox="require") as s:
        s.execute(_c_storm(100))
        s.execute("try { await t(-1) } catch (e) {} return 1")
        return len(ran) > 3 or s.calls_made > 3


@_c_probe
def tool_name_shadows_a_session_global() -> bool:
    """A tool whose name is one of the session's own globals is silently unreachable (or breaks
    every run) instead of being refused when the session is made."""
    from pydeno import AgentSandbox

    for name in ("__pydeno_agent_settle", "globalThis", "__pydeno_external"):
        try:
            AgentSandbox({name: lambda: 1}, sandbox="require").close()
        except (ValueError, TypeError):
            continue
        return True
    return False


@_c_probe
def tool_error_text_reaches_the_guest_by_default() -> bool:
    from pydeno import AgentSandbox, Pydeno, ToolCall

    def leak(x):  # type: ignore[no-untyped-def]
        raise ValueError("SECRET-host-path /srv/app")

    def leak_base(x):  # type: ignore[no-untyped-def]
        raise _CStop("SECRET-base")

    seen = []
    with AgentSandbox({"leak_base": leak_base}, sandbox="require") as s:
        # A BaseException ends the run (and the worker); neither the guest nor the error says why.
        r = s.execute(
            "try { await leak_base(1) } catch (e) { return e.name + e.message }"
        )
        seen.append(repr(r.result) + repr(r.error))
    with AgentSandbox({"leak": leak}, sandbox="require") as s:
        r = s.execute("try { await leak(1) } catch (e) { return e.name + e.message }")
        seen.append(repr(r.result) + repr(r.error))
        step = s.start("try { await leak(1) } catch (e) { return e.message }")
        if isinstance(step, ToolCall):
            step = s.resume(step, error=PermissionError("SECRET-approver"))
        seen.append(repr(getattr(step, "value", None)))
    with Pydeno(min_processes=1) as pool, pool.checkout() as sess:
        try:
            seen.append(
                repr(
                    sess.feed_run(
                        "try { await leak(1) } catch (e) { e.message }",
                        external_lookup={"leak": leak},
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001
            seen.append(repr(exc))
    return any("SECRET" in text for text in seen)


@_c_probe
def foreign_or_used_tool_call_is_accepted() -> bool:
    from pydeno import AgentSandbox, ToolCall

    tools = {"t": lambda x: x}
    with (
        AgentSandbox(tools, sandbox="require") as a,
        AgentSandbox(tools, sandbox="require") as b,
    ):
        sa, sb = a.start("return await t(1)"), b.start("return await t(2)")
        attempts = [
            lambda: a.resume(sb, 9),
            lambda: a.resume(ToolCall("t", (1,), sa.call_id, 0), 9),
        ]
        for attempt in attempts:
            try:
                attempt()
                return True
            except RuntimeError:
                pass
        a.resume(sa, 1)
        try:
            a.resume(sa, 2)
            return True
        except RuntimeError:
            pass
        restored = type(b).load(b.dump(_C_KEY), _C_KEY, tools, sandbox="require")
        try:
            restored.resume(sb, 3)
            return True
        except RuntimeError:
            pass
        finally:
            restored.close()
    return False


@_c_probe
def catalog_tool_reachable_or_charged_before_discovery() -> bool:
    from pydeno import AgentSandbox, SchemaTool

    ran = []
    secret = SchemaTool(
        name="secret_tool",
        description="internal",
        input_schema={"type": "object"},
        callable=lambda args: ran.append(args) or "hit",
    )
    with AgentSandbox(
        {}, tools_catalog=[secret], max_tool_calls=5, sandbox="require"
    ) as s:
        s.execute(
            "for (const f of [() => tools.secret_tool({}), () => tools['secret_tool']({}),"
            " () => tools.constructor({}), () => tools.__proto__.x({})]) { try { await f() } catch {} }"
            "return 1"
        )
        return bool(ran) or s.calls_made > 0


@_c_probe
def front_invalid_answer_consumes_the_snapshot() -> bool:
    """An answer the session refuses (not an Exception) used up the snapshot, leaving the session
    paused forever: neither resumable nor feedable."""
    from pydeno import AsyncPydeno, Pydeno

    with Pydeno(min_processes=1) as pool, pool.checkout() as s:
        snap = s.feed_start("await f(1)", external_lookup={"f": lambda x: x})
        try:
            snap.resume({"exception": "not an exception"})
        except TypeError:
            pass
        try:
            snap.resume(value=1)
        except RuntimeError:
            return True

    async def go() -> bool:
        async with AsyncPydeno(min_processes=1) as pool:
            async with pool.checkout() as s:
                snap = await s.feed_start(
                    "await f(1)", external_lookup={"f": lambda x: x}
                )
                try:
                    await snap.resume(error="not an exception")  # type: ignore[arg-type]
                except TypeError:
                    pass
                try:
                    await snap.resume(value=1)
                except RuntimeError:
                    return True
        return False

    return _c_asyncio.run(go())


@_c_probe
def front_syntax_check_is_steered_by_the_guest() -> bool:
    """The front door's check of whether a failed feed compiled must not depend on built-ins
    the guest can replace, nor run outside the journal."""
    from pydeno import Pydeno, PydenoError, PydenoSyntaxError

    ran = []
    with Pydeno(min_processes=1) as pool:
        with pool.checkout() as s:
            s.feed_run(
                "const real = Object.getPrototypeOf;"
                "Object.getPrototypeOf = function (o) {"
                " globalThis.n = (globalThis.n || 0) + 1;"
                " if (globalThis.lie) throw new SyntaxError('lie'); return real(o) }"
            )
            try:
                s.feed_run("throw new SyntaxError('mine')")
            except PydenoError:
                pass
            s.feed_run("globalThis.lie = true")
            try:
                s.feed_run(
                    "await mark(1); throw new SyntaxError('after a side effect')",
                    external_lookup={"mark": lambda x: ran.append(x)},
                )
            except PydenoSyntaxError:
                if ran:
                    return True
            except PydenoError:
                pass
            s.feed_run("globalThis.lie = false")
            state = s.dump()
        with pool.checkout() as s2:
            try:
                s2.load_session(state)
            except PydenoError:
                return True
    return False


@_c_probe
def front_snapshot_for_an_undeclared_function() -> bool:
    """`feed_start` handed the host a snapshot for any name the guest passed to the hidden
    dispatcher (or a stub left from an earlier feed), not only for this feed's `external_lookup`: an
    approver dispatching on `function_name` could be asked to run a function the feed never had."""
    from pydeno import AsyncPydeno, Pydeno, PydenoSnapshot

    code = (
        "for (const f of [() => leftover(), () => __pydeno_external('drop_db', 1), () => read(1)])"
        " { try { await f() } catch (e) {} } 1"
    )
    with Pydeno(min_processes=1) as pool, pool.checkout() as s:
        s.feed_run("1", external_lookup={"leftover": lambda: 1})
        snap = s.feed_start(code, external_lookup={"read": lambda x: x})
        while isinstance(snap, PydenoSnapshot):
            if snap.function_name != "read":
                return True
            snap = snap.resume(value=None)

    async def go() -> bool:
        async with AsyncPydeno(min_processes=1) as pool:
            async with pool.checkout() as s:
                await s.feed_run("1", external_lookup={"leftover": lambda: 1})
                snap = await s.feed_start(code, external_lookup={"read": lambda x: x})
                while not hasattr(snap, "output"):
                    if snap.function_name != "read":
                        return True
                    snap = await snap.resume(value=None)
        return False

    return _c_asyncio.run(go())


# 2. journals and state ----------------------------------------------------------------------------------------
@_c_probe
def tampered_truncated_or_spliced_journal_loads() -> bool:
    from pydeno import AgentSandbox, JournalError

    tools = {"t": lambda x: x}
    with AgentSandbox(tools, sandbox="require") as s:
        s.run("const a = await t(1)")
        one = s.dump(_C_KEY, associated_data=b"tenant-a")
        s.run("const b = await t(2)")
        two = s.dump(_C_KEY, associated_data=b"tenant-a")
    head = len(b"pydeno-agent2\x00") + 32
    forged = [
        one[:-1],
        one + b" ",
        one[:head] + two[head:],
        one[:-3] + bytes([one[-3] ^ 1]) + one[-2:],
        one.replace(b'"t"', b'"u"'),
    ]
    for blob in forged:
        try:
            AgentSandbox.load(
                blob, _C_KEY, tools, associated_data=b"tenant-a", sandbox="require"
            ).close()
            return True
        except JournalError:
            pass
    for ad in (b"tenant-b", b""):
        try:
            AgentSandbox.load(
                one, _C_KEY, tools, associated_data=ad, sandbox="require"
            ).close()
            return True
        except JournalError:
            pass
    return False


@_c_probe
def replay_calls_the_real_tool_or_crash_changes_budget() -> bool:
    from pydeno import AgentSandbox

    ran = []

    def t(x):  # type: ignore[no-untyped-def]
        ran.append(x)
        return x

    tools = {"t": t}
    with AgentSandbox(tools, max_tool_calls=10, timeout=2, sandbox="require") as s:
        s.run("const a = await t(1)")
        s.execute("await t(2); await t(3); for (;;) {}")  # spends two calls, then dies
        blob = s.dump(_C_KEY)
    before = len(ran)
    restored = AgentSandbox.load(blob, _C_KEY, tools, sandbox="require")
    try:
        return (
            len(ran) != before
            or restored.calls_made < 3
            or restored.run("return a") != 1
        )
    finally:
        restored.close()


@_c_probe
def guest_reads_the_host_clock_or_entropy() -> bool:
    from pydeno import AgentSandbox

    with AgentSandbox({}, clock=1_000_000, random_seed=7, sandbox="require") as s:
        seen = s.run(
            "return [Date.now(), new Date().getTime(), typeof performance, typeof crypto,"
            " typeof Temporal === 'undefined' ? 1e9 * 1000 : Temporal.Now.instant().epochMilliseconds,"
            " Math.random()]"
        )
    with AgentSandbox({}, clock=1_000_000, random_seed=7, sandbox="require") as s:
        again = s.run("return Math.random()")
    return (
        seen[0] != 1_000_000_000
        or seen[1] != 1_000_000_000
        or seen[2] != "undefined"
        or seen[3] != "undefined"
        or seen[4] != 1_000_000_000
        or seen[5] != again
    )


@_c_probe
def guest_error_name_claims_a_host_failure() -> bool:
    from pydeno import AgentSandbox, classify_error

    with AgentSandbox({}, sandbox="require") as s:
        for name in (
            "WorkerCrashed",
            "RuntimeTimeout",
            "ResultTooLarge",
            "JournalError",
        ):
            code = f"const e = new Error('worker used 9 bytes, over max_memory=1; killed'); e.name = '{name}'; throw e"
            r = s.execute(code)
            if r.error_type != "Error":
                return True
            try:
                s.run(code)
            except Exception as exc:  # noqa: BLE001
                if classify_error(exc).kind != "js_error":
                    return True
    return False


async def _c_pool_turns(pool, owner: str, sid: str, code: str, turns: int) -> None:  # type: ignore[no-untyped-def]
    from pydeno import JournalError

    for _ in range(turns):
        try:
            async with pool.session(owner, sid) as sb:
                await sb.execute(code)
        except JournalError:
            pass


@_c_probe
def pool_oversized_journal_restore_budget_mismatch() -> bool:
    """After a journal outgrows `max_journal_bytes`, the restored session must keep the budget
    the session had already spent."""
    from pydeno import InMemoryJournalStore, SessionPool

    ran = []

    def big(i):  # type: ignore[no-untyped-def]
        ran.append(i)
        return "x" * 200_000

    async def go() -> bool:
        async with SessionPool(
            InMemoryJournalStore(),
            _C_KEY,
            {"big": big},
            max_tool_calls=4,
            max_journal_bytes=1 << 19,
            sandbox="require",
        ) as pool:
            code = "for (let i = 0; i < 10; i++) { try { await big(i) } catch (e) { break } } return 1"
            await _c_pool_turns(pool, "alice", "chat", code, 3)
        return len(ran) > 4

    return _c_asyncio.run(go())


@_c_probe
def pool_accepts_an_id_it_cannot_persist() -> bool:
    from pydeno import InMemoryJournalStore, SessionPool

    async def go() -> bool:
        async with SessionPool(
            InMemoryJournalStore(), _C_KEY, {}, sandbox="require"
        ) as pool:
            owner, sid = "ä" * 256, "\U0001f600" * 256
            try:
                sb = await pool.get(owner, sid)
            except ValueError:
                return False  # refused up front: fine
            await sb.run("globalThis.x = 1")
            try:
                await pool.release(owner, sid)
            except ValueError:
                return True
            return False

    return _c_asyncio.run(go())


@_c_probe
def pool_drop_loses_to_a_concurrent_restore() -> bool:
    """`drop()` awaited the store with the session absent from the map; a `get` in that window
    restored the old journal, and the dropped session (and its state) lived on."""
    from pydeno import InMemoryJournalStore, SessionPool

    class SlowStore(InMemoryJournalStore):
        gate: _c_asyncio.Event | None = None

        async def get(self, key):  # type: ignore[no-untyped-def]
            gate = self.gate
            if gate is not None and key.endswith(":counter"):
                self.gate = None
                await gate.wait()
            return await super().get(key)

    async def go() -> bool:
        store = SlowStore()
        async with SessionPool(
            store,
            _C_KEY,
            {},
            idle_timeout=0.01,
            eviction_interval=1000,
            sandbox="require",
        ) as pool:
            async with pool.session("o", "s") as sb:
                await sb.run("globalThis.secret = 'kept'; return 1")
            await _c_asyncio.sleep(0.05)
            await pool.evict_idle()
            gate = _c_asyncio.Event()
            store.gate = gate
            dropping = _c_asyncio.ensure_future(pool.drop("o", "s"))
            await _c_asyncio.sleep(0.05)  # drop() is waiting on the store
            getting = _c_asyncio.ensure_future(pool.get("o", "s"))
            await _c_asyncio.sleep(0.5)
            gate.set()
            await dropping
            await getting
            await pool.release("o", "s")
            async with pool.session("o", "s") as sb:
                state = await sb.run(
                    "return typeof secret === 'undefined' ? 'fresh' : secret"
                )
        return state != "fresh"

    return _c_asyncio.run(go())


@_c_probe
def pool_rollback_or_moved_journal_loads() -> bool:
    from pydeno import InMemoryJournalStore, JournalError, SessionPool

    async def go() -> bool:
        store = InMemoryJournalStore()
        async with SessionPool(store, _C_KEY, {}, sandbox="require") as pool:
            async with pool.session("alice", "s") as sb:
                await sb.run("globalThis.v = 1")
            old = await store.get("pydeno:session:alice:s:journal")
            async with pool.session("alice", "s") as sb:
                await sb.run("globalThis.v = 2")
            await pool.drop("alice", "s")
            await pool.drop("bob", "s")
        for owner, value in (("alice", old), ("bob", old)):
            async with SessionPool(store, _C_KEY, {}, sandbox="require") as pool:
                await store.set(f"pydeno:session:{owner}:s:journal", value, ttl=None)
                try:
                    await pool.get(owner, "s")
                    return True
                except JournalError:
                    pass
        return False

    return _c_asyncio.run(go())


@_c_probe
def front_dump_cannot_be_bound_to_a_tenant() -> bool:
    """`Pydeno` dumps had no associated data: a host could not stop one tenant's (or an older) dump
    from loading into another session of the same pool."""
    from pydeno import Pydeno, PydenoError

    with Pydeno(min_processes=1) as pool:
        with pool.checkout() as s:
            s.feed_run("const owner = 'a'")
            try:
                state = s.dump(associated_data=b"tenant-a")
            except TypeError:
                return True
        with pool.checkout() as s2:
            for kw in ({"associated_data": b"tenant-b"}, {}):
                try:
                    s2.load_session(state, **kw)
                    return True
                except PydenoError:
                    pass
            s2.load_session(state, associated_data=b"tenant-a")
            return s2.feed_run("owner") != "a"


_C_LOOP = (
    "let n = 0; for (let i = 0; i < %d; i++) { try { await %s(i); n++ } catch { break } }"
    " return n"
)


@_c_probe
def pool_overlapping_get_and_oversized_release_budget_mismatch() -> bool:
    """A `get` overlapping the release of an oversized journal must see the same spent budget."""
    from pydeno import InMemoryJournalStore, JournalTooLarge, SessionPool

    ran: list[int] = []
    tools = {
        "big": lambda i: ran.append(i) or "x" * 5000,
        "small": lambda i: ran.append(i) or i,
    }

    async def go() -> bool:
        async with SessionPool(
            InMemoryJournalStore(),
            _C_KEY,
            tools,
            max_tool_calls=6,
            max_journal_bytes=8000,
            sandbox="require",
        ) as pool:
            async with pool.session("a", "s") as sb:
                await sb.execute("await small(0)")
            sb = await pool.get("a", "s")
            await sb.execute(_C_LOOP % (9, "big"))
            releasing = _c_asyncio.ensure_future(pool.release("a", "s"))
            await _c_asyncio.sleep(0)
            racer = await pool.get("a", "s")
            await racer.execute(_C_LOOP % (9, "small"))
            try:
                await releasing
            except JournalTooLarge:
                pass
            await pool.release("a", "s")
        return len(ran) > 6

    return _c_asyncio.run(go())


@_c_probe
def pool_two_instances_on_one_store_budget_mismatch() -> bool:
    """Two pools over one store (sequential use, as behind a load balancer) must not each hand
    out the session's whole budget."""
    from pydeno import InMemoryJournalStore, SessionPool

    ran: list[int] = []
    tools = {"small": lambda i: ran.append(i) or i}

    async def go() -> bool:
        store = InMemoryJournalStore()
        kw = dict(max_tool_calls=3, sandbox="require")
        async with (
            SessionPool(store, _C_KEY, tools, **kw) as pa,
            SessionPool(store, _C_KEY, tools, **kw) as pb,
        ):
            async with pa.session("o", "s") as sb:
                await sb.run("return 0")
            for pool in (pb, pa, pb, pa):
                try:
                    async with pool.session("o", "s") as sb:
                        await sb.execute(_C_LOOP % (9, "small"))
                except Exception:  # noqa: BLE001, S110 - a refusal is fine
                    pass
        return len(ran) > 3

    return _c_asyncio.run(go())


@_c_probe
def pool_failed_release_budget_mismatch() -> bool:
    """A release that fails (here: a run still in progress) must not leave a live session that
    eviction later forgets with its spending."""
    from pydeno import InMemoryJournalStore, SessionPool

    ran: list[int] = []

    async def slow(i):  # type: ignore[no-untyped-def]
        ran.append(i)
        await _c_asyncio.sleep(0.05)
        return i

    async def go() -> bool:
        async with SessionPool(
            InMemoryJournalStore(),
            _C_KEY,
            {"slow": slow},
            max_tool_calls=4,
            idle_timeout=0.01,
            eviction_interval=1000,
            sandbox="require",
        ) as pool:
            sb = await pool.get("o", "s")
            task = _c_asyncio.ensure_future(sb.execute(_C_LOOP % (4, "slow")))
            await _c_asyncio.sleep(0.02)
            try:
                await pool.release("o", "s")
            except Exception:  # noqa: BLE001, S110
                pass
            await _c_asyncio.gather(task, return_exceptions=True)
            await _c_asyncio.sleep(0.05)
            await pool.evict_idle()
            async with pool.session("o", "s") as again:
                await again.execute(_C_LOOP % (4, "slow"))
        return len(ran) > 4

    return _c_asyncio.run(go())


@_c_probe
def pool_queued_get_ignores_its_timeout() -> bool:
    """A get that queues in the instant a lease is handed to an earlier waiter must still time
    out (`acquire_timeout`) instead of waiting forever."""
    from pydeno import InMemoryJournalStore, SessionPool

    async def go() -> bool:
        pool = SessionPool(
            InMemoryJournalStore(), _C_KEY, {}, acquire_timeout=0.5, sandbox="require"
        )
        try:
            await pool.get("o", "s")
            b = _c_asyncio.ensure_future(pool.get("o", "s"))
            await _c_asyncio.sleep(0.1)

            async def again() -> object:
                await pool.release("o", "s")
                return await pool.get("o", "s")

            a = _c_asyncio.ensure_future(again())
            await _c_asyncio.wait_for(b, 5)
            done, _ = await _c_asyncio.wait({a}, timeout=4)
            if not done:
                a.cancel()
                return True
            return False
        finally:
            await pool.close()

    return _c_asyncio.run(go())


# 4. text pydeno writes for the host ---------------------------------------------------------------------------
@_c_probe
def cli_prints_guest_terminal_escapes() -> bool:
    for args in (
        ["-c", "'\\x1b]0;owned\\x07' + '\\u202e' + 'ok'"],
        ["-c", "throw new Error('\\x1b[2J\\u202e')"],
    ):
        done = _c_subprocess.run(
            [sys.executable, "-m", "pydeno", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        text = done.stdout + done.stderr
        if "\x1b" in text or any(c in text for c in _C_BIDI):
            return True
    return False


@_c_probe
def guest_bidi_controls_reach_host_messages() -> bool:
    import contextlib
    import io

    from pydeno import AgentSandbox, Pydeno

    with AgentSandbox({}, sandbox="require") as s:
        try:
            s.run(f"throw new Error('a{_C_BIDI}b')")
        except Exception as exc:  # noqa: BLE001
            if any(c in str(exc) for c in _C_BIDI):
                return True
        r = s.execute(f"throw new Error('a{_C_BIDI}b')")
        if any(c in (r.error or "") for c in _C_BIDI):
            return True
    out = io.StringIO()
    with (
        Pydeno(min_processes=1) as pool,
        pool.checkout() as sess,
        contextlib.redirect_stdout(out),
    ):
        sess.feed_run(f"console.log('a{_C_BIDI}\\x1b[31mb')")
    return any(c in out.getvalue() for c in _C_BIDI + "\x1b")


@_c_probe
def captured_console_carries_terminal_escapes() -> bool:
    """`ExecutionResult.stdout`/`stderr` (and `Done`/`Failed`'s) are text pydeno collected for the
    host to show or log; they passed escape sequences and bidi overrides through untouched."""
    import asyncio

    from pydeno import AgentSandbox, AsyncAgentSandbox, IsolatedRuntime

    code = (
        f"console.log('a\\x1b]0;owned\\x07{_C_BIDI}{_C_INVISIBLE}b');"
        " console.error('\\x1b[2J'); return 1"
    )
    texts = []
    with AgentSandbox({}, sandbox="require") as s:
        r = s.execute(code)
        texts += [r.stdout, r.stderr]
    with IsolatedRuntime(sandbox="require", capture_console=True) as rt:
        r = rt.execute(code.replace("return 1", "1"))
        texts += [r.stdout, r.stderr]

    async def go() -> list[str]:
        async with AsyncAgentSandbox({}, sandbox="require") as s:
            r = await s.execute(code)
            return [r.stdout, r.stderr]

    texts += asyncio.run(go())
    return any(c in text for text in texts for c in _C_BIDI + _C_INVISIBLE + "\x1b\x07")


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--slice-c-probe":
    sys.exit(_c_child(sys.argv[2]))
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
        _subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=cap, check=False
        )
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
    return (
        took is None or took > 2 * 3 + 6
    )  # twice the deadline, plus start-up and generous slack


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
