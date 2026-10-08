"""What error text an isolated guest can read (issues #131 and #135).

A guest is untrusted, so the messages it can reach must not name the host's language, config
fields, docs paths, API surface or bridge scripts. An in-process `Runtime` is driven by the host
that owns it and keeps its guiding text; only an isolated worker turns the terse mode on.
"""

from __future__ import annotations

import asyncio

import pytest
from pydeno import IsolatedRuntime, JavaScriptError, Runtime, RuntimeConfig

HOST_DETAIL = (
    "RuntimeConfig",
    "max_serialization",
    "docs/",
    ".md",
    "add_static_module",
    "set_int_max_str_digits",
    "digits",
    "sys.",
    "Python",
    "python_bridge",
    "ext:",
)


def _assert_no_host_detail(text: str) -> None:
    leaked = [word for word in HOST_DETAIL if word in text]
    assert not leaked, f"guest-visible text leaked {leaked}: {text!r}"


@pytest.fixture(scope="module")
def rt():  # type: ignore[no-untyped-def]
    with IsolatedRuntime(RuntimeConfig(timeout=10)) as runtime:
        runtime.bind_function("host", lambda *args: 1)
        yield runtime


GUEST_CATCH = "try { %s; 'no error' } catch (e) { e.name + ': ' + e.message }"


@pytest.mark.parametrize(
    "call",
    [
        "host(1n << 100000n)",
        "host('x'.repeat(2 ** 26))",
        "host(Array.from({length: 1}, () => 'x'.repeat(2 ** 26)))",
    ],
)
def test_marshalling_and_limit_errors_in_the_guest_carry_no_host_detail(
    rt,
    call: str,  # type: ignore[no-untyped-def]
) -> None:
    text = rt.eval(GUEST_CATCH % call)
    assert text != "no error"
    _assert_no_host_detail(text)


def test_the_guest_still_learns_that_the_limit_was_hit(rt) -> None:  # type: ignore[no-untyped-def]
    assert "limit exceeded" in rt.eval(GUEST_CATCH % "host('x'.repeat(2 ** 26))")
    assert "too large" in rt.eval(GUEST_CATCH % "host(1n << 100000n)")


def test_a_bigint_result_past_the_digit_limit_names_no_interpreter_setting(rt) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError) as excinfo:
        rt.eval("10n ** 5000n")
    _assert_no_host_detail(str(excinfo.value))


def test_a_denied_module_names_no_host_api(rt) -> None:  # type: ignore[no-untyped-def]
    text = asyncio.run(
        rt.eval_async(
            "(async () => { try { await import('nonexistent'); return 'ok' }"
            " catch (e) { return e.name + ': ' + e.message } })()"
        )
    )
    assert text == "TypeError: Module resolution denied for nonexistent"


def test_an_in_process_runtime_keeps_its_guiding_text() -> None:
    """The terse mode is the isolated worker's, not a process-wide switch of the parent."""
    with Runtime(RuntimeConfig(timeout=10, max_serialization_bytes=1000)) as inner:
        inner.bind_function("host", lambda *args: 1)
        text = inner.eval(GUEST_CATCH % "host('x'.repeat(5000))")
    assert "max_serialization_bytes" in text

    async def denied_import() -> str:
        with Runtime(RuntimeConfig(timeout=10)) as inner:
            return await inner.eval_async(
                "(async () => { try { await import('nonexistent'); return 'ok' }"
                " catch (e) { return e.message } })()"
            )

    text = asyncio.run(denied_import())
    assert "add_static_module" in text


# -- stack traces (issue #131 items 4 and 5) ---------------------------------------------------


def test_the_guest_cannot_install_a_prepare_stack_trace_hook(rt) -> None:  # type: ignore[no-untyped-def]
    seen = rt.eval(
        "Error.prepareStackTrace = (e, sites) => sites.map(s => s.getFileName()).join(',');"
        "String(new Error('q').stack)"
    )
    assert seen.startswith("Error: q\n    at ")
    desc = rt.eval(
        "(() => { const d = Object.getOwnPropertyDescriptor(Error, 'prepareStackTrace');"
        "return [d.writable, d.configurable, d.enumerable] })()"
    )
    assert desc == [False, False, False]
    assert "read only" in rt.eval(
        "'use strict'; try { Error.prepareStackTrace = 1 } catch (e) { e.message }"
    )
    assert rt.eval("delete Error.prepareStackTrace") is False
    with pytest.raises(JavaScriptError):
        rt.eval(
            "Object.defineProperty(Error, 'prepareStackTrace', {value: () => 'x'}); 0"
        )


def test_stack_traces_name_no_bridge_frames(rt) -> None:  # type: ignore[no-untyped-def]
    rt.bind_function("boom", lambda: 1 / 0)
    stacks = rt.eval(
        """(() => {
        const out = [];
        try { boom() } catch (e) { out.push(e.stack) }
        try { host(1n << 100000n) } catch (e) { out.push(e.stack) }
        const o = {}; Error.captureStackTrace(o); out.push(o.stack);
        return out })()"""
    )
    assert len(stacks) == 3
    for stack in stacks:
        _assert_no_host_detail(stack)
        assert "<eval>" in stack or stack.startswith("TypeError") or "Error" in stack


def test_the_stack_keeps_the_error_header_and_guest_frames(rt) -> None:  # type: ignore[no-untyped-def]
    stack = rt.eval(
        "function inner() { return new TypeError('boom').stack }"
        "function outer() { return inner() } outer()"
    )
    lines = stack.splitlines()
    assert lines[0] == "TypeError: boom"
    assert any("inner" in line for line in lines[1:])
    assert any("outer" in line for line in lines[1:])


