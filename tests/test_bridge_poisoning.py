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

import re
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
        "__pydeno_bind_function",
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


# ---------------------------------------------------------------------------------------------
# A guest that prepares the ground *before* a later host bind.
#
# A long-lived runtime runs guest code, and the host binds more tools afterwards (a second
# `ToolBridge.attach`, a late `bind_function`). The bind step used to install onto whatever
# `globalThis[name]` already was, by plain assignment and `for...of`, so a guest could plant a
# Proxy, an accessor or a replaced `Array.prototype[Symbol.iterator]` and make that bind silently
# inert -- while the host still got (and exposed) a token for a binding that was never installed.
# The bind now refuses a namespace or global it cannot install onto, and runs no guest code.
# ---------------------------------------------------------------------------------------------

_BIND_REFUSED = r"Cannot bind '(tools|lookup)'"


@pytest.fixture(params=["runtime", "isolated"])
def make_rt(request: pytest.FixtureRequest):
    from pydeno import Runtime

    opened: list = []

    def factory():
        cls = Runtime if request.param == "runtime" else IsolatedRuntime
        rt = cls(RuntimeConfig(timeout=20.0))
        opened.append(rt)
        return rt

    yield factory
    for rt in opened:
        rt.close()


# Each of these used to leave `tools` without the host's members while `bind_object` returned
# the tokens (the frozen object already failed loudly; it is pinned so it stays that way). A
# namespace the bridge cannot verify as a plain, extensible object is now refused.
REFUSED_NAMESPACES = {
    "proxy-swallows-define": "globalThis.tools = new Proxy({}, {defineProperty: () => true})",
    "accessor-returns-fresh-object": (
        "Object.defineProperty(globalThis, 'tools', {get() { return {} }, configurable: true})"
    ),
    "frozen-object": "globalThis.tools = Object.freeze({})",
    "class-instance": "globalThis.tools = new (class Tools {})()",
    "function": "globalThis.tools = function () {}",
}


@pytest.mark.parametrize("name", list(REFUSED_NAMESPACES))
def test_bind_object_refuses_a_namespace_it_cannot_install_onto(
    make_rt, name: str
) -> None:
    rt = make_rt()
    rt.eval(REFUSED_NAMESPACES[name] + "; 0")
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_object("tools", {"f": lambda: 1})
    assert rt.eval("1 + 1") == 2


@pytest.mark.parametrize("name", list(REFUSED_NAMESPACES))
def test_tool_bridge_attach_fails_closed_and_keeps_no_token(make_rt, name: str) -> None:
    from pydeno import ToolBridge

    rt = make_rt()
    rt.eval(REFUSED_NAMESPACES[name] + "; 0")
    bridge = ToolBridge({"f": lambda: 1})
    with pytest.raises(Exception, match=_BIND_REFUSED):
        bridge.attach(rt)
    assert bridge.detach(rt) == 0  # nothing was recorded, so nothing to revoke


def test_an_inherited_namespace_is_shadowed_not_installed_onto(make_rt) -> None:
    rt = make_rt()
    rt.eval("Object.prototype.tools = new Proxy({}, {defineProperty: () => true}); 0")
    rt.bind_object("tools", {"f": lambda: 1})
    assert rt.eval("tools.f()") == 1
    assert rt.eval("Object.hasOwn(globalThis, 'tools')") is True


def test_a_replaced_array_iterator_does_not_run_during_bind(make_rt) -> None:
    rt = make_rt()
    # The hook would see `this`, the host's assignment list (tokens included), and by yielding
    # nothing it left the namespace empty.
    rt.eval(
        "globalThis.hits = 0; globalThis.seen = null;"
        "Array.prototype[Symbol.iterator] = function* () { hits++; seen = this; }; 0"
    )
    rt.bind_object("tools", {"f": lambda: 1, "g": lambda x: x})
    assert rt.eval("tools.f()") == 1
    assert rt.eval("hits") == 0
    assert rt.eval("seen") is None
    # Calling a bound function does not spread its arguments through the guest's iterator.
    assert rt.eval("tools.g(5)") == 5


def test_an_array_index_setter_does_not_intercept_bind(make_rt) -> None:
    rt = make_rt()
    rt.eval(
        "for (const i of ['0', '1']) Object.defineProperty(Array.prototype, i, "
        "{get() { return 'guest' }, set(v) {}, configurable: true}); 0"
    )
    rt.bind_object("tools", {"f": lambda: 1, "g": lambda: 2})
    assert rt.eval("[tools.f(), tools.g()]") == [1, 2]


