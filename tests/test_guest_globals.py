"""Pin the exact guest-visible `globalThis` surface.

Before this fix, deno_core's bootstrap scaffolding survived the bridge's
`delete globalThis.Deno`: `globalThis.__bootstrap = {primordials, core,
internals}` (installed by deno_core's `00_infra.js`) still exposed
`__bootstrap.core.ops`, the *same* raw op table `Deno.core.ops` used to
expose. `globalThis.__bootstrap.core.ops.op_print('X', false)` wrote directly
to the **host process's** stdout/stderr -- a full escape from the sandbox's
declared "no ambient I/O" contract, with no timeout, no metering, and no
Python-side visibility.

This file is the boundary: it does not special-case `__bootstrap`, it lists
every own key `Reflect.ownKeys(globalThis)` is allowed to return, drawn from
ECMAScript intrinsics plus what peno itself installs. A future deno_core
bump that starts installing a new bootstrap global -- or a peno change that
adds a new bridge global without updating this list -- fails this test
immediately, rather than silently reopening an escape.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from peno import Runtime, RuntimeConfig, SnapshotBuilder

# ECMAScript/V8 intrinsics peno does not (and should not) hide.
_ECMASCRIPT_INTRINSICS = {
    "Object", "Function", "Array", "Number", "parseFloat", "parseInt",
    "Infinity", "NaN", "undefined", "Boolean", "String", "Symbol", "Date",
    "Promise", "RegExp", "Error", "AggregateError", "EvalError",
    "RangeError", "ReferenceError", "SyntaxError", "TypeError", "URIError",
    "globalThis", "JSON", "Math", "Intl", "ArrayBuffer", "Atomics",
    "Uint8Array", "Int8Array", "Uint16Array", "Int16Array", "Uint32Array",
    "Int32Array", "BigUint64Array", "BigInt64Array", "Uint8ClampedArray",
    "Float32Array", "Float64Array", "DataView", "Map", "BigInt", "Set",
    "Iterator", "WeakMap", "WeakSet", "Proxy", "Reflect",
    "FinalizationRegistry", "WeakRef", "decodeURI", "decodeURIComponent",
    "encodeURI", "encodeURIComponent", "escape", "unescape", "eval",
    "isFinite", "isNaN", "console", "Temporal", "SuppressedError",
    "DisposableStack", "AsyncDisposableStack", "Float16Array",
    "SharedArrayBuffer", "queueMicrotask", "WebAssembly",
}

# Globals peno's own bridge installs deliberately (src/runtime/ops.rs).
# `ReadableStream` is a conditional polyfill only installed when V8's build
# doesn't already provide a native one; on this V8 build it is peno's own
# polyfill, not a deno_core global.
_PENO_BRIDGE_GLOBALS = {
    "__penoCallSync",
    "__penoCallAsync",
    "__host_op_sync__",
    "__host_op_async__",
    "__peno_bind_object",
    "__peno_from_py_stream",
    "ReadableStream",
}

ALLOWED_GLOBALS = _ECMASCRIPT_INTRINSICS | _PENO_BRIDGE_GLOBALS

# Globals deno_core's own bootstrap is known to install ambiently and that
# must never survive the bridge's cleanup, regardless of whether this V8
# build happens to expose them today.
FORBIDDEN_GLOBALS = {"Deno", "__bootstrap", "__infra"}


def _own_keys(rt: Runtime) -> set[str]:
    keys = rt.eval("Reflect.ownKeys(globalThis).map(String)")
    return set(keys)


def test_guest_global_surface_matches_allowlist() -> None:
    with Runtime(RuntimeConfig()) as rt:
        keys = _own_keys(rt)
        unexpected = keys - ALLOWED_GLOBALS
        assert not unexpected, (
            f"unexpected guest-visible global(s): {sorted(unexpected)} -- "
            "if this is a legitimate new peno bridge global, add it to "
            "_PENO_BRIDGE_GLOBALS deliberately; if it came from deno_core, "
            "it must be deleted in the bridge bootstrap (src/runtime/ops.rs) "
            "before this test is widened"
        )
        for forbidden in FORBIDDEN_GLOBALS:
            assert forbidden not in keys, f"{forbidden!r} leaked onto globalThis"


def test_guest_global_surface_matches_allowlist_under_snapshot() -> None:
    # 01_core.js can reinstall `__bootstrap` for lazily-loaded core JS, and a
    # snapshot changes when/whether that lazy path runs -- so the pin must
    # hold when RuntimeConfig(snapshot=...) is used too, not just on a
    # from-scratch runtime.
    builder = SnapshotBuilder()
    builder.execute_script("init.js", "1")
    snapshot = builder.build()

    with Runtime(RuntimeConfig(snapshot=snapshot)) as rt:
        keys = _own_keys(rt)
        unexpected = keys - ALLOWED_GLOBALS
        assert not unexpected, (
            f"unexpected guest-visible global(s) under snapshot: "
            f"{sorted(unexpected)}"
        )
        for forbidden in FORBIDDEN_GLOBALS:
            assert forbidden not in keys, (
                f"{forbidden!r} leaked onto globalThis under snapshot"
            )


def test_bootstrap_op_print_escape_is_closed() -> None:
    """Regression test for the specific disclosed escape: before the fix,
    `__bootstrap.core.ops.op_print` wrote straight to the host's fd 1,
    bypassing peno entirely. Runs in a subprocess with fd 1 captured so a
    regression is an assertion failure, not contamination of the pytest
    session's own stdout.
    """
    body = """
        import sys
        from peno import Runtime, RuntimeConfig

        with Runtime(RuntimeConfig()) as rt:
            result = rt.eval(
                "(() => {"
                "  const b = globalThis.__bootstrap;"
                "  if (typeof b === 'undefined') return 'undefined';"
                "  try { b.core.ops.op_print('ESCAPE-MARKER', false); return 'wrote'; }"
                "  catch (e) { return 'threw: ' + e; }"
                "})()"
            )
            print("RESULT:" + str(result))
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ESCAPE-MARKER" not in completed.stdout, (
        "host stdout was written to directly by guest JS via "
        "__bootstrap.core.ops.op_print -- the escape is back"
    )
    assert "RESULT:undefined" in completed.stdout
