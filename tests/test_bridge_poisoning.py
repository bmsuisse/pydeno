"""A guest that tampers with JavaScript built-ins must not be able to abort the process.

The bridge that copies a guest's arguments before they reach a host function used to look up
`Date.prototype.valueOf`, `Array.prototype.map`, `Object.entries`, `ArrayBuffer.isView`... at call
time. A guest that replaced one of them could make the bridge hand a Symbol to the Rust converter,
which panics and aborts: a lost worker in `IsolatedRuntime`, a dead host process in a plain
`Runtime`. The bridge now captures the intrinsics before any guest code exists.

Plain `Runtime` cases run in a subprocess: if the fix ever regresses, the test fails instead of
taking pytest down with it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    RuntimeConfig,
    WorkerCrashed,
    undefined,
)

# Each snippet tampers with a built-in, then calls the bound `lookup` with a value that goes
# through the tampered path. Before the fix every one of these killed the process.
POISON = {
    "date-valueof-symbol": "Date.prototype.valueOf = () => Symbol('x'); lookup(new Date(0))",
    "bigint-tostring-symbol": "BigInt.prototype.toString = () => Symbol('x'); lookup(5n)",
    "isview-always-true": "ArrayBuffer.isView = () => true; lookup({a: Symbol('s')})",
    "array-map-symbol": "Array.prototype.map = () => Symbol(); lookup([1, 2, 3])",
    "object-entries-symbol": "Object.entries = () => [[Symbol(), 1]]; lookup({a: 1})",
    "array-iterator-symbol": (
        "Array.prototype[Symbol.iterator] = function* () { yield Symbol('i'); }; "
        "lookup({a: 1, b: [1]})"
    ),
    "set-foreach-symbol": "Set.prototype.forEach = () => { throw Symbol('s') }; lookup(new Set([1]))",
    "define-property-tamper": "Object.defineProperty = () => { throw new Error('gone') }; lookup({a: 1})",
    "getter-returns-symbol": "lookup({ get a() { return Symbol('g') } })",
}
IDS = list(POISON)


@pytest.mark.parametrize("name", IDS)
def test_a_tampered_builtin_cannot_kill_the_isolated_worker(name: str) -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
        rt.bind_function("lookup", lambda v: v)
        try:
            rt.eval(POISON[name])
        except WorkerCrashed:
            raise  # the one outcome that is not acceptable
        except Exception:
            pass  # any ordinary, catchable error is a fine answer
        assert rt.eval("1 + 1") == 2
        assert not rt.is_closed()


_PLAIN = textwrap.dedent(
    """
    import sys
    from pydeno import JavaScriptError, Runtime, RuntimeConfig
    rt = Runtime(RuntimeConfig(timeout=20.0))
    rt.bind_function("lookup", lambda v: v)
    try:
        rt.eval({snippet!r})
    except Exception:
        pass  # an ordinary catchable error is fine; the process is still here to say so
    assert rt.eval("1 + 1") == 2
    print("alive")
    """
)


@pytest.mark.parametrize("name", IDS)
def test_a_tampered_builtin_cannot_abort_the_host_process(name: str) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _PLAIN.format(snippet=POISON[name])],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0 and done.stdout.strip() == "alive", (
        done.returncode,
        done.stderr[-400:],
    )


def test_an_argument_with_millions_of_values_is_refused_before_it_is_copied() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=60.0)) as rt:
        rt.bind_function("lookup", lambda v: len(v))
        with pytest.raises(JavaScriptError, match="too large"):
            rt.eval(
                "const o = {}; for (let i = 0; i < 1500000; i++) o['k' + i] = i; lookup(o)"
            )
        assert rt.eval("1 + 1") == 2


def test_an_argument_nested_too_deeply_is_refused() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
        rt.bind_function("lookup", lambda v: 1)
        with pytest.raises(JavaScriptError, match="nested too deeply"):
            rt.eval("let a = []; for (let i = 0; i < 300; i++) a = [a]; lookup(a)")
        assert rt.eval("lookup([[[1]]])") == 1


def test_ordinary_arguments_still_cross_unchanged() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
        rt.bind_function("echo", lambda v: v)
        assert rt.eval("echo({a: [1, 2, {b: 'x'}], c: null, d: true})") == {
            "a": [1, 2, {"b": "x"}],
            "c": undefined,  # null on the op-argument path is JsUndefined, documented
            "d": True,
        }
        assert rt.eval("echo(new Set([1, 2]))") == {1, 2}
        assert rt.eval("echo(12345678901234567890n)") == 12345678901234567890
        assert rt.eval("echo(new Uint8Array([1, 2, 3]))") == b"\x01\x02\x03"


def test_the_bridge_is_strict_so_it_does_not_expose_its_own_arguments() -> None:
    # A sloppy-mode function exposes `fn.arguments`, which includes the host-op token.
    with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
        rt.bind_function("lookup", lambda v: v)
        out = rt.eval(
            "let seen; lookup({ get x() { try { seen = __pydenoCallSync.arguments } "
            "catch (e) { seen = 'blocked' } return 1 } }); String(seen)"
        )
        assert out in ("blocked", "null")


def test_a_sparse_array_is_refused_before_it_is_iterated() -> None:
    """`null`/`undefined` entries used to skip the node count, so a four-billion-entry sparse array
    looped for as long as the deadline allowed."""
    import time

    with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
        rt.bind_function("ping", lambda v: 1)
        start = time.monotonic()
        with pytest.raises(JavaScriptError, match="too large"):
            rt.eval("ping(new Array(2 ** 32 - 1))")
        assert time.monotonic() - start < 5
        with pytest.raises(JavaScriptError, match="too large"):
            rt.eval("ping(new Array(1e8))")
        assert rt.eval("1 + 1") == 2


@pytest.mark.parametrize(
    "name",
    [
        "__pydenoCallSync",
        "__pydenoCallAsync",
        "__host_op_sync__",
        "__host_op_async__",
        "__pydeno_bind_object",
    ],
)
def test_the_bridge_globals_cannot_be_replaced_or_deleted(name: str) -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
        rt.bind_function("echo", lambda v: v)
        before = rt.eval(f"typeof {name}")
        assert before == "function"
        # an assignment in sloppy mode is silently ignored; deleting returns false
        rt.eval(f"{name} = () => 'hijacked'; 0")
        assert rt.eval(f"delete globalThis.{name}") is False
        assert rt.eval(f"typeof {name}") == "function"
        assert rt.eval("echo(5)") == 5  # and the bound host function still works
        assert rt.eval(f"Object.keys(globalThis).includes('{name}')") is False