def test_a_plain_namespace_is_still_extended_and_rebinding_still_works(make_rt) -> None:
    rt = make_rt()
    rt.eval("globalThis.tools = {own: 1}; 0")
    rt.bind_object("tools", {"f": lambda: 1})
    rt.bind_object("tools", {"g": lambda: 2})
    assert rt.eval("[tools.own, tools.f(), tools.g()]") == [1, 1, 2]
    rt.eval("globalThis.bare = Object.create(null); 0")
    rt.bind_object("bare", {"h": lambda: 3})
    assert rt.eval("bare.h()") == 3


# The single-function path assigned `globalThis.name = ...` in sloppy mode: a read-only global
# swallowed the assignment and an accessor (own or inherited) intercepted it.
REFUSED_GLOBALS = {
    "read-only": (
        "Object.defineProperty(globalThis, 'lookup', "
        "{value: () => 'guest', writable: false, configurable: false})"
    ),
    "accessor": (
        "Object.defineProperty(globalThis, 'lookup', "
        "{get() { return () => 'guest' }, set(v) {}, configurable: true})"
    ),
}


@pytest.mark.parametrize("name", list(REFUSED_GLOBALS))
def test_bind_function_refuses_a_global_it_cannot_install(make_rt, name: str) -> None:
    rt = make_rt()
    rt.eval(REFUSED_GLOBALS[name] + "; 0")
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_function("lookup", lambda: "host")


@pytest.mark.parametrize("name", list(REFUSED_GLOBALS))
def test_tool_bridge_without_namespace_fails_closed(make_rt, name: str) -> None:
    from pydeno import ToolBridge

    rt = make_rt()
    rt.eval(REFUSED_GLOBALS[name] + "; 0")
    bridge = ToolBridge({"lookup": lambda: "host"}, namespace=None)
    with pytest.raises(Exception, match=_BIND_REFUSED):
        bridge.attach(rt)
    assert bridge.detach(rt) == 0


def test_an_inherited_setter_cannot_intercept_bind_function(make_rt) -> None:
    rt = make_rt()
    rt.eval(
        "Object.defineProperty(Object.prototype, 'lookup', "
        "{get() { return () => 'guest' }, set(v) {}, configurable: true}); 0"
    )
    rt.bind_function("lookup", lambda: "host")
    assert rt.eval("lookup()") == "host"


def test_bind_function_still_replaces_a_writable_global(make_rt) -> None:
    rt = make_rt()
    rt.eval(
        "var lookup = () => 'guest'; globalThis.other = 1; 0"
    )  # `var`: non-configurable
    rt.bind_function("lookup", lambda: "host")
    rt.bind_function("other", lambda: "other")
    rt.bind_function("lookup", lambda: "again")  # rebinding the same name
    assert rt.eval("[lookup(), other()]") == ["again", "other"]
    rt.eval("Array.prototype[Symbol.iterator] = function* () {}; 0")
    rt.bind_function("echo", lambda v: v)
    assert rt.eval("echo(5)") == 5  # arguments do not go through the guest's iterator


# A host function's result is rebuilt into JavaScript values by the bridge. It used to do that
# with `Array.prototype.map`, `Object.entries`, `for...of` and the global `Array`/`Date`/`Set`/
# `BigInt`, all of which the guest can replace. That only ever let a guest corrupt its own view of
# a result, but the bridge should not run guest code while it does its job.
REVIVE_POISON = {
    "array-map": "Array.prototype.map = () => 'poisoned'",
    "object-entries": "Object.entries = () => [['poisoned', 1]]",
    "array-iterator": "Array.prototype[Symbol.iterator] = function* () { yield 'poisoned' }",
    "array-isarray": "Array.isArray = () => false",
    "global-date": "globalThis.Date = function () { return 'poisoned' }",
    "global-set": "globalThis.Set = function () { return 'poisoned' }",
    "global-bigint": "globalThis.BigInt = () => 'poisoned'",
}


