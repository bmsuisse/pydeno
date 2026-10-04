"""`Pydeno` / `PydenoSession`: the Monty-shaped front door (sync).

What it must keep: Monty's shape (`with Pydeno() as pool`, `with pool.checkout() as session`,
`feed_run`, `feed_start` + snapshots, `dump` / `load_session` / `load_snapshot`, `worker_pid`,
`PydenoLimits`, typed errors), the agent sandbox's guarantees underneath (one single-use worker per
session, a signed journal replayed deterministically), and security by default (the OS sandbox is
required, never silently downgraded).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import pydeno
from pydeno import (
    AgentSandbox,
    IsolatedRuntime,
    Pydeno,
    PydenoComplete,
    PydenoCrashedError,
    PydenoError,
    PydenoRuntimeError,
    PydenoSnapshot,
    PydenoSyntaxError,
    PydenoTimeoutError,
    RuntimeConfig,
    classify_error,
)
from pydeno import _front, _isolated
from pydeno._agent import preinstall
from pydeno._front import _completion

# A container matrix profile that simulates a kernel without every layer cannot give "require";
# everything else here does.
_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"


@pytest.fixture(scope="module")
def pool():
    with Pydeno(sandbox=MODE) as p:
        yield p


def _gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# the shape
# ---------------------------------------------------------------------------


class TestShape:
    def test_monty_shape_runs_and_keeps_state(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            assert session.feed_run("1 + 1") == 2
            assert session.feed_run("const x = 20") is None
            assert session.feed_run("function twice(n) { return n * 2 }") is None
            assert session.feed_run("twice(x) + 2") == 42
            session.feed_run("globalThis.counter = (globalThis.counter || 0) + 1")
            session.feed_run("counter += 1")
            assert session.feed_run("counter") == 2

    def test_declarations_after_a_semicolon_persist_too(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            assert session.feed_run("const a = 1; let b = 2; a + b") == 3
            assert session.feed_run("a * 10 + b") == 12

    def test_a_feed_may_await_and_return(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            assert session.feed_run("await Promise.resolve(5)") == 5
            assert session.feed_run("return 7") == 7
            assert session.feed_run("if (true) { return 'early' }\n'late'") == "early"
            assert session.feed_run("") is None
            assert session.feed_run("// only a comment") is None

    def test_results_convert_like_pydeno(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            assert session.feed_run("({a: [1, 'two', null], b: true})") == {
                "a": [1, "two", None],
                "b": True,
            }
            assert session.feed_run("undefined") is None
            assert session.feed_run("2n ** 70n") == 2**70

    def test_checkout_needs_with_and_is_entered_once(self, pool: Pydeno) -> None:
        session = pool.checkout()
        with pytest.raises(RuntimeError, match="with pool.checkout"):
            session.feed_run("1")
        with session:
            assert session.feed_run("1") == 1
        with pytest.raises(RuntimeError, match="once"):
            session.__enter__()
        with pytest.raises(RuntimeError, match="closed"):
            session.feed_run("1")

    def test_script_name_and_repr(self, pool: Pydeno) -> None:
        with pool.checkout(script_name="agent.js") as session:
            assert session.script_name == "agent.js"
            assert "agent.js" in repr(session)

    def test_sandbox_status_is_surfaced(self) -> None:
        status = Pydeno.sandbox_status()
        assert isinstance(status, pydeno.SandboxStatus)
        assert status.platform == pydeno.sandbox_status().platform


class TestCompletion:
    """The trailing expression becomes the feed's result; nothing else changes."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("1 + 1", "return (1 + 1);"),
            ("const x = 1\nx", "const x = 1\nreturn (x);"),
            ("a\n+ b", "return (a\n+ b);"),
            ("x;", "return (x);;"),
            ("(a = 1)", "return ((a = 1));"),
            ("`a${1}b`", "return (`a${1}b`);"),
            ("f()\n(g)", "return (f()\n(g));"),
            ("await go()", "return (await go());"),
            ("/re/.test(s)", "return (/re/.test(s));"),
        ],
        ids=[
            "sum",
            "after-decl",
            "continued",
            "semicolon",
            "paren-assign",
            "template",
            "call",
            "await",
            "regex",
        ],
    )
    def test_rewritten(self, code: str, expected: str) -> None:
        assert _completion(code) == expected

    @pytest.mark.parametrize(
        "code",
        [
            "const x = 1",
            "for (let i = 0; i < 3; i++)\n  f(i)",
            "while (go())\n  step()",
            "if (a)\n  b()\nelse\n  c()",
            "label: x",
            "{ a: 1 }",
            "function f() {}",
            "return 3",
            "throw new Error('x')",
            "x = 'unterminated",
            "x = 5",
            "o.k += 1",
        ],
        ids=[
            "decl",
            "for-body",
            "while-body",
            "else",
            "label",
            "block",
            "function",
            "return",
            "throw",
            "unterminated",
            "assignment",
            "compound-assignment",
        ],
    )
    def test_left_alone(self, code: str) -> None:
        assert _completion(code) == code

    def test_a_body_of_a_loop_still_loops(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            session.feed_run("var n = 0")
            session.feed_run("for (let i = 0; i < 5; i++)\n  n += 1")
            assert session.feed_run("n") == 5

    def test_a_wrong_rewrite_falls_back_to_the_code_as_written(
        self, pool: Pydeno, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Should the rewrite of the last statement ever not compile, the feed runs as written
        # (no result) rather than fail, and nothing ran twice.
        monkeypatch.setattr(_front, "_completion", lambda code: code + " )")
        with pool.checkout() as session:
            assert session.feed_run("var k = (globalThis.k || 0) + 1\nk") is None
            monkeypatch.undo()
            assert session.feed_run("k") == 1
            with pytest.raises(PydenoSyntaxError):
                session.feed_run("x y")


# ---------------------------------------------------------------------------
# inputs, external functions, printing
# ---------------------------------------------------------------------------


class TestInputs:
    def test_inputs_are_globals_that_persist(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            data = {"rows": [1, 2.5, "x", None, True], "nested": {"k": [{"v": 1}]}}
            assert (
                session.feed_run("data.rows.length + y", inputs={"data": data, "y": 10})
                == 15
            )
            assert session.feed_run("data.nested.k[0].v") == 1

    def test_inputs_cannot_inject_code(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            evil = '"); globalThis.pwned = 1; ("'
            assert session.feed_run("s", inputs={"s": evil}) == evil
            assert session.feed_run("typeof pwned") == "undefined"
            # A __proto__ key stays a plain key, never the object's prototype.
            assert (
                session.feed_run(
                    "Object.getPrototypeOf(o) === Object.prototype && o.__proto__.x",
                    inputs={"o": {"__proto__": {"x": 1}}},
                )
                == 1
            )

    @pytest.mark.parametrize(
        "value",
        [object(), b"bytes", {1: "int key"}, float("nan"), 2**60, {1, 2}],
        ids=["object", "bytes", "int-key", "nan", "big-int", "set"],
    )
    def test_only_plain_data_crosses(self, pool: Pydeno, value: object) -> None:
        with pool.checkout() as session:
            with pytest.raises(TypeError):
                session.feed_run("1", inputs={"v": value})
            assert session.feed_run("2") == 2

    @pytest.mark.parametrize(
        "name", ["1x", "a-b", "class", "__pydeno_external", "__proto__", ""], ids=repr
    )
    def test_names_are_checked(self, pool: Pydeno, name: str) -> None:
        with pool.checkout() as session:
            with pytest.raises((ValueError, TypeError)):
                session.feed_run("1", inputs={name: 1})


class TestExternalLookup:
    def test_sync_external_functions(self, pool: Pydeno) -> None:
        seen = []

        def lookup(id_: int) -> dict:
            seen.append(id_)
            return {"id": id_, "name": f"user{id_}"}

        with pool.checkout() as session:
            result = session.feed_run(
                "const u = await lookup(7)\nu.name + ':' + (await lookup(8)).id",
                external_lookup={"lookup": lookup},
            )
            assert result == "user7:8"
            assert seen == [7, 8]
            # The stub is gone with the lookup: a later feed without it cannot reach the host.
            with pytest.raises(PydenoRuntimeError, match="ReferenceError"):
                session.feed_run("await lookup(9)")
            assert seen == [7, 8]

    def test_a_tool_error_reaches_the_guest_redacted(self, pool: Pydeno) -> None:
        def boom() -> None:
            raise ValueError("/secret/path/db.sqlite")

        with pool.checkout() as session:
            out = session.feed_run(
                "try { await boom() } catch (e) { return [e.name, e.message] }",
                external_lookup={"boom": boom},
            )
            assert out == ["ValueError", "host function failed"]
            with pytest.raises(PydenoRuntimeError) as info:
                session.feed_run("await boom()", external_lookup={"boom": boom})
            assert "/secret" not in str(info.value)

    def test_async_external_functions_are_refused_like_monty(
        self, pool: Pydeno
    ) -> None:
        async def fetch() -> int:
            return 1

        with pool.checkout() as session:
            with pytest.raises(RuntimeError, match="AsyncPydeno"):
                session.feed_run("await fetch()", external_lookup={"fetch": fetch})
            assert session.feed_run("3") == 3

    def test_non_callable_values_are_inputs(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            assert session.feed_run("limit * 2", external_lookup={"limit": 21}) == 42

    def test_a_result_the_sandbox_cannot_hold_is_the_guests_error(
        self, pool: Pydeno
    ) -> None:
        with pool.checkout() as session:
            out = session.feed_run(
                "try { await f() } catch (e) { return e.name }",
                external_lookup={"f": object},
            )
            assert out == "TypeError"


class TestPrint:
    def test_print_callback_gets_both_streams(self, pool: Pydeno) -> None:
        got: list[tuple[str, str]] = []
        with pool.checkout() as session:
            session.feed_run(
                "console.log('a', 1, {b: 2}); console.error('bad'); console.info('i')",
                print_callback=lambda stream, text: got.append((stream, text)),
            )
        assert got == [
            ("stdout", 'a 1 {"b":2}\n'),
            ("stderr", "bad\n"),
            ("stdout", "i\n"),
        ]

    def test_default_prints_to_this_process_without_control_characters(
        self, pool: Pydeno, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pool.checkout() as session:
            session.feed_run("console.log('hello\\u001b[2J'); console.warn('w')")
        out, err = capsys.readouterr()
        assert out == "hello?[2J\n"
        assert err == "w\n"

    def test_the_callback_is_per_feed(self, pool: Pydeno) -> None:
        got: list[str] = []
        with pool.checkout() as session:
            session.feed_run(
                "console.log('one')", print_callback=lambda s, t: got.append(t)
            )
            session.feed_run("1", print_callback=lambda s, t: got.append("never"))
        assert got == ["one\n"]


# ---------------------------------------------------------------------------
# feed_start, snapshots, dump/load
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_feed_start_suspends_at_every_external_call(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            snap = session.feed_start(
                "const a = await fetch(3)\nconst b = await fetch(a)\na + b",
                external_lookup={"fetch": lambda n: n * 10},
            )
            assert isinstance(snap, PydenoSnapshot)
            assert (snap.function_name, snap.args, snap.kwargs) == ("fetch", (3,), {})
            snap = snap.resume({"return_value": 7})  # Monty's form
            assert isinstance(snap, PydenoSnapshot) and snap.args == (7,)
            done = snap.resume(value=1)  # pydeno's form
            assert isinstance(done, PydenoComplete)
            assert done.output == 8
            assert session.feed_run("a + b") == 8

    def test_resume_auto_and_errors(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            snap = session.feed_start(
                "try { await f(1) } catch (e) { return e.name + ':' + e.message }",
                external_lookup={"f": lambda n: n},
            )
            assert isinstance(snap, PydenoSnapshot)
            done = snap.resume(error=KeyError("hidden"))
            assert isinstance(done, PydenoComplete)
            assert done.output == "KeyError:host function failed"
            snap = session.feed_start(
                "await f(41)", external_lookup={"f": lambda n: n + 1}
            )
            assert snap.resume_auto().output == 42

    def test_a_snapshot_is_answered_once(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            snap = session.feed_start("await f()", external_lookup={"f": print})
            snap.resume(value=1)
            with pytest.raises(RuntimeError, match="already"):
                snap.resume(value=2)
            with pytest.raises(TypeError):
                session.feed_start("await f()", external_lookup={"f": print}).resume(
                    value=1, error=ValueError()
                )

    def test_uncaught_answer_errors_raise_from_resume(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            snap = session.feed_start("await f()", external_lookup={"f": print})
            with pytest.raises(PydenoRuntimeError, match="TimeoutError"):
                snap.resume({"exc_type": "TimeoutError", "message": "slow"})
            assert session.feed_run("1") == 1

    def test_resume_auto_refuses_an_async_function(self, pool: Pydeno) -> None:
        async def f() -> int:
            return 1

        with pool.checkout() as session:
            snap = session.feed_start("await f()", external_lookup={"f": f})
            with pytest.raises(RuntimeError, match="AsyncPydeno"):
                snap.resume_auto()
            assert snap.resume(value=2).output == 2  # not consumed by the refusal

    def test_dump_and_load_snapshot_round_trip(self, pool: Pydeno) -> None:
        with pool.checkout() as first:
            first.feed_run("var base = 100")
            snap = first.feed_start(
                "const got = await f(1)\nbase + got", external_lookup={"f": print}
            )
            state = snap.dump()
            assert isinstance(state, bytes)
        with pool.checkout() as second:
            restored = second.load_snapshot(
                state, external_lookup={"f": lambda n: n + 1}
            )
            assert isinstance(restored, PydenoSnapshot)
            assert (restored.function_name, restored.args) == ("f", (1,))
            assert restored.resume_auto().output == 102
            assert second.feed_run("got") == 2

    def test_dump_and_load_session(self, pool: Pydeno) -> None:
        printed: list[str] = []
        with pool.checkout() as first:
            first.feed_run("console.log('once')\nvar total = 0")
            first.feed_run(
                "total += await add(5)", external_lookup={"add": lambda n: n}
            )
            first.feed_run("Math.random()")
            first_now = first.feed_run("Date.now()")
            state = first.dump()
            next_rand = first.feed_run("Math.random()")
            old_pid = first.worker_pid
        with pool.checkout() as second:
            second.feed_run(
                "var other = 1", print_callback=lambda s, t: printed.append(t)
            )
            second.load_session(
                state
            )  # replayed on a fresh worker; tools are not called again
            assert second.worker_pid not in (None, old_pid)
            assert second.feed_run("total") == 5
            assert second.feed_run("typeof other") == "undefined"
            # The same frozen clock and the same random stream: replay is deterministic.
            assert second.feed_run("Date.now()") == first_now
            assert second.feed_run("Math.random()") == next_rand
        assert printed == []  # the replay printed nothing

    def test_load_checks_kind_and_authenticity(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            idle = session.dump()
            snap = session.feed_start("await f()", external_lookup={"f": print})
            suspended = snap.dump()
            snap.resume(value=1)
            with pytest.raises(PydenoError, match="load_session"):
                session.load_snapshot(idle)
            with pytest.raises(PydenoError, match="load_snapshot"):
                session.load_session(suspended)
            tampered = bytearray(idle)
            tampered[-1] ^= 1
            with pytest.raises(PydenoError) as info:
                session.load_session(bytes(tampered))
            assert classify_error(info.value).kind == "journal_invalid"
            assert session.feed_run("1 + 1") == 2

    def test_state_loads_only_under_the_same_dump_key(self) -> None:
        key = b"k" * 32
        with Pydeno(sandbox=MODE, min_processes=1, dump_key=key) as a:
            with a.checkout() as session:
                session.feed_run("var v = 9")
                state = session.dump()
        with Pydeno(sandbox=MODE, min_processes=1, dump_key=key) as b:
            with b.checkout() as session:
                session.load_session(state)
                assert session.feed_run("v") == 9
        with Pydeno(sandbox=MODE, min_processes=1) as c:  # a random key of its own
            with c.checkout() as session:
                with pytest.raises(PydenoError):
                    session.load_session(state)

    def test_a_crashed_session_can_be_reloaded(self, pool: Pydeno) -> None:
        with pool.checkout(limits={"max_feed_duration_secs": 0.5}) as session:
            session.feed_run("var kept = 'yes'")
            state = session.dump()
            with pytest.raises(PydenoTimeoutError):
                session.feed_run("for (;;) {}")
            with pytest.raises(PydenoCrashedError):
                session.feed_run("1")
            after = session.dump()  # as of the last good feed
            session.load_session(state)
            assert session.feed_run("kept") == "yes"
        with pool.checkout() as fresh:
            fresh.load_session(after)
            assert fresh.feed_run("kept") == "yes"


# ---------------------------------------------------------------------------
# limits and errors
# ---------------------------------------------------------------------------


class TestLimits:
    def test_feed_duration_kills_the_worker_with_a_timeout_error(
        self, pool: Pydeno
    ) -> None:
        with pool.checkout(limits={"max_feed_duration_secs": 0.5}) as session:
            start = time.monotonic()
            with pytest.raises(PydenoTimeoutError) as info:
                session.feed_run("for (;;) {}")
            assert time.monotonic() - start < 10
            assert isinstance(info.value, TimeoutError)
            assert isinstance(info.value, PydenoCrashedError) and info.value.timed_out
            assert classify_error(info.value).kind in ("timeout", "cpu_limit")
            assert session.worker_pid is None

    def test_time_suspended_at_an_external_call_does_not_count(
        self, pool: Pydeno
    ) -> None:
        def slow() -> int:
            time.sleep(0.8)
            return 1

        with pool.checkout(limits={"max_feed_duration_secs": 0.5}) as session:
            assert session.feed_run("await slow()", external_lookup={"slow": slow}) == 1

    def test_turn_duration_is_at_least_as_strict(self, pool: Pydeno) -> None:
        with pool.checkout(
            limits={"max_feed_duration_secs": 30, "max_turn_duration_secs": 0.5}
        ) as session:
            with pytest.raises(PydenoTimeoutError):
                session.feed_run("for (;;) {}")

    def test_memory_limit_kills_with_a_typed_error(self, pool: Pydeno) -> None:
        with pool.checkout(limits={"max_memory": 200 * 1024 * 1024}) as session:
            with pytest.raises(PydenoCrashedError) as info:
                session.feed_run(
                    "const keep = []; for (;;) keep.push(new Array(1e6).fill(Math.random()))"
                )
            assert not isinstance(info.value, PydenoTimeoutError)
            assert classify_error(info.value).kind == "memory_limit"
        with pool.checkout() as session:  # the pool is unaffected
            assert session.feed_run("1 + 1") == 2

    def test_max_suspensions_is_the_external_call_budget(self, pool: Pydeno) -> None:
        with pool.checkout(limits={"max_suspensions": 2}) as session:
            lookup = {"f": lambda: 1}
            assert (
                session.feed_run("await f() + await f()", external_lookup=lookup) == 2
            )
            out = session.feed_run(
                "try { await f() } catch (e) { return e.name }", external_lookup=lookup
            )
            assert out == "ToolBudgetError"
            with pytest.raises(PydenoRuntimeError) as info:
                session.feed_run("await f()", external_lookup=lookup)
            assert classify_error(info.value).kind == "tool_budget"

    def test_unsupported_and_unknown_limits_are_refused(self, pool: Pydeno) -> None:
        with pytest.raises(ValueError, match="max_recursion_depth"):
            pool.checkout(limits={"max_recursion_depth": 1000})
        with pytest.raises(ValueError, match="gc_interval"):
            Pydeno(limits={"gc_interval": 10})
        with pytest.raises(TypeError, match="unknown limits"):
            pool.checkout(limits={"max_cpu": 1})  # type: ignore[typeddict-unknown-key]
        with pytest.raises(ValueError):
            pool.checkout(limits={"max_feed_duration_secs": -1})
        pool.checkout(limits={"max_total_sleep_secs": 5, "max_recursion_depth": None})

    def test_limits_map_onto_the_session(self, pool: Pydeno) -> None:
        with pool.checkout(
            limits={
                "max_feed_duration_secs": 7,
                "max_host_wait_secs": 9,
                "max_suspensions": 3,
            }
        ) as session:
            agent = session._agent  # noqa: SLF001
            rt = agent._core.rt  # noqa: SLF001
            assert rt._request_timeout == 7  # noqa: SLF001
            assert rt._max_host_wait == 9  # noqa: SLF001
            assert agent.calls_remaining == 3


class TestErrors:
    def test_runtime_errors_carry_name_message_and_display(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            with pytest.raises(PydenoRuntimeError) as info:
                session.feed_run("null.x")
            err = info.value
            assert err.name == "TypeError"
            assert "null" in err.message
            assert str(err) == f"TypeError: {err.message}"
            assert err.display("msg") == err.message
            assert err.display("type-msg") == str(err)
            assert err.display().startswith("TypeError")
            assert isinstance(err.exception(), pydeno.JavaScriptError)
            assert classify_error(err).kind == "js_error"
            with pytest.raises(PydenoRuntimeError, match="Error: custom"):
                session.feed_run("throw new Error('custom')")
            assert session.feed_run("'still alive'") == "still alive"

    def test_syntax_errors_are_told_from_thrown_ones(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            with pytest.raises(PydenoSyntaxError) as info:
                session.feed_run("x y")
            assert info.value.name == "SyntaxError"
            assert classify_error(info.value).kind == "js_error"
            with pytest.raises(PydenoRuntimeError) as thrown:
                session.feed_run("JSON.parse('{')")
            assert not isinstance(thrown.value, PydenoSyntaxError)
            assert thrown.value.name == "SyntaxError"
            assert session.feed_run("1") == 1

    def test_every_error_is_a_pydeno_error(self) -> None:
        for cls in (
            PydenoRuntimeError,
            PydenoSyntaxError,
            PydenoCrashedError,
            PydenoTimeoutError,
        ):
            assert issubclass(cls, PydenoError)
        assert issubclass(PydenoTimeoutError, TimeoutError)


# ---------------------------------------------------------------------------
# secure by default
# ---------------------------------------------------------------------------

_REFUSING_WORKER = textwrap.dedent(
    """
    import json, struct, sys
    h = sys.stdin.buffer.read(4)
    sys.stdin.buffer.read(struct.unpack("<I", h)[0])
    msg = json.dumps({"t": "error", "msg": "an OS sandbox is required but ['seatbelt'] could "
                      "not be applied here (applied: none)"}).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(msg)) + msg)
    sys.stdout.buffer.flush()
    """
)


def _fake_python(tmp_path: Path) -> str:
    script = tmp_path / "refuse.py"
    script.write_text(_REFUSING_WORKER)
    wrapper = tmp_path / "refuse.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


class TestSecureByDefault:
    def test_refuses_to_start_without_the_os_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _fake_python(tmp_path)
        real = _isolated._start_worker
        monkeypatch.setattr(_isolated, "_start_worker", lambda python: real(fake))
        with pytest.raises(PydenoCrashedError) as info:
            Pydeno(min_processes=1)
        message = str(info.value)
        assert "sandbox_status()" in message
        assert "sandbox='auto'" in message
        assert classify_error(info.value).kind == "sandbox_unavailable"

    def test_the_default_is_require(self, pool: Pydeno) -> None:
        assert Pydeno.__init__.__kwdefaults__["sandbox"] == "require"  # type: ignore[index]
        assert Pydeno.__init__.__kwdefaults__["jitless"] is True  # type: ignore[index]

    def test_bad_arguments_are_refused(self) -> None:
        with pytest.raises(ValueError, match="sandbox"):
            Pydeno(sandbox="maybe")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="dump_key"):
            Pydeno(dump_key=b"short")
        with pytest.raises(ValueError, match="min_processes"):
            Pydeno(min_processes=0)


# ---------------------------------------------------------------------------
# the pool
# ---------------------------------------------------------------------------


class TestPool:
    def test_a_worker_is_single_use(self, pool: Pydeno) -> None:
        pids = set()
        for _ in range(3):
            with pool.checkout() as session:
                session.feed_run("globalThis.leak = 'secret'")
                pid = session.worker_pid
                assert pid is not None and pid not in pids
                pids.add(pid)
            assert _gone(pid), "the worker must die with its session"
            with pool.checkout() as session:
                assert session.feed_run("typeof leak") == "undefined"

    def test_concurrent_checkouts_are_isolated(self) -> None:
        errors: list[BaseException] = []
        results: dict[int, object] = {}

        with Pydeno(sandbox=MODE, min_processes=2) as p:

            def work(i: int) -> None:
                try:
                    with p.checkout() as session:
                        session.feed_run(f"var mine = {i}")
                        time.sleep(0.05)
                        results[i] = session.feed_run("mine")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
        assert not errors
        assert results == {i: i for i in range(6)}

    def test_exhaustion_falls_back_to_a_cold_start(self) -> None:
        with Pydeno(sandbox=MODE, min_processes=1) as p:
            sessions = [p.checkout() for _ in range(3)]
            for s in sessions:
                s.__enter__()
            try:
                assert [s.feed_run(f"{i} + 1") for i, s in enumerate(sessions)] == [
                    1,
                    2,
                    3,
                ]
                assert p.stats()["cold_starts"] >= 1
            finally:
                for s in sessions:
                    s.close()

    def test_a_session_with_its_own_memory_limit_gets_a_fresh_worker(self) -> None:
        with Pydeno(sandbox=MODE, min_processes=1) as p:
            before = p.stats()["checkouts"]
            with p.checkout(limits={"max_memory": 300 * 1024 * 1024}) as session:
                assert session._agent._core.rt._max_memory == 300 * 1024 * 1024  # noqa: SLF001
                assert session.feed_run("1") == 1
            assert p.stats()["checkouts"] == before

    def test_closing_the_pool_spares_checked_out_sessions(self) -> None:
        p = Pydeno(sandbox=MODE, min_processes=1)
        with p.checkout() as session:
            p.close()
            assert session.feed_run("5") == 5


# ---------------------------------------------------------------------------
# AgentSandbox(runtime=...): the injection point the front door uses
# ---------------------------------------------------------------------------


def _adoptable(**options: object) -> IsolatedRuntime:
    return IsolatedRuntime(
        RuntimeConfig(on_console=lambda level, args: None),
        sandbox=MODE,
        random_seed=1234,
        **options,  # type: ignore[arg-type]
    )


class TestAgentSandboxRuntime:
    def test_adopts_a_runtime_and_its_seed(self) -> None:
        rt = _adoptable()
        with AgentSandbox({"f": lambda: 1}, runtime=rt, timeout=5) as sb:
            assert sb.random_seed == 1234
            assert sb.run("return await f()") == 1
            assert sb.execute("console.log('x'); return 2").stdout == "x\n"
            # no frozen clock at start-up: the session froze it before any guest code ran
            assert sb.run("return Date.now()") == sb.run("return Date.now()")
            assert rt._request_timeout == 5  # noqa: SLF001
        assert rt.is_closed()

    def test_a_journal_from_an_adopted_runtime_loads_anywhere(self) -> None:
        key = b"j" * 32
        with AgentSandbox({}, runtime=_adoptable()) as sb:
            sb.run("globalThis.t = Date.now(); globalThis.r = Math.random()")
            expected = sb.run("return [t, r]")
            blob = sb.dump(key)
        with AgentSandbox.load(blob, key, {}, sandbox=MODE) as plain:
            assert plain.run("return [t, r]") == expected

    def test_refuses_what_it_cannot_honour(self) -> None:
        rt = _adoptable()
        try:
            with pytest.raises(TypeError, match="drop"):
                AgentSandbox({}, runtime=rt, max_memory=1)
            with pytest.raises(ValueError, match="random_seed"):
                AgentSandbox({}, runtime=rt, random_seed=1)
            with pytest.raises(TypeError, match="IsolatedRuntime"):
                AgentSandbox({}, runtime=object())  # type: ignore[arg-type]
            assert not rt.is_closed()  # a refusal leaves it with the caller
        finally:
            rt.close()
        with IsolatedRuntime(sandbox=MODE, random_seed=1) as quiet:
            with pytest.raises(ValueError, match="console"):
                AgentSandbox({}, runtime=quiet)
        with IsolatedRuntime(RuntimeConfig(on_console=print), sandbox=MODE) as unseeded:
            with pytest.raises(ValueError, match="random_seed"):
                AgentSandbox({}, runtime=unseeded)
        used = _adoptable()
        try:
            used.bind_function("x", lambda: 1)
            with pytest.raises(ValueError, match="fresh"):
                AgentSandbox({}, runtime=used)
        finally:
            used.close()


# ---------------------------------------------------------------------------
# pre-installed workers and runs driven on the caller's thread
# ---------------------------------------------------------------------------


def _prepared(names: list[str]) -> IsolatedRuntime:
    rt = _adoptable()
    preinstall(rt, names)
    return rt


class TestPreinstalled:
    def test_the_clock_is_frozen_at_checkout_not_when_the_worker_started(self) -> None:
        with Pydeno(sandbox=MODE, min_processes=1) as p:
            assert p._pool.wait_ready(30)  # noqa: SLF001
            time.sleep(1.5)  # the worker waits in the pool
            before = int(time.time() * 1000)
            with p.checkout() as session:
                now = session.feed_run("Date.now()")
                assert before - 5 <= now <= int(time.time() * 1000) + 5
                assert session.feed_run("Date.now()") == now  # frozen
                assert session.feed_run("new Date().getTime()") == now
                # The freezer is gone before the first feed's code ran, and stays gone.
                assert session.feed_run("typeof __pydeno_agent_freeze") == "undefined"

    def test_a_first_feed_that_does_not_compile_still_freezes_before_guest_code(
        self, pool: Pydeno
    ) -> None:
        with pool.checkout() as session:
            with pytest.raises(PydenoSyntaxError):
                session.feed_run("x y")
            first = session.feed_run("Date.now()")
            assert session.feed_run("Date.now()") == first
        with pool.checkout() as session:
            # A SyntaxError the guest throws: the freeze ran; sending it again changes nothing.
            with pytest.raises(PydenoRuntimeError):
                session.feed_run("JSON.parse('{')")
            first = session.feed_run("Date.now()")
            assert session.feed_run("Date.now()") == first

    def test_dump_after_a_preinstalled_checkout_replays_to_the_same_state(
        self, pool: Pydeno
    ) -> None:
        with pool.checkout() as session:
            assert session._agent._core.rt._pydeno_prepared is not None  # noqa: SLF001
            session.feed_run(
                "var t = Date.now(); var r = [Math.random(), Math.random()]"
            )
            session.feed_run(
                "var got = await Promise.all([f(1), f(2), f(3)])",
                external_lookup={"f": lambda n: n * 10},
            )
            state = session.dump()
            expected = session.feed_run("[t, r, got, Date.now(), Math.random()]")
        with pool.checkout() as other:
            other.load_session(state)
            assert other.feed_run("[t, r, got, Date.now(), Math.random()]") == expected
        # The same journal through the plain agent sandbox (threaded replay, a fresh worker).
        with AgentSandbox.load(
            state,
            pool._key,
            {"__pydeno_external": print},
            sandbox=MODE,  # noqa: SLF001
        ) as plain:
            assert (
                plain.run("return [t, r, got, Date.now(), Math.random()]") == expected
            )

    def test_a_prepared_runtime_serves_exactly_one_matching_session(self) -> None:
        rt = _prepared(["f"])
        with pytest.raises(ValueError, match="prepared for other tools"):
            AgentSandbox({"g": print}, runtime=rt)
        assert not rt.is_closed()  # a refusal leaves it with the caller
        sb = AgentSandbox({"f": lambda: 3}, runtime=rt)
        try:
            assert sb.run("return await f()") == 3
            with pytest.raises(ValueError, match="used already"):
                AgentSandbox({"f": print}, runtime=rt)
            assert sb.run("return 4") == 4  # the refusal did not touch the session
        finally:
            sb.close()

    def test_a_prepared_runtime_answers_no_call_before_it_is_adopted(self) -> None:
        rt = _prepared(["f"])
        try:
            with pytest.raises(pydeno.JavaScriptError, match="RuntimeError"):
                rt._request(  # noqa: SLF001
                    {"t": "eval_async", "code": "f()", "timeout": None},
                    soft_timeout=None,
                )
        finally:
            rt.close()


class TestRunsOnTheCallersThread:
    def test_run_needs_no_loop_thread_and_answers_tools_in_the_callers_context(
        self,
    ) -> None:
        seen: list[str] = []
        var: contextvars.ContextVar[str] = contextvars.ContextVar("v", default="unset")

        def tool(n: int) -> int:
            seen.append(var.get())
            return n + 1

        var.set("caller")
        with AgentSandbox({"tool": tool}, sandbox=MODE) as sb:
            assert sb.run("return await tool(1) + await tool(2)") == 5
            assert seen == ["caller", "caller"]
            assert sb._core.thread is None  # noqa: SLF001
            # start/resume still work on the same session (the loop starts on demand)
            step = sb.start("return await tool(5)")
            assert step.name == "tool"
            assert sb.resume(step, 9).value == 9
            assert sb.run("return await tool(10)") == 11

    def test_async_tools_and_re_entry(self) -> None:
        async def slow(n: int) -> int:
            await asyncio.sleep(0)
            return n * 2

        with AgentSandbox({"slow": slow}, sandbox=MODE) as sb:
            assert sb.run("return await slow(21)") == 42

        holder: list[AgentSandbox] = []

        def reenter() -> int:
            return holder[0].run("return 1")

        with AgentSandbox({"reenter": reenter}, sandbox=MODE) as sb:
            holder.append(sb)
            out = sb.run("try { await reenter() } catch (e) { return e.name }")
            assert out == "RuntimeError"
            assert sb.run("return 2") == 2

    def test_deadlines_still_kill_a_run_driven_here(self) -> None:
        with AgentSandbox({}, sandbox=MODE, timeout=0.5) as sb:
            start = time.monotonic()
            with pytest.raises(pydeno.RuntimeTimeout):
                sb.run("for (;;) {}")
            assert time.monotonic() - start < 10
            assert sb.is_closed()


def test_import_pydeno_stays_light() -> None:
    code = (
        "import sys, pydeno\n"
        "assert 'pydeno._front' not in sys.modules\n"
        "assert 'pydeno._agent' not in sys.modules\n"
        "pydeno.Pydeno\n"
        "assert 'pydeno._front' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ})
