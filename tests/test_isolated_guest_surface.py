"""What the isolated worker's engine offers guest code (issue #75, slice A).

Removing a feature means switching it off in V8, not deleting a global: a deleted constructor is
still reachable through syntax or through the objects it produced.

deno_core's platform start-up switches several features on *after* the worker's own flags
(`Temporal`, `Float16Array`, explicit resource management, source-phase and deferred imports,
the native `queueMicrotask`), and V8 keeps the last value. A caller's `v8_flags=["--no-harmony-
temporal"]` was therefore silently undone while `IsolatedRuntime.v8_flags` listed it as applied.
Such a flag is now refused at start-up instead.
"""

from __future__ import annotations

import pytest

from pydeno import IsolatedRuntime, RuntimeConfig


@pytest.fixture(scope="module")
def rt():  # type: ignore[no-untyped-def]
    with IsolatedRuntime(RuntimeConfig(timeout=5)) as runtime:
        yield runtime


@pytest.mark.parametrize(
    "expr",
    [
        "typeof ShadowRealm",
        "typeof SharedStructType",
        "typeof SharedArray",
        "typeof WebAssembly",
        "typeof SharedArrayBuffer",
        "typeof Atomics",
        "typeof WeakRef",
        "typeof FinalizationRegistry",
    ],
)
def test_experimental_jit_only_and_stripped_features_stay_off(rt, expr: str) -> None:  # type: ignore[no-untyped-def]
    assert rt.eval(expr) == "undefined"


@pytest.mark.parametrize(
    "flag",
    [
        "--no-harmony-temporal",
        "--no-js-float16array",
        "--no-js-explicit-resource-management",
        "--noenable-queue-microtask",
    ],
)
def test_a_restriction_the_engine_would_undo_is_refused_not_reported_as_applied(
    flag: str,
) -> None:
    # A ValueError from the constructor, before any worker is started (not a crashed worker).
    with pytest.raises(ValueError, match="cannot take effect"):
        IsolatedRuntime(RuntimeConfig(timeout=5), v8_flags=[flag], prewarm=False)


def test_a_restriction_v8_honours_still_applies() -> None:
    with IsolatedRuntime(
        RuntimeConfig(timeout=5), v8_flags=["--no-js-regexp-modifiers"]
    ) as runtime:
        assert "--no-js-regexp-modifiers" in runtime.v8_flags
        with pytest.raises(Exception, match="SyntaxError"):
            runtime.eval("new RegExp('(?i:a)')")


# Everything the isolation guide says `--no-js-shipping` removes, and what it says stays.
_NO_SHIPPING_GONE = {
    "Temporal": "typeof Temporal",
    "Float16Array": "typeof Float16Array",
    "DisposableStack": "typeof DisposableStack",
    "AsyncDisposableStack": "typeof AsyncDisposableStack",
    "SuppressedError": "typeof SuppressedError",
    "Promise.try": "typeof Promise.try",
    "RegExp.escape": "typeof RegExp.escape",
    "Math.sumPrecise": "typeof Math.sumPrecise",
    "Error.isError": "typeof Error.isError",
    "Uint8Array.fromBase64": "typeof Uint8Array.fromBase64",
    "Uint8Array.prototype.toBase64": "typeof Uint8Array.prototype.toBase64",
}
_NO_SHIPPING_SYNTAX_GONE = ["{ using x = null; }", "new RegExp('(?i:a)')"]
_NO_SHIPPING_KEPT = {
    "iterator helpers": "typeof Iterator.prototype.map",
    "Set methods": "typeof Set.prototype.union",
    "Object.groupBy": "typeof Object.groupBy",
    "findLast": "typeof [].findLast",
}


def test_no_js_shipping_removes_exactly_what_the_guide_says() -> None:
    with IsolatedRuntime(
        RuntimeConfig(timeout=5), v8_flags=["--no-js-shipping"]
    ) as runtime:
        for name, expr in _NO_SHIPPING_GONE.items():
            assert runtime.eval(expr) == "undefined", name
        for source in _NO_SHIPPING_SYNTAX_GONE:
            with pytest.raises(Exception, match="SyntaxError"):
                runtime.eval(source)
        for name, expr in _NO_SHIPPING_KEPT.items():
            assert runtime.eval(expr) == "function", name
    with IsolatedRuntime(
        RuntimeConfig(timeout=5)
    ) as runtime:  # and all present by default
        for name, expr in _NO_SHIPPING_GONE.items():
            assert runtime.eval(expr) != "undefined", name