@pytest.mark.parametrize("name", list(REVIVE_POISON))
def test_a_host_result_is_revived_without_guest_code(make_rt, name: str) -> None:
    import datetime

    rt = make_rt()
    rt.bind_function(
        "lookup",
        lambda: {
            "list": [1, 2, {"a": 1}],
            "when": datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
            "set": {1, 2},
            "big": 2**70,
        },
    )
    # Uses only references captured before the poison, and no array iteration.
    rt.eval(
        "globalThis.probe = ((RealDate, RealSet, size, stringify) => () => {"
        " const r = lookup();"
        " return stringify([r.list, r.when instanceof RealDate && r.when.getTime(),"
        " r.set instanceof RealSet && size.call(r.set), typeof r.big]) })"
        "(Date, Set, Object.getOwnPropertyDescriptor(Set.prototype, 'size').get, JSON.stringify);"
        " 0"
    )
    expected = '[[1,2,{"a":1}],1577836800000,2,"bigint"]'
    assert rt.eval("probe()") == expected
    rt.eval(REVIVE_POISON[name] + "; 0")
    assert rt.eval("probe()") == expected


async def test_an_async_host_result_does_not_go_through_a_replaced_promise_then(
    make_rt,
) -> None:
    rt = make_rt()

    async def lookup():
        return [1, 2]

    rt.bind_function("lookup", lookup)
    rt.eval("Promise.prototype.then = function () { return 'poisoned' }; 0")
    assert (
        await rt.eval_async("(async () => JSON.stringify(await lookup()))()") == "[1,2]"
    )


# ---------------------------------------------------------------------------------------------
# Follow-up review of the bind hardening.
# ---------------------------------------------------------------------------------------------

# The Rust converter turns a Python stream into a JS `ReadableStream` through the global
# `__pydeno_from_py_stream`, and `revive` did the same. It was defined after the bridge fixed its
# globals in place, so a guest could replace it (or `ReadableStream`, or plant `Object.prototype`
# getters the stream constructor reads) and run code inside a later host bind, see every stream
# id, and have its own value installed. Streams exist only on the in-process `Runtime`
# (`IsolatedRuntime` has no `stream_from_async_iterable`), so these run there only.
STREAM_POISON = {
    "replace-helper": (
        "globalThis.__pydeno_from_py_stream = (id) => { hits++; return 'guest' }"
    ),
    "replace-readablestream": (
        "globalThis.ReadableStream = function (src) { hits++; return 'guest' }"
    ),
    "object-prototype-getter": (
        "Object.defineProperty(Object.prototype, 'start', "
        "{get() { hits++; return undefined }, configurable: true})"
    ),
}


async def _two_chunks():
    yield "a"
    yield "b"


def _stream_runtime(poison: str):
    from pydeno import Runtime

    rt = Runtime(RuntimeConfig(timeout=20.0))
    rt.eval("globalThis.hits = 0; " + poison + "; 0")
    return rt


@pytest.mark.parametrize("name", list(STREAM_POISON))
async def test_binding_a_python_stream_runs_no_guest_code(name: str) -> None:
    rt = _stream_runtime(STREAM_POISON[name])
    try:
        rt.bind_object("tools", {"s": rt.stream_from_async_iterable(_two_chunks())})
        assert rt.eval("hits") == 0
        assert (
            rt.eval("typeof tools.s") == "object"
        )  # a stream, not the guest's 'guest'
    finally:
        rt.close()


@pytest.mark.parametrize("name", list(STREAM_POISON))
async def test_passing_a_python_stream_to_js_runs_no_guest_code(name: str) -> None:
    rt = _stream_runtime(STREAM_POISON[name])
    try:
        store = rt.eval("(s) => { globalThis.kept = s; }")
        store(rt.stream_from_async_iterable(_two_chunks()))
        assert rt.eval("hits") == 0
        assert rt.eval("typeof kept") == "object"
    finally:
        rt.close()


def test_the_stream_helper_is_fixed_in_place() -> None:
    from pydeno import Runtime

    with Runtime(RuntimeConfig(timeout=20.0)) as rt:
        rt.eval("globalThis.__pydeno_from_py_stream = () => 'guest'; 0")
        assert rt.eval("delete globalThis.__pydeno_from_py_stream") is False
        assert (
            rt.eval("Object.keys(globalThis).includes('__pydeno_from_py_stream')")
            is False
        )


# `ToolBridge.attach(namespace=None)` binds one function at a time. When a later one was refused,
# the earlier ones stayed installed and exposed although the caller got an exception.
_BLOCK_B = (
    "Object.defineProperty(globalThis, 'b', "
    "{value: 1, writable: false, configurable: false}); 0"
)


def test_tool_bridge_without_namespace_is_all_or_nothing(make_rt) -> None:
    from pydeno import ToolBridge

    rt = make_rt()
    rt.eval(_BLOCK_B)
    bridge = ToolBridge({"a": lambda: "a", "b": lambda: "b"}, namespace=None)
    with pytest.raises(Exception, match=r"Cannot bind 'b'"):
        bridge.attach(rt)
    assert bridge.detach(rt) == 0
    # `a` may still name the closure, but its capability was revoked: nothing is callable.
    assert rt.eval("try { a(); 'called' } catch (e) { 'refused' }") == "refused"


