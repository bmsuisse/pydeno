"""`expose_host_errors` (#146): an allowlist of exceptions whose message the guest may see."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    IsolatedRuntime,
    RuntimeConfig,
    SandboxPool,
)

pytestmark = pytest.mark.full_sandbox

KEY = b"0123456789abcdef0123456789abcdef"
SECRET = "hunter2-secret"
REDACTED = "host function failed"

# Everything a guest can read about a failure, as one JSON string.
PROBE = """
(f) => { try { f(); return 'no error'; } catch (e) {
  const own = {};
  for (const k of Object.getOwnPropertyNames(e)) { own[k] = String(e[k]); }
  return JSON.stringify({name: e.name, message: e.message, stack: String(e.stack), own});
} }
"""
ASYNC_PROBE = "(async (f) => { try { await f(); return 'no error'; } catch (e) { return JSON.stringify({name: e.name, message: e.message, stack: String(e.stack)}); } })"


class Validation(Exception):
    pass


class SubValidation(Validation):
    pass


def boom_validation() -> None:
    raise Validation("price must be positive")


def boom_sub() -> None:
    raise SubValidation("sub says hi")


def boom_secret() -> None:
    raise ValueError(f"cannot open {SECRET}")


def boom_chained() -> None:
    try:
        raise ValueError(f"inner {SECRET}")
    except ValueError as inner:
        raise Validation("outer is fine") from inner


def boom_wrapping_allowed() -> None:
    try:
        raise Validation("allowed text")
    except Validation as inner:
        raise RuntimeError(f"wrapper leaks {SECRET}") from inner


def boom_noted() -> None:
    exc = Validation("with a note")
    exc.add_note(f"note {SECRET}")
    raise exc


FUNCS = {
    "validation": boom_validation,
    "sub": boom_sub,
    "secret": boom_secret,
    "chained": boom_chained,
    "wrapping": boom_wrapping_allowed,
    "noted": boom_noted,
}


def probe(rt: Any, name: str) -> dict[str, Any]:
    raw = rt.eval(f"({PROBE})({name})")
    return json.loads(raw)


def make(**options: Any) -> IsolatedRuntime:
    rt = IsolatedRuntime(RuntimeConfig(), **options)
    for name, func in FUNCS.items():
        rt.bind_function(name, func)
    return rt


def test_default_is_unchanged_every_message_redacted() -> None:
    with make() as rt:
        for name in FUNCS:
            assert probe(rt, name)["message"] == REDACTED


def test_class_allowlist_shows_only_those_and_their_subclasses() -> None:
    with make(expose_host_errors=Validation) as rt:
        assert probe(rt, "validation") == {
            **probe(rt, "validation"),
            "name": "Validation",
            "message": "price must be positive",
        }
        assert probe(rt, "sub")["message"] == "sub says hi"
        assert probe(rt, "secret")["message"] == REDACTED
        assert probe(rt, "secret")["name"] == "ValueError"  # the class name is kept


def test_collection_of_classes() -> None:
    with make(expose_host_errors=(KeyError, Validation)) as rt:
        assert probe(rt, "validation")["message"] == "price must be positive"
        assert probe(rt, "secret")["message"] == REDACTED
    with make(expose_host_errors=[Validation]) as rt:
        assert probe(rt, "validation")["message"] == "price must be positive"
    with make(expose_host_errors=()) as rt:  # empty: nothing exposed
        assert probe(rt, "validation")["message"] == REDACTED


def test_callable_hook() -> None:
    seen: list[BaseException] = []

    def hook(exc: BaseException) -> bool:
        seen.append(exc)
        return type(exc).__name__ == "Validation"

    with make(expose_host_errors=hook) as rt:
        assert probe(rt, "validation")["message"] == "price must be positive"
        assert probe(rt, "sub")["message"] == REDACTED  # the hook says exact name only
        assert probe(rt, "secret")["message"] == REDACTED
    assert seen


def test_hook_that_raises_or_answers_loosely_fails_closed() -> None:
    def broken(exc: BaseException) -> bool:
        raise RuntimeError("hook bug")

    with make(expose_host_errors=broken) as rt:
        assert probe(rt, "validation")["message"] == REDACTED
    with make(expose_host_errors=lambda exc: "yes") as rt:  # truthy, but not True
        assert probe(rt, "validation")["message"] == REDACTED


def test_nothing_chained_or_attached_reaches_the_guest() -> None:
    with make(expose_host_errors=Validation) as rt:
        for name in ("chained", "wrapping", "noted", "secret"):
            assert SECRET not in json.dumps(probe(rt, name)), name
        # the allowed outer exception shows its own text, not its cause's
        assert probe(rt, "chained")["message"] == "outer is fine"
        # an exception that merely wraps an allowed one stays redacted
        assert probe(rt, "wrapping")["message"] == REDACTED


def test_an_uncaught_exposed_error_ends_the_run_with_the_same_text() -> None:
    with make(expose_host_errors=Validation) as rt:
        with pytest.raises(Exception) as caught:
            rt.eval("chained()")
        assert "outer is fine" in str(caught.value)
        assert SECRET not in str(caught.value)
        with pytest.raises(Exception) as caught:
            rt.eval("secret()")
        assert SECRET not in str(caught.value)


def test_validation_of_the_option() -> None:
    with pytest.raises(TypeError, match="expose_host_errors"):
        IsolatedRuntime(RuntimeConfig(), expose_host_errors="Validation")
    with pytest.raises(TypeError, match="expose_host_errors"):
        IsolatedRuntime(RuntimeConfig(), expose_host_errors=[ValueError, "x"])
    with pytest.raises(TypeError, match="expose_host_errors"):
        IsolatedRuntime(RuntimeConfig(), expose_host_errors=[int])
    with pytest.raises(ValueError, match="no effect"):
        IsolatedRuntime(
            RuntimeConfig(), redact_host_errors=False, expose_host_errors=Validation
        )


def test_pools_checkout_can_set_it() -> None:
    with SandboxPool(size=1) as pool:
        with pool.checkout(expose_host_errors=Validation) as rt:
            rt.bind_function("validation", boom_validation)
            assert probe(rt, "validation")["message"] == "price must be positive"
        with pool.checkout() as rt:
            rt.bind_function("validation", boom_validation)
            assert probe(rt, "validation")["message"] == REDACTED


async def test_async_runtime() -> None:
    async with AsyncIsolatedRuntime(
        RuntimeConfig(), expose_host_errors=Validation
    ) as rt:
        for name, func in FUNCS.items():
            await rt.bind_function(name, func)
        got = json.loads(await rt.eval_async(f"({ASYNC_PROBE})(chained)"))
        assert got["message"] == "outer is fine"
        got = json.loads(await rt.eval_async(f"({ASYNC_PROBE})(secret)"))
        assert got["message"] == REDACTED
        assert SECRET not in json.dumps(got)
        got = json.loads(await rt.eval_async(f"({ASYNC_PROBE})(wrapping)"))
        assert got["message"] == REDACTED


CODE = "try { await t() } catch (e) { return [e.name, e.message] }"


def _agent_tools() -> dict[str, Any]:
    return {"validation": boom_validation, "secret": boom_secret}


def test_agent_sandbox_run_and_journal_replay() -> None:
    code = (
        "const out = []; for (const f of [validation, secret]) { try { await f() } "
        "catch (e) { out.push([e.name, e.message]) } } return out"
    )
    with AgentSandbox(_agent_tools(), expose_host_errors=Validation) as s:
        assert s.run(code) == [
            ["Validation", "price must be positive"],
            ["ValueError", REDACTED],
        ]
        blob = s.dump(KEY)
    # replay answers with the recorded text; it neither re-decides nor widens it
    with AgentSandbox.load(
        blob, KEY, _agent_tools(), expose_host_errors=Validation
    ) as loaded:
        assert loaded.run("return 1") == 1


def test_agent_sandbox_default_and_resume_error() -> None:
    with AgentSandbox(_agent_tools()) as s:
        step = s.start(CODE.replace("t()", "secret()"))
        assert s.resume(step, error=Validation("shown?")).result == [
            "Validation",
            REDACTED,
        ]
    with AgentSandbox(_agent_tools(), expose_host_errors=Validation) as s:
        step = s.start(CODE.replace("t()", "secret()"))
        assert s.resume(step, error=Validation("shown")).result == [
            "Validation",
            "shown",
        ]
        step = s.start(CODE.replace("t()", "secret()"))
        assert s.resume(step, error=ValueError(SECRET)).result == [
            "ValueError",
            REDACTED,
        ]


def test_agent_sandbox_journal_still_records_the_redact_flag_only() -> None:
    with AgentSandbox(_agent_tools(), expose_host_errors=Validation) as s:
        blob = s.dump(KEY)
    with pytest.raises(Exception, match="redact_host_errors"):
        AgentSandbox.load(blob, KEY, _agent_tools(), redact_host_errors=False)


async def test_async_agent_sandbox() -> None:
    code = (
        "const out = []; for (const f of [validation, secret]) { try { await f() } "
        "catch (e) { out.push([e.name, e.message]) } } return out"
    )
    async with AsyncAgentSandbox(_agent_tools(), expose_host_errors=Validation) as s:
        assert await s.run(code) == [
            ["Validation", "price must be positive"],
            ["ValueError", REDACTED],
        ]
    async with AsyncAgentSandbox(_agent_tools()) as s:
        assert await s.run(code) == [
            ["Validation", REDACTED],
            ["ValueError", REDACTED],
        ]
