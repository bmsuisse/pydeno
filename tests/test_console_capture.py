"""Tests for `RuntimeConfig(on_console=...)` -- routing guest `console.*`
output back to the Python caller.

Before v0.3, `console.log` from sandboxed JS either went to the *process's*
stdout (`enable_console=True`) or nowhere at all (`enable_console=False`,
the default). There was no channel back to the embedding Python code, so
"show the model what its script printed" was not expressible without
capturing the whole process's file descriptors.

`on_console` and `enable_console` are independent knobs; the composition
matrix is asserted below in `TestConsoleComposition`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pydeno import Runtime, RuntimeConfig, undefined


def _capturing_runtime(records: list[tuple[str, list[Any]]], **kwargs: Any) -> Runtime:
    def on_console(level: str, args: list[Any]) -> None:
        records.append((level, args))

    return Runtime(RuntimeConfig(on_console=on_console, **kwargs))


def test_console_log_reaches_the_python_callback() -> None:
    """The headline behaviour: a console.log in guest JS arrives in Python."""
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.eval("console.log('hello from the sandbox'); void 0;")

    assert records == [("log", ["hello from the sandbox"])]


def test_every_console_level_is_forwarded_with_its_own_name() -> None:
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.eval("""
          console.log('l');
          console.info('i');
          console.warn('w');
          console.error('e');
          console.debug('d');
          console.trace('t');
          void 0;
        """)

    assert [level for level, _ in records] == [
        "log",
        "info",
        "warn",
        "error",
        "debug",
        "trace",
    ]
    assert [args[0] for _, args in records] == ["l", "i", "w", "e", "d", "t"]


def test_arguments_arrive_structured_not_stringified() -> None:
    """`args` keeps real Python types, using the normal conversion rules.

    This is the difference between a console bridge and a print bridge: the
    host gets the values, not a rendering of them.
    """
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.eval("console.log('n', 42, 1.5, true, [1, 2], {a: {b: 3}}); void 0;")

    (level, args) = records[0]
    assert level == "log"
    assert args == ["n", 42, 1.5, True, [1, 2], {"a": {"b": 3}}]


def test_console_null_and_undefined_both_arrive_as_js_undefined() -> None:
    """Documents (rather than changes) the existing host-callback convention.

    Every op-argument path in pydeno collapses JS `null` and `undefined`
    onto the same `JsUndefined` sentinel -- this is not specific to console
    capture, it is what `bind_function`/`bind_object` handlers have always
    received. Pinned here so console capture's argument semantics are stated
    explicitly rather than surprising someone.
    """
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.eval("console.log(null, undefined); void 0;")

    (_, args) = records[0]
    assert args == [undefined, undefined]


def test_multiple_calls_are_captured_in_order() -> None:
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.eval("for (let i = 0; i < 5; i++) console.log(i); void 0;")

    assert [args[0] for _, args in records] == [0, 1, 2, 3, 4]


def test_console_with_no_arguments_yields_an_empty_list() -> None:
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.eval("console.log(); void 0;")

    assert records == [("log", [])]


async def test_console_is_captured_from_eval_async() -> None:
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        await rt.eval_async(
            "(async () => { console.log('async'); await Promise.resolve(); })()"
        )

    assert ("log", ["async"]) in records


def test_console_output_from_the_bootstrap_script_is_captured() -> None:
    """Capture is installed before the user's bootstrap runs."""
    records: list[tuple[str, list[Any]]] = []

    def on_console(level: str, args: list[Any]) -> None:
        records.append((level, args))

    config = RuntimeConfig(
        on_console=on_console, bootstrap="console.log('from bootstrap');"
    )
    with Runtime(config) as rt:
        rt.eval("1")

    assert ("log", ["from bootstrap"]) in records


class TestConsoleRobustness:
    """A console call must never break the script that made it."""

    def test_unrepresentable_argument_degrades_to_a_string(self) -> None:
        """A circular object can't cross the FFI boundary; it must not throw.

        The bridge retries the call with every argument stringified, and
        drops the message entirely only if even that fails. Either way guest
        JS sees `console.log` return normally.
        """
        records: list[tuple[str, list[Any]]] = []
        with _capturing_runtime(records) as rt:
            result = rt.eval("""
              const circular = {};
              circular.self = circular;
              console.log(circular);
              'script-survived'
            """)

        assert result == "script-survived"
        assert len(records) == 1
        (level, args) = records[0]
        assert level == "log"
        assert [isinstance(a, str) for a in args] == [True]

    def test_a_raising_callback_does_not_break_guest_js(self) -> None:
        """If the host callback itself raises, the script still completes.

        A logging sink is not allowed to become a control-flow channel into
        the sandbox.
        """

        def exploding(level: str, args: list[Any]) -> None:
            raise RuntimeError("sink is down")

        with Runtime(RuntimeConfig(on_console=exploding)) as rt:
            assert rt.eval("console.log('x'); 'still-here'") == "still-here"

    def test_console_calls_inside_a_try_block_do_not_alter_control_flow(self) -> None:
        records: list[tuple[str, list[Any]]] = []
        with _capturing_runtime(records) as rt:
            result = rt.eval("""
              let reached = false;
              try { console.log('in try'); reached = true; }
              catch (e) { reached = 'caught: ' + e }
              reached
            """)
        assert result is True