def test_tool_bridge_with_namespace_is_all_or_nothing(make_rt) -> None:
    from pydeno import ToolBridge

    rt = make_rt()
    rt.eval(
        "globalThis.tools = {}; Object.defineProperty(tools, 'b', "
        "{value: 1, writable: false, configurable: false}); 0"
    )
    bridge = ToolBridge({"a": lambda: "a", "b": lambda: "b"})
    with pytest.raises(Exception, match=r"Cannot bind 'tools'"):
        bridge.attach(rt)
    assert bridge.detach(rt) == 0
    assert rt.eval("typeof tools.a") == "undefined"


# A refused bind's error is read (its `.stack`) by the host. A guest `Error.prepareStackTrace`
# used to run then, inside the host's bind, and could read the message.
def test_a_refused_bind_does_not_run_prepare_stack_trace(make_rt) -> None:
    rt = make_rt()
    rt.eval(
        "globalThis.seen = null;"
        "Error.prepareStackTrace = (e, frames) => { seen = String(e.message); return 'x' };"
        "globalThis.tools = new Proxy({}, {}); 0"
    )
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_object("tools", {"f": lambda: 1})
    assert rt.eval("seen") is None


# The plain-object test looked at the prototype only, so a guest could point the namespace at an
# intrinsic such as `Object.prototype` and have the host's tools installed on every object.
INTRINSIC_NAMESPACES = {
    "object-prototype": "Object.prototype",
    "array-prototype": "Array.prototype",
    "math": "Math",
    "json": "JSON",
    "reflect": "Reflect",
    "iterator-prototype": "Object.getPrototypeOf(Object.getPrototypeOf([][Symbol.iterator]()))",
    "typedarray-prototype": "Object.getPrototypeOf(Uint8Array.prototype)",
    "global-object": "globalThis",
    "array-unscopables": "Array.prototype[Symbol.unscopables]",
    "intl-collator-prototype": "Intl.Collator.prototype",
    # Reachable only through instances:
    "segments-prototype": "Object.getPrototypeOf(new Intl.Segmenter().segment('a'))",
    "callsite-prototype": (
        "(() => { const saved = Error.prepareStackTrace;"
        " Error.prepareStackTrace = (e, frames) => frames;"
        " const frames = new Error().stack; Error.prepareStackTrace = saved;"
        " return Object.getPrototypeOf(frames[0]) })()"
    ),
    "readablestream-prototype": "ReadableStream.prototype",
}


@pytest.mark.parametrize("name", list(INTRINSIC_NAMESPACES))
def test_bind_object_refuses_an_intrinsic_namespace(make_rt, name: str) -> None:
    rt = make_rt()
    rt.eval(f"globalThis.tools = {INTRINSIC_NAMESPACES[name]}; 0")
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_object("tools", {"zz_host_tool": lambda: 1})
    assert rt.eval("typeof ({}).zz_host_tool") == "undefined"
    assert rt.eval("typeof tools.zz_host_tool") == "undefined"


# ---------------------------------------------------------------------------------------------
# Second follow-up review.
# ---------------------------------------------------------------------------------------------


def test_a_snapshot_provided_namespace_is_not_mistaken_for_a_built_in() -> None:
    """The built-in set was collected after a host snapshot was restored, so every object the
    snapshot's bootstrap created counted as a built-in and could no longer be bound onto.
    (`IsolatedRuntime` refuses snapshots, so this is `Runtime` only.)"""
    from pydeno import Runtime, SnapshotBuilder

    builder = SnapshotBuilder()
    builder.execute_script("lib.js", "globalThis.myLib = {version: '1.0'};")
    snapshot = builder.build()
    with Runtime(RuntimeConfig(snapshot=snapshot, timeout=20.0)) as rt:
        rt.bind_object("myLib", {"f": lambda: 1})
        assert rt.eval("[myLib.version, myLib.f()]") == ["1.0", 1]
        # ...and the built-in check still holds in the same runtime.
        rt.eval("globalThis.tools = Object.prototype; 0")
        with pytest.raises(Exception, match=_BIND_REFUSED):
            rt.bind_object("tools", {"zz_host_tool": lambda: 1})
        assert rt.eval("typeof ({}).zz_host_tool") == "undefined"