def test_a_throwing_name_getter_cannot_break_the_formatter(rt) -> None:  # type: ignore[no-untyped-def]
    text = rt.eval(
        "(() => { const e = new Error('m');"
        "Object.defineProperty(e, 'name', {get() { throw new Error('nope') }});"
        "return typeof e.stack })()"
    )
    assert text == "string"


def test_a_hosts_own_prepare_stack_trace_bootstrap_still_runs() -> None:
    config = RuntimeConfig(
        timeout=10,
        bootstrap="Error.prepareStackTrace = (e, sites) => 'host: ' + e.message;",
    )
    with IsolatedRuntime(config) as runtime:
        # The host's hook is replaced by the pinned one after the bootstrap ran: it did not fail.
        assert runtime.eval("1 + 1") == 2


# -- bridge globals and late binds (issue #135) --------------------------------------------------

BRIDGE_GLOBALS = [
    "__pydenoCallSync",
    "__pydenoCallAsync",
    "__host_op_sync__",
    "__host_op_async__",
    "__pydeno_bind_object",
    "__pydeno_bind_function",
    "__pydeno_from_py_stream",
]


@pytest.mark.parametrize("name", BRIDGE_GLOBALS)
def test_bridge_globals_are_fixed_in_place(rt, name: str) -> None:  # type: ignore[no-untyped-def]
    got = rt.eval(
        f"(() => {{ const d = Object.getOwnPropertyDescriptor(globalThis, '{name}');"
        "return [typeof d, d.writable, d.configurable, d.enumerable] })()"
    )
    assert got == ["object", False, False, False]
    assert rt.eval(f"delete globalThis['{name}']") is False
    assert rt.eval(f"globalThis['{name}'] = 1; typeof globalThis['{name}']") == (
        "function"
    )
    with pytest.raises(JavaScriptError):
        rt.eval(f"Object.defineProperty(globalThis, '{name}', {{value: 1}}); 0")


def test_a_late_bind_over_a_guest_made_read_only_global_fails_loudly() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=10)) as runtime:
        runtime.eval(
            "Object.defineProperty(globalThis, 'late',"
            " {value: 1, writable: false, configurable: false}); 0"
        )
        with pytest.raises(JavaScriptError, match=r"Cannot bind 'late'"):
            runtime.bind_function("late", lambda: "host")
        assert runtime.eval("late") == 1


# -- the host side of an isolated runtime keeps the detail (issue #131 residual) -------------------


def _notes(exc: BaseException) -> str:
    return "\n".join(getattr(exc, "__notes__", []))


@pytest.mark.exception_notes
def test_the_host_exception_for_a_size_limit_names_the_limit_the_guest_does_not_see() -> (
    None
):
    config = RuntimeConfig(timeout=10, max_serialization_bytes=1000)
    with IsolatedRuntime(config) as runtime:
        runtime.bind_function("host", lambda *args: 1)
        with pytest.raises(JavaScriptError) as excinfo:
            runtime.eval("host('x'.repeat(5000))")
        guest = runtime.eval(GUEST_CATCH % "host('x'.repeat(5000))")
    _assert_no_host_detail(guest)
    assert "Serialization size limit exceeded" in str(excinfo.value)
    _assert_no_host_detail(str(excinfo.value))
    notes = _notes(excinfo.value)
    assert "max_serialization_bytes=1000" in notes
    assert "arrow-ipc-dataframes.md" in notes


@pytest.mark.exception_notes
def test_the_host_exception_for_a_depth_limit_names_the_limit() -> None:
    config = RuntimeConfig(timeout=10, max_serialization_depth=8)
    with IsolatedRuntime(config) as runtime:
        runtime.bind_function("host", lambda *args: 1)
        with pytest.raises(JavaScriptError) as excinfo:
            runtime.eval("let o = 0; for (let i = 0; i < 50; i++) o = [o]; host(o)")
    assert "max_serialization_depth=8" in _notes(excinfo.value)


@pytest.mark.exception_notes
def test_the_host_exception_for_a_denied_module_carries_the_hint() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=10)) as runtime:
        with pytest.raises(JavaScriptError) as excinfo:
            asyncio.run(runtime.eval_async("import('nonexistent')"))
    assert "Module resolution denied for nonexistent" in str(excinfo.value)
    assert "add_static_module" in _notes(excinfo.value)


@pytest.mark.exception_notes
def test_the_host_exception_for_a_bigint_result_names_the_interpreter_setting(
    rt,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError) as excinfo:
        rt.eval("10n ** 5000n")
    assert "set_int_max_str_digits" in _notes(excinfo.value)


def test_an_unrelated_error_gets_no_note(rt) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(JavaScriptError) as excinfo:
        rt.eval("throw new Error('plain')")
    if hasattr(excinfo.value, "add_note"):
        assert not getattr(excinfo.value, "__notes__", [])


@pytest.mark.exception_notes
def test_the_async_runtime_attaches_the_same_notes() -> None:
    from pydeno import AsyncIsolatedRuntime

    async def go() -> BaseException:
        config = RuntimeConfig(timeout=10, max_serialization_bytes=1000)
        async with AsyncIsolatedRuntime(config) as runtime:
            await runtime.bind_function("host", lambda *args: 1)
            try:
                await runtime.eval("host('x'.repeat(5000))")
            except JavaScriptError as exc:
                return exc
        raise AssertionError("no error")

    assert "max_serialization_bytes=1000" in _notes(asyncio.run(go()))