class TestConsoleComposition:
    """`on_console` and `enable_console` are orthogonal (see RuntimeConfig)."""

    def test_callback_fires_even_when_enable_console_is_false(self) -> None:
        """The default `enable_console=False` stubs console to no-ops, but the
        callback is installed *after* that stub, so capture still works."""
        records: list[tuple[str, list[Any]]] = []
        with _capturing_runtime(records, enable_console=False) as rt:
            rt.eval("console.log('captured anyway'); void 0;")
        assert records == [("log", ["captured anyway"])]

    def test_callback_fires_when_enable_console_is_true(self) -> None:
        records: list[tuple[str, list[Any]]] = []
        with _capturing_runtime(records, enable_console=True) as rt:
            rt.eval("console.log('both'); void 0;")
        assert records == [("log", ["both"])]

    def test_console_is_still_stubbed_when_no_callback_is_given(self) -> None:
        """No on_console: unchanged v0.2 behaviour, console.* is a no-op."""
        with Runtime(RuntimeConfig(enable_console=False)) as rt:
            assert rt.eval("typeof console.log") == "function"
            assert rt.eval("console.log('nowhere')") is undefined


class TestConsoleConfig:
    def test_on_console_defaults_to_none(self) -> None:
        assert RuntimeConfig().on_console is None

    def test_on_console_round_trips_through_the_property(self) -> None:
        def sink(level: str, args: list[Any]) -> None:
            pass

        config = RuntimeConfig(on_console=sink)
        assert config.on_console is sink

    def test_on_console_can_be_cleared(self) -> None:
        def sink(level: str, args: list[Any]) -> None:
            pass

        config = RuntimeConfig(on_console=sink)
        config.on_console = None
        assert config.on_console is None

    def test_a_non_callable_on_console_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be callable"):
            RuntimeConfig(on_console="not a function")


def test_console_capture_works_alongside_bound_functions() -> None:
    """Console capture is just another op; it must not collide with the
    op ids `bind_function` hands out."""
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        rt.bind_function("double", lambda x: x * 2)
        assert rt.eval("console.log('calling'); double(21)") == 42
    assert records == [("log", ["calling"])]


def test_each_runtime_gets_its_own_console_sink() -> None:
    a: list[tuple[str, list[Any]]] = []
    b: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(a) as rt_a, _capturing_runtime(b) as rt_b:
        rt_a.eval("console.log('to-a'); void 0;")
        rt_b.eval("console.log('to-b'); void 0;")

    assert a == [("log", ["to-a"])]
    assert b == [("log", ["to-b"])]


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_async_callback_is_rejected_at_config_time_or_ignored_safely() -> None:
    """`console.log` is synchronous, so an async sink can never be awaited.

    Documented contract: the callback must be synchronous. An async one is
    registered as a sync op, so calling it produces an un-awaited coroutine
    rather than captured output -- guest JS must still not break.
    """

    async def async_sink(level: str, args: list[Any]) -> None:  # pragma: no cover
        pass

    with Runtime(RuntimeConfig(on_console=async_sink)) as rt:
        # The important guarantee: the script completes regardless.
        assert rt.eval("console.log('x'); 'ok'") == "ok"


def test_console_capture_survives_a_thrown_script() -> None:
    """Output produced before an exception is still delivered."""
    records: list[tuple[str, list[Any]]] = []
    with _capturing_runtime(records) as rt:
        with pytest.raises(Exception):
            rt.eval("console.log('before the throw'); throw new Error('boom')")

    assert records == [("log", ["before the throw"])]


def test_console_capture_under_concurrent_async_evals() -> None:
    """Capture is per-runtime state; concurrent evals on one runtime must not
    lose or duplicate messages."""
    records: list[tuple[str, list[Any]]] = []

    async def scenario() -> None:
        with _capturing_runtime(records) as rt:
            await asyncio.gather(
                *[
                    rt.eval_async(
                        f"(async () => {{ console.log('task-{i}'); "
                        f"await Promise.resolve(); }})()"
                    )
                    for i in range(8)
                ]
            )

    asyncio.run(scenario())

    assert sorted(args[0] for _, args in records) == [f"task-{i}" for i in range(8)]