# The polyfill `ReadableStream` kept its state in `this._queue = ...` assignments, so setters a
# guest planted on `ReadableStream.prototype` ran inside a host bind and received the source.
_POLYFILL_FIELDS = (
    "_queue",
    "_closed",
    "_errored",
    "_error",
    "_pulling",
    "_underlying",
    "_controller",
)


async def test_a_stream_prototype_setter_does_not_run_inside_a_bind() -> None:
    fields = ", ".join(repr(f) for f in _POLYFILL_FIELDS)
    rt = _stream_runtime(
        f"for (const f of [{fields}]) Object.defineProperty(ReadableStream.prototype, f, "
        "{set(v) { hits++ }, get() {}, configurable: true})"
    )
    try:
        source = rt.stream_from_async_iterable(
            _two_chunks()
        )  # kept alive while JS reads it
        rt.bind_object("tools", {"s": source})
        assert rt.eval("hits") == 0
        result = await rt.eval_async(
            "(async () => { const r = tools.s.getReader(); const out = [];"
            " for (;;) { const {done, value} = await r.read(); if (done) break; out.push(value) }"
            " return out.join('') })()"
        )
        assert result == "ab"
    finally:
        rt.close()


# Converting a guest result to Python asked `value instanceof globalThis.ReadableStream`, which
# runs a guest `Symbol.hasInstance` and follows a replaced global: every object could be made to
# arrive as a stream.
STREAM_BRAND_POISON = {
    "has-instance-true": (
        "Object.defineProperty(ReadableStream, Symbol.hasInstance, "
        "{value: () => { hits++; return true }, configurable: true})"
    ),
    "global-replaced-with-object": "globalThis.ReadableStream = Object",
}


@pytest.mark.parametrize("name", list(STREAM_BRAND_POISON))
def test_converting_a_result_does_not_ask_the_guest_what_a_stream_is(
    make_rt, name: str
) -> None:
    rt = make_rt()
    rt.eval("globalThis.hits = 0; " + STREAM_BRAND_POISON[name] + "; 0")
    assert rt.eval("({a: 1})") == {"a": 1}
    assert rt.eval("hits") == 0


# deno_core reads `name` and `cause` of a thrown error through the prototype chain, so getters
# planted there ran on the refusal error, inside the host's bind, and saw its message.
ERROR_PROTO_POISON = {
    "cause-getter": (
        "Object.defineProperty(Error.prototype, 'cause', "
        "{get() { seen.push(String(this.message)) }, configurable: true})"
    ),
    "name-getter": (
        "Object.defineProperty(TypeError.prototype, 'name', "
        "{get() { seen.push(String(this.message)); return 'TypeError' }, configurable: true})"
    ),
}


@pytest.mark.parametrize("name", list(ERROR_PROTO_POISON))
def test_a_refused_bind_reads_no_error_property_through_the_prototype(
    make_rt, name: str
) -> None:
    rt = make_rt()
    rt.eval(
        "globalThis.seen = []; " + ERROR_PROTO_POISON[name] + ";"
        " globalThis.tools = new Proxy({}, {}); 0"
    )
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_object("tools", {"f": lambda: 1})
    assert rt.eval("seen.length") == 0


def test_a_non_extensible_global_object_is_refused_clearly(make_rt) -> None:
    rt = make_rt()
    rt.eval("Object.preventExtensions(globalThis); 0")
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_object("tools", {"f": lambda: 1})
    with pytest.raises(Exception, match=_BIND_REFUSED):
        rt.bind_function("lookup", lambda: 1)


# A refused bind left its handler registered for the runtime's lifetime.
@pytest.mark.parametrize("path", ["bind_object", "bind_function"])
def test_a_refused_bind_does_not_keep_its_handler(make_rt, path: str) -> None:
    import gc
    import weakref

    class Handler:
        def __call__(self) -> int:
            return 1

    rt = make_rt()
    rt.eval(
        "globalThis.tools = new Proxy({}, {});"
        " Object.defineProperty(globalThis, 'lookup', {get() {}, configurable: true}); 0"
    )
    handler = Handler()
    ref = weakref.ref(handler)
    try:
        if path == "bind_object":
            rt.bind_object("tools", {"f": handler})
        else:
            rt.bind_function("lookup", handler)
    except Exception as exc:
        assert re.search(_BIND_REFUSED, str(exc)), exc
    else:
        raise AssertionError("the bind was not refused")
    del handler
    gc.collect()
    assert ref() is None
