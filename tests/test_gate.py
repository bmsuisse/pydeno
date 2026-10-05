"""The gate's pure parts: `Verdict`, `GateContext`, the errors and their kinds, `gate_check` /
`async_gate_check`, the combinators, `SourcePolicy` and `static_gate`. No worker is started here
(the hooks are `tests/test_gate_hooks.py`)."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import gc
import hashlib
import pickle
import subprocess
import sys
import threading
import time
import warnings

import pytest

import pydeno
from pydeno import (
    POLICY_MESSAGES,
    GateContext,
    GateDenied,
    GateUnavailable,
    PydenoError,
    SourcePolicy,
    Verdict,
    all_of,
    any_of,
    async_gate_check,
    check_source,
    classify_error,
    gate_check,
    static_gate,
)
from pydeno._errors import KINDS
from pydeno._gate import MAX_GATE_SOURCE_BYTES, _hook

ALLOW = Verdict(True, "")


def allow(source: str, context: GateContext) -> Verdict:
    return ALLOW


def deny(source: str, context: GateContext) -> Verdict:
    return Verdict(False, "no", ("bad", "worse"))


class Recorder:
    """A sync gate that remembers every (source, context) and answers with `verdict`."""

    def __init__(self, verdict: Verdict = ALLOW) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, GateContext]] = []
        self.lock = threading.Lock()

    def __call__(self, source: str, context: GateContext) -> Verdict:
        with self.lock:
            self.calls.append((source, context))
        return self.verdict


# ---------------------------------------------------------------------------
# Verdict and GateContext
# ---------------------------------------------------------------------------


def test_verdict_fields_are_checked_and_frozen() -> None:
    v = Verdict(False, "why", ("a", "b"))
    assert (v.allow, v.reason, v.labels) == (False, "why", ("a", "b"))
    with pytest.raises(AttributeError):
        v.allow = True  # type: ignore[misc]
    for bad in (
        lambda: Verdict(1, "x"),  # type: ignore[arg-type]
        lambda: Verdict("yes", "x"),  # type: ignore[arg-type]
        lambda: Verdict(True, None),  # type: ignore[arg-type]
        lambda: Verdict(True, "x", ["a"]),  # type: ignore[arg-type]
        lambda: Verdict(True, "x", ("a", 1)),  # type: ignore[arg-type]
    ):
        with pytest.raises(TypeError):
            bad()


def test_context_for_source_counts_utf8_bytes_and_hashes_them() -> None:
    source = "const é = '日本'"
    ctx = GateContext.for_source(source, mode="feed_run", tools=["a", "b"])
    data = source.encode()
    assert ctx.source_length == len(data) > len(source)
    assert ctx.source_sha256 == hashlib.sha256(data).hexdigest()
    assert (ctx.language, ctx.mode, ctx.tools, ctx.specifier) == (
        "javascript",
        "feed_run",
        ("a", "b"),
        None,
    )
    with pytest.raises(AttributeError):
        ctx.mode = "eval"  # type: ignore[misc]
    # A lone surrogate is counted and hashed, never refused.
    assert GateContext.for_source("\ud800").source_length == 3


# ---------------------------------------------------------------------------
# errors and their kinds
# ---------------------------------------------------------------------------


def test_denied_and_unavailable_are_pydeno_errors_with_stable_kinds() -> None:
    denied = GateDenied("because", ("top", "other"))
    assert isinstance(denied, PydenoError)
    assert (denied.reason, denied.labels, denied.top_label) == (
        "because",
        ("top", "other"),
        "top",
    )
    assert GateDenied("x").top_label is None
    info = classify_error(denied)
    assert (info.kind, info.retryable, info.retry_with_larger_limits) == (
        "gate_denied",
        False,
        False,
    )
    unavailable = GateUnavailable("down")
    assert isinstance(unavailable, PydenoError) and unavailable.reason == "down"
    info = classify_error(unavailable)
    assert (info.kind, info.retryable) == ("gate_unavailable", True)
    assert KINDS["gate_denied"][:2] == (False, False)
    assert KINDS["gate_unavailable"][:2] == (True, False)
    # The cause of an unavailable gate is not what classifies it.
    try:
        raise GateUnavailable("the gate raised ValueError") from ValueError("x")
    except GateUnavailable as exc:
        assert classify_error(exc).kind == "gate_unavailable"


def test_gate_errors_pickle() -> None:
    d = pickle.loads(pickle.dumps(GateDenied("r", ("l",))))
    assert (type(d), d.reason, d.labels) == (GateDenied, "r", ("l",))
    u = pickle.loads(pickle.dumps(GateUnavailable("r")))
    assert (type(u), u.reason) == (GateUnavailable, "r")


def test_the_names_are_exported() -> None:
    for name in (
        "Gate",
        "GateContext",
        "GateDenied",
        "GateUnavailable",
        "StaticGate",
        "Verdict",
        "SourcePolicy",
        "POLICY_MESSAGES",
        "all_of",
        "any_of",
        "gate_check",
        "async_gate_check",
        "static_gate",
    ):
        assert name in pydeno.__all__
        assert getattr(pydeno, name) is not None


# ---------------------------------------------------------------------------
# gate_check: the shared rules
# ---------------------------------------------------------------------------


def test_an_allowing_gate_returns_its_verdict() -> None:
    rec = Recorder(Verdict(True, "fine", ("ok",)))
    assert gate_check(rec, "1 + 1") == Verdict(True, "fine", ("ok",))
    source, ctx = rec.calls[0]
    assert source == "1 + 1" and type(source) is str
    assert (ctx.mode, ctx.entry_point) == ("check", "gate_check")


def test_a_denying_verdict_raises_gate_denied() -> None:
    with pytest.raises(GateDenied) as info:
        gate_check(deny, "x")
    assert (info.value.reason, info.value.labels, info.value.top_label) == (
        "no",
        ("bad", "worse"),
        "bad",
    )


def test_a_gate_may_raise_its_own_denial_or_unavailability() -> None:
    def raises_denied(source: str, context: GateContext) -> Verdict:
        raise GateDenied("mine", ("x",))

    def raises_unavailable(source: str, context: GateContext) -> Verdict:
        raise GateUnavailable("retry shortly")

    with pytest.raises(GateDenied, match="mine"):
        gate_check(raises_denied, "x")
    with pytest.raises(GateUnavailable) as info:
        gate_check(raises_unavailable, "x")
    assert info.value.reason == "retry shortly"
    assert classify_error(info.value).retryable


@pytest.mark.parametrize(
    "exc",
    [ValueError("secret detail"), RuntimeError("x"), TimeoutError(), KeyError("k")],
    ids=["value", "runtime", "timeout", "key"],
)
def test_a_gate_that_raises_is_unavailable_and_keeps_its_message_out(
    exc: Exception,
) -> None:
    def broken(source: str, context: GateContext) -> Verdict:
        raise exc

    with pytest.raises(GateUnavailable) as info:
        gate_check(broken, "x")
    assert info.value.__cause__ is exc
    assert "secret detail" not in str(info.value)
    assert type(exc).__name__ in info.value.reason


class SneakyVerdict(Verdict):
    """A subclass whose `allow` reads True however it was built."""

    @property  # type: ignore[override]
    def allow(self) -> bool:  # type: ignore[override]
        return True


@pytest.mark.parametrize(
    "answer",
    [None, True, False, "allow", {"allow": True}, (True, "x"), 1],
    ids=["none", "true", "false", "str", "dict", "tuple", "int"],
)
def test_anything_but_a_verdict_is_unavailable(answer: object) -> None:
    with pytest.raises(GateUnavailable, match="not a Verdict"):
        gate_check(lambda s, c: answer, "x")


def test_a_verdict_subclass_or_a_tampered_verdict_is_unavailable() -> None:
    with pytest.raises(GateUnavailable):
        gate_check(lambda s, c: SneakyVerdict.__new__(SneakyVerdict), "x")
    tampered = Verdict(False, "x")
    object.__setattr__(tampered, "allow", 1)
    with pytest.raises(GateUnavailable, match="malformed"):
        gate_check(lambda s, c: tampered, "x")
    object.__setattr__(tampered, "allow", True)
    object.__setattr__(tampered, "labels", ["a"])
    with pytest.raises(GateUnavailable, match="malformed"):
        gate_check(lambda s, c: tampered, "x")


@pytest.mark.parametrize(
    "exc_type",
    [KeyboardInterrupt, SystemExit, asyncio.CancelledError, GeneratorExit],
    ids=["keyboard", "exit", "cancelled", "generator-exit"],
)
def test_cancellation_and_other_base_exceptions_propagate(exc_type: type) -> None:
    def stop(source: str, context: GateContext) -> Verdict:
        raise exc_type()

    with pytest.raises(exc_type):
        gate_check(stop, "x")


def test_an_async_gate_in_a_sync_check_is_unavailable_and_closed() -> None:
    async def agate(source: str, context: GateContext) -> Verdict:
        return ALLOW

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # "coroutine was never awaited" would fail here
        with pytest.raises(GateUnavailable, match="awaitable"):
            gate_check(agate, "x")
        gc.collect()


def test_a_slow_sync_gate_cannot_be_interrupted_but_its_late_verdict_is_discarded() -> (
    None
):
    def slow(source: str, context: GateContext) -> Verdict:
        time.sleep(0.3)
        return ALLOW

    started = time.monotonic()
    with pytest.raises(GateUnavailable, match="gate_timeout"):
        gate_check(slow, "x", timeout=0.05)
    assert time.monotonic() - started >= 0.3  # it ran to the end
    assert gate_check(slow, "x", timeout=None) == ALLOW


def test_an_oversized_source_is_denied_before_the_gate_runs() -> None:
    rec = Recorder()
    for source in (
        "x" * (MAX_GATE_SOURCE_BYTES + 1),
        "é" * (MAX_GATE_SOURCE_BYTES // 2 + 1),
    ):
        with pytest.raises(GateDenied) as info:
            gate_check(rec, source)
        assert info.value.top_label == "source-too-large"
        assert classify_error(info.value).kind == "gate_denied"
    assert rec.calls == []
    assert gate_check(rec, "x" * 1000) == ALLOW


class Shifty(str):
    """A str whose `__str__`, `__eq__`, `__hash__`, `encode` and slicing lie, and change."""

    flips = 0

    def __str__(self) -> str:
        Shifty.flips += 1
        return "evil()"

    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__

    def encode(self, *a: object, **k: object) -> bytes:  # type: ignore[override]
        return b"evil()"

    def __getitem__(self, key: object) -> str:  # type: ignore[override]
        return "evil()"


def test_a_str_subclass_is_normalised_to_its_exact_text() -> None:
    rec = Recorder()
    gate_check(rec, Shifty("const ok = 1"))
    source, ctx = rec.calls[0]
    assert type(source) is str
    assert str.__eq__(source, "const ok = 1")
    assert ctx.source_sha256 == hashlib.sha256(b"const ok = 1").hexdigest()
    assert Shifty.flips == 0  # its own methods were never consulted
    with pytest.raises(TypeError):
        gate_check(rec, b"bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        gate_check(rec, None)  # type: ignore[arg-type]


def test_a_context_passed_in_must_match_the_source() -> None:
    rec = Recorder()
    ctx = GateContext.for_source("a", mode="mine", tools=("t",))
    gate_check(rec, "a", ctx)
    assert rec.calls[0][1] is ctx
    with pytest.raises(ValueError, match="another source"):
        gate_check(rec, "b", ctx)
    with pytest.raises(TypeError):
        gate_check(rec, "a", {"mode": "x"})  # type: ignore[arg-type]


def test_the_gate_cannot_change_what_it_was_given() -> None:
    """A gate that mutates shared state still sees an immutable source and a frozen context."""
    shared: dict[str, object] = {}

    def meddler(source: str, context: GateContext) -> Verdict:
        shared["source"] = source
        with pytest.raises(AttributeError):
            context.tools = ("everything",)  # type: ignore[misc]
        return ALLOW

    gate_check(meddler, "a")
    assert shared["source"] == "a"


def test_hook_arguments_are_validated() -> None:
    assert _hook(None, 10.0, who="X", sync_only=True) is None
    with pytest.raises(TypeError):
        _hook("not callable", 10.0, who="X", sync_only=True)
    with pytest.raises(ValueError):
        _hook(allow, -1, who="X", sync_only=True)
    with pytest.raises(ValueError):
        _hook(None, 0, who="X", sync_only=True)  # validated even without a gate

    async def agate(source: str, context: GateContext) -> Verdict:
        return ALLOW

    class AsyncCallable:
        async def __call__(self, source: str, context: GateContext) -> Verdict:
            return ALLOW

    for async_gate in (agate, AsyncCallable(), all_of(allow, agate)):
        with pytest.raises(TypeError, match="async"):
            _hook(async_gate, 10.0, who="X", sync_only=True)
        assert _hook(async_gate, 10.0, who="X", sync_only=False) is not None


# ---------------------------------------------------------------------------
# async_gate_check
# ---------------------------------------------------------------------------


async def test_async_gates_are_awaited() -> None:
    async def agate(source: str, context: GateContext) -> Verdict:
        await asyncio.sleep(0)
        return Verdict(source == "ok", "checked", ("async",))

    assert (await async_gate_check(agate, "ok")).labels == ("async",)
    with pytest.raises(GateDenied, match="checked"):
        await async_gate_check(agate, "nope")
    assert await async_gate_check(allow, "sync gates work too") == ALLOW


async def test_an_async_gate_is_cancelled_at_its_timeout() -> None:
    cancelled = asyncio.Event()

    async def hangs(source: str, context: GateContext) -> Verdict:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ALLOW

    started = time.monotonic()
    with pytest.raises(GateUnavailable, match="gate_timeout"):
        await async_gate_check(hangs, "x", timeout=0.05)
    assert time.monotonic() - started < 5
    assert cancelled.is_set()


async def test_a_gate_that_swallows_its_cancellation_is_still_too_late() -> None:
    async def stubborn(source: str, context: GateContext) -> Verdict:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass  # refuses to stop, and answers "allow" after the deadline
        return ALLOW

    with pytest.raises(GateUnavailable, match="gate_timeout"):
        await async_gate_check(stubborn, "x", timeout=0.05)


async def test_cancelling_the_caller_propagates_and_is_not_a_verdict() -> None:
    started = asyncio.Event()

    async def hangs(source: str, context: GateContext) -> Verdict:
        started.set()
        await asyncio.sleep(30)
        return ALLOW

    task = asyncio.ensure_future(async_gate_check(hangs, "x"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_async_failures_are_unavailable() -> None:
    async def broken(source: str, context: GateContext) -> Verdict:
        raise ValueError("boom")

    async def wrong(source: str, context: GateContext) -> object:
        return "allow"

    with pytest.raises(GateUnavailable, match="ValueError"):
        await async_gate_check(broken, "x")
    with pytest.raises(GateUnavailable, match="not a Verdict"):
        await async_gate_check(wrong, "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# combinators
# ---------------------------------------------------------------------------


def test_all_of_stops_at_the_first_denial_and_merges_labels() -> None:
    first = Recorder(Verdict(True, "static ok", ("static",)))
    second = Recorder(Verdict(False, "classifier says no", ("injection", "static")))
    third = Recorder()
    with pytest.raises(GateDenied) as info:
        gate_check(all_of(first, second, third), "x")
    assert info.value.reason == "classifier says no"
    assert info.value.labels == ("injection", "static")
    assert info.value.top_label == "injection"
    assert third.calls == []  # not consulted after the denial


def test_all_of_allowing_merges_reasons_and_labels() -> None:
    v = gate_check(
        all_of(
            Recorder(Verdict(True, "a", ("x",))),
            Recorder(Verdict(True, "", ("y", "x"))),
            Recorder(Verdict(True, "c")),
        ),
        "x",
    )
    assert v == Verdict(True, "a; c", ("x", "y"))


def test_all_of_is_unavailable_when_any_gate_cannot_decide() -> None:
    def broken(source: str, context: GateContext) -> Verdict:
        raise RuntimeError

    with pytest.raises(GateUnavailable):
        gate_check(all_of(allow, broken, deny), "x")


def test_any_of_stops_at_the_first_allow() -> None:
    later = Recorder()
    v = gate_check(any_of(deny, Recorder(Verdict(True, "ok", ("fine",))), later), "x")
    assert v == Verdict(True, "ok", ("fine",))
    assert later.calls == []


def test_any_of_denying_merges_every_denial() -> None:
    with pytest.raises(GateDenied) as info:
        gate_check(
            any_of(deny, lambda s, c: Verdict(False, "also no", ("other", "bad"))), "x"
        )
    assert info.value.reason == "no\nalso no"
    assert info.value.labels == ("bad", "worse", "other")


def test_any_of_is_unavailable_only_when_nothing_allowed() -> None:
    def broken(source: str, context: GateContext) -> Verdict:
        raise RuntimeError

    assert gate_check(any_of(broken, allow), "x") == ALLOW
    with pytest.raises(GateUnavailable):
        gate_check(any_of(broken, deny), "x")


def test_combinators_need_gates() -> None:
    with pytest.raises(TypeError):
        all_of()
    with pytest.raises(TypeError):
        any_of(allow, "nope")  # type: ignore[arg-type]


async def test_a_combinator_over_an_async_gate_is_async() -> None:
    async def agate(source: str, context: GateContext) -> Verdict:
        return Verdict(False, "async no", ("a",))

    combined = all_of(static_gate(SourcePolicy()), agate)
    with pytest.raises(GateDenied, match="async no"):
        await async_gate_check(combined, "x")
    with pytest.raises(GateUnavailable, match="awaitable"):
        gate_check(combined, "x")
    nested = any_of(all_of(deny), any_of(agate, allow))
    assert await async_gate_check(nested, "x") == ALLOW


def test_concurrent_checks_with_one_sync_gate() -> None:
    rec = Recorder()
    gate = all_of(static_gate(SourcePolicy(forbid_eval=True)), rec)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(50):
                gate_check(gate, f"const v{i}_{j} = {j}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(rec.calls) == 400
    assert len({s for s, _ in rec.calls}) == 400


# ---------------------------------------------------------------------------
# SourcePolicy and static_gate
# ---------------------------------------------------------------------------

FULL = SourcePolicy(
    forbidden_identifiers={"secretTool"},
    forbidden_globals={"process", "require"},
    forbid_dynamic_import=True,
    forbid_eval=True,
    forbid_function=True,
    forbid_webassembly=True,
)


#: The opt-in, tokenizer-based mode (best effort; see the guide's list of known bypasses).
PRECISE = dataclasses.replace(FULL, ignore_strings_and_comments=True)
BOTH_MODES = pytest.mark.parametrize(
    "policy", [FULL, PRECISE], ids=["default", "precise"]
)


def rules(code: str, policy: SourcePolicy = FULL) -> list[str]:
    return [f.rule for f in check_source(code, policy=policy).findings]


@pytest.mark.parametrize(
    ("code", "default", "precise"),
    [
        ("eval('1')", ["forbidden-eval"], ["forbidden-eval"]),
        ("(0, eval)('1')", ["forbidden-eval"], ["forbidden-eval"]),
        ("globalThis.eval('1')", ["forbidden-eval"], ["forbidden-eval"]),
        (
            "setTimeout('alert(1)', 10)",
            ["forbidden-string-timer"],
            ["forbidden-string-timer"],
        ),
        (
            "setInterval(`x`, 10)",
            ["forbidden-string-timer"],
            ["forbidden-string-timer"],
        ),
        ("setTimeout(() => 1, 10)", [], []),
        (
            "new Function('return 1')",
            ["forbidden-function-constructor"],
            ["forbidden-function-constructor"],
        ),
        (
            "Function('return 1')()",
            ["forbidden-function-constructor"],
            ["forbidden-function-constructor"],
        ),
        (
            "(async () => {}).constructor('x')",
            ["forbidden-function-constructor"],
            ["forbidden-function-constructor"],
        ),
        (
            "x['constructor']('a')",
            ["forbidden-function-constructor"],
            ["forbidden-function-constructor"],
        ),
        # The default mode reports every `constructor` but a class's own method definition.
        ("x.constructor === Array", ["forbidden-function-constructor"], []),
        ("class A { constructor(x) { this.x = x } }", [], []),
        ("class A { m() {}\n  // set up\n  constructor() {} }", [], []),
        ("import('m')", ["forbidden-dynamic-import"], ["forbidden-dynamic-import"]),
        ("obj.import('m')", [], []),
        (
            "WebAssembly.instantiate(b)",
            ["forbidden-webassembly"],
            ["forbidden-webassembly"],
        ),
        ("process.env", ["forbidden-global"], ["forbidden-global"]),
        ("globalThis.process", ["forbidden-global"], ["forbidden-global"]),
        ("self['require']('fs')", ["forbidden-global"], ["forbidden-global"]),
        # A forbidden global's name is reported anywhere by default (it may be an alias's).
        ("obj.process", ["forbidden-global"], []),
        ("secretTool()", ["forbidden-identifier"], ["forbidden-identifier"]),
        ("tools.secretTool()", ["forbidden-identifier"], ["forbidden-identifier"]),
        ("tools['secretTool']()", ["forbidden-identifier"], ["forbidden-identifier"]),
        ("const x = 1 + 2", [], []),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_policy_rules(code: str, default: list[str], precise: list[str]) -> None:
    assert rules(code) == default
    assert rules(code, PRECISE) == precise


@pytest.mark.parametrize(
    ("code", "rule"),
    [
        ("// eval('1')\n1", "forbidden-eval"),
        ("/* new Function('x') */ 1", "forbidden-function-constructor"),
        ("'eval(1)' + \"x\"", "forbidden-eval"),
        ("/eval\\(/.test(s)", "forbidden-eval"),
        ("const s = `process.env ${1 + 1}`", "forbidden-global"),
    ],
    ids=["line-comment", "block-comment", "strings", "regex", "template-text"],
)
def test_strings_and_comments_count_by_default_and_not_in_precise_mode(
    code: str, rule: str
) -> None:
    """Failing closed: the default scan reads the whole text, so a name in a string or a comment
    is reported too. Only the opt-in precise mode skips them."""
    assert rule in rules(code)
    assert rules(code, PRECISE) == []


@pytest.mark.parametrize(
    "code",
    [
        "\\u0065val('1')",
        "\\u{65}val('1')",
        "ev\\u0061l('1')",
        "globalThis['\\x65val']('1')",
        "globalThis['\\u0065val']('1')",
        "globalThis[`eval`]('1')",
        "// a comment eval('1')",  # U+2028 ends a line comment
        "// a comment eval('1')",
        "/* ‮ } */ eval('1')",  # a bidi override inside a comment hides nothing
        "eval　('1')",  # any Unicode space is a space
        "`${eval('1')}`",
    ],
    ids=[
        "u-escape",
        "u-brace",
        "mid-escape",
        "hex-key",
        "u-key",
        "template-key",
        "u2028",
        "u2029",
        "bidi",
        "ideographic-space",
        "substitution",
    ],
)
@BOTH_MODES
def test_escapes_and_unicode_spaces_do_not_hide_names(
    code: str, policy: SourcePolicy
) -> None:
    assert "forbidden-eval" in rules(code, policy)


@pytest.mark.parametrize(
    "code",
    [
        "x = {} / eval('globalThis.hit = 1') / 1",  # `}` of an object literal, then division
        "var a = 1; a++ / eval('globalThis.hit = 1') / 2",  # a postfix operator, then division
        "var o = {in: 4}; o.in / eval('globalThis.hit = 1') / 1",  # a keyword as a property
        "var of = 1; of / eval('globalThis.hit = 1') / 1",  # a contextual keyword as a variable
        "if (true) /'/.test('x'); eval('globalThis.hit = 1') //'",  # a regex after `if (...)`
        "{}\n/'/; eval('globalThis.hit = 1') //'",  # a regex after a block
        "x = () => {}\n/'/.test(1); eval('globalThis.hit = 1') //'",  # after an arrow body
    ],
    ids=["object", "postfix", "keyword-property", "of", "control", "block", "arrow"],
)
@BOTH_MODES
def test_regex_or_division_cannot_hide_code_from_the_policy(
    code: str, policy: SourcePolicy
) -> None:
    """Each line runs `eval` in the engine; a scanner that took the `/` the other way would see
    a regex or a string where the engine sees code."""
    with pydeno.Runtime() as rt:
        rt.eval(code)
        assert rt.eval("hit") == 1
    assert "forbidden-eval" in rules(code, policy)


@BOTH_MODES
def test_names_the_engine_does_not_treat_as_eval_are_not_eval(
    policy: SourcePolicy,
) -> None:
    # Fullwidth letters and a zero-width joiner make different identifiers in JavaScript.
    assert rules("ｅｖａｌ('1')", policy) == []
    assert rules("ev‍al('1')", policy) == []


def test_policy_messages_are_the_documented_templates() -> None:
    expected = {
        "source-too-large": (
            "The code is {size} bytes, over the limit of {limit} bytes. Send a shorter program."
        ),
        "forbidden-identifier": "`{name}` is not allowed here. Rewrite the code without it.",
        "forbidden-global": "The global `{name}` is not allowed here. Rewrite the code without it.",
        "forbidden-dynamic-import": (
            "import(...) is not allowed here. Use only the functions you were given."
        ),
        "forbidden-eval": (
            "eval is not allowed here. Write the code directly instead of building it from "
            "strings."
        ),
        "forbidden-string-timer": (
            "{name} with a string argument compiles that string, which is not allowed here. "
            "Pass a function instead."
        ),
        "forbidden-function-constructor": (
            "The Function constructor is not allowed here. Write the code directly instead of "
            "building it from strings."
        ),
        "forbidden-webassembly": (
            "WebAssembly is not allowed here. Write the computation in JavaScript."
        ),
        "forbidden-computed-global-access": (
            "Looking up a global by a computed name is not allowed here. Use the name "
            "directly."
        ),
    }
    assert POLICY_MESSAGES == expected


def test_findings_carry_only_template_text() -> None:
    code = "process.exit(); secretTool(); eval('/etc/passwd'); setTimeout('x')"
    findings = check_source(code, policy=FULL).findings
    assert findings
    allowed = {
        POLICY_MESSAGES[f.rule].format(name=n, size=0, limit=0)
        for f in findings
        for n in ("process", "require", "secretTool", "setTimeout", "setInterval")
    }
    for f in findings:
        assert f.severity == "error"
        assert f.message in allowed
        assert "/etc" not in f.message and "0x" not in f.message


def test_max_source_bytes_is_checked_first() -> None:
    policy = SourcePolicy(max_source_bytes=10, forbid_eval=True)
    result = check_source("eval('a long program')", policy=policy)
    assert not result.ok
    assert [f.rule for f in result.findings] == ["source-too-large"]
    assert result.findings[0].message == POLICY_MESSAGES["source-too-large"].format(
        size=22, limit=10
    )
    assert check_source("é" * 6, policy=policy).findings[0].rule == "source-too-large"
    assert check_source("x" * 10, policy=policy).ok


def test_a_policy_caps_source_at_one_mib_by_default() -> None:
    assert SourcePolicy().max_source_bytes == 1024 * 1024
    big = "x" * (1024 * 1024 + 1)
    assert rules(big, SourcePolicy()) == ["source-too-large"]
    assert check_source(big, policy=SourcePolicy(max_source_bytes=None)).ok


def test_policy_fields_are_validated() -> None:
    with pytest.raises(TypeError):
        SourcePolicy(forbidden_identifiers="eval")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SourcePolicy(forbidden_globals={1})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SourcePolicy(forbid_eval="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SourcePolicy(ignore_strings_and_comments=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourcePolicy(max_source_bytes=0)
    with pytest.raises(ValueError):
        SourcePolicy(max_source_bytes=True)  # type: ignore[arg-type]
    assert SourcePolicy(
        forbidden_identifiers=["a", "a"]
    ).forbidden_identifiers == frozenset({"a"})
    with pytest.raises(TypeError):
        check_source("x", policy={"forbid_eval": True})  # type: ignore[arg-type]


def test_preflight_rules_are_off_under_a_policy_unless_asked_for() -> None:
    code = "fetch('x'); require('y')"
    assert not check_source(code).ok  # unchanged without a policy
    assert check_source(code, policy=SourcePolicy()).ok
    with_rules = check_source(code, policy=SourcePolicy(include_preflight_rules=True))
    assert {f.rule for f in with_rules.findings} == {"fetch", "require"}


def test_check_source_without_a_policy_is_unchanged() -> None:
    # Escapes are not decoded and Unicode spaces are not spaces without a policy, as before.
    assert check_source("\\u0066etch('x')").ok
    result = check_source("import fs from 'fs'\nfetch('x')\neval('1')")
    assert [(f.rule, f.line, f.column, f.severity) for f in result.findings] == [
        ("static-import", 1, 1, "error"),
        ("fetch", 2, 1, "error"),
        ("eval", 3, 1, "info"),
    ]


def test_static_gate_denies_with_findings_and_labels() -> None:
    gate = static_gate(FULL)
    assert gate_check(gate, "const a = [1, 2].map(x => x * 2)") == ALLOW
    with pytest.raises(GateDenied) as info:
        gate_check(gate, "eval('1')\nprocess.exit()\neval('2')")
    assert info.value.labels == ("forbidden-eval", "forbidden-global")
    assert info.value.top_label == "forbidden-eval"
    lines = info.value.reason.splitlines()
    assert lines[0] == "1:1 [forbidden-eval] " + POLICY_MESSAGES["forbidden-eval"]
    assert len(lines) == 3
    with pytest.raises(TypeError):
        static_gate({"forbid_eval": True})  # type: ignore[arg-type]


def test_static_gate_caps_the_listed_findings() -> None:
    with pytest.raises(GateDenied) as info:
        gate_check(static_gate(FULL), "eval(1);" * 30)
    lines = info.value.reason.splitlines()
    assert len(lines) == 21 and lines[-1] == "... and 10 more findings"


def test_static_gate_is_deterministic_pure_and_imports_nothing_when_called() -> None:
    code = r"""
import sys
from pydeno import SourcePolicy, static_gate, GateContext
gate = static_gate(SourcePolicy(forbid_eval=True, forbidden_globals={"process"}))
ctx = GateContext.for_source("eval(1)")
before = set(sys.modules)
answers = {repr(gate("eval(1)", ctx)) for _ in range(5)} | {repr(gate("1 + 1", ctx))}
after = set(sys.modules)
assert after == before, sorted(after - before)
print(len(answers))
"""
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "2"


# ---------------------------------------------------------------------------
# gate signatures: programming errors are TypeErrors, not outages
# ---------------------------------------------------------------------------


def test_a_one_argument_gate_is_called_with_the_source() -> None:
    seen: list[str] = []

    def clf(source):  # type: ignore[no-untyped-def]
        seen.append(source)
        return Verdict("bad" not in source, "one-arg", ("clf",))

    assert gate_check(clf, "good") == Verdict(True, "one-arg", ("clf",))
    with pytest.raises(GateDenied, match="one-arg"):
        gate_check(all_of(static_gate(SourcePolicy()), clf), "bad")
    assert seen == ["good", "bad"]
    assert _hook(clf, 10.0, who="X", sync_only=True) is not None


async def test_an_async_one_argument_gate_works() -> None:
    async def clf(source):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return Verdict(source == "ok", "async one-arg")

    assert (await async_gate_check(clf, "ok")).reason == "async one-arg"
    with pytest.raises(GateDenied):
        await async_gate_check(any_of(clf), "no")
    with pytest.raises(TypeError, match="async"):
        _hook(clf, 10.0, who="X", sync_only=True)  # still recognised as async
    assert _hook(clf, 10.0, who="X", sync_only=False) is not None


def test_optional_and_variadic_signatures_are_accepted() -> None:
    def defaults(source, context=None, extra=1):  # type: ignore[no-untyped-def]
        return ALLOW

    def variadic(*args):  # type: ignore[no-untyped-def]
        assert len(args) == 2
        return ALLOW

    class Method:
        def check(self, source, context):  # type: ignore[no-untyped-def]
            return ALLOW

    for gate in (defaults, variadic, Method().check, static_gate(SourcePolicy())):
        assert gate_check(gate, "x") == ALLOW
        assert _hook(gate, 10.0, who="X", sync_only=True) is not None


@pytest.mark.parametrize(
    "gate",
    [
        lambda a, b, c: ALLOW,
        lambda: ALLOW,
        lambda *, source, context: ALLOW,
        "not callable",
    ],
    ids=["three-args", "no-args", "keyword-only", "string"],
)
def test_an_incompatible_gate_is_a_type_error_everywhere(gate: object) -> None:
    with pytest.raises(TypeError):
        gate_check(gate, "x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        all_of(gate)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _hook(gate, 10.0, who="X", sync_only=False)


async def test_an_incompatible_gate_is_a_type_error_in_async_gate_check() -> None:
    with pytest.raises(TypeError):
        await async_gate_check(lambda a, b, c: ALLOW, "x")  # type: ignore[arg-type]


def test_a_type_error_raised_inside_the_gate_is_still_unavailable() -> None:
    def buggy(source, context):  # type: ignore[no-untyped-def]
        return len(source, context)  # a TypeError from the gate's own body

    with pytest.raises(GateUnavailable, match="TypeError"):
        gate_check(buggy, "x")


def test_a_partial_gate_is_inspected_through() -> None:
    gate = functools.partial(lambda verdict, source, context: verdict, ALLOW)
    assert gate_check(gate, "x") == ALLOW
    with pytest.raises(TypeError):
        gate_check(functools.partial(lambda a, b, c, d: ALLOW, 1), "x")


# ---------------------------------------------------------------------------
# model-facing text: the clean message without a location
# ---------------------------------------------------------------------------


def test_findings_expose_the_bare_text_and_template() -> None:
    policy = SourcePolicy(forbidden_globals={"process"}, forbid_eval=True)
    result = check_source("let a = 1;\nprocess.exit(eval('1'))", policy=policy)
    first, second = result.findings
    assert (first.rule, first.line, first.column) == ("forbidden-global", 2, 1)
    assert first.text == first.message
    assert first.text == POLICY_MESSAGES["forbidden-global"].format(name="process")
    assert first.template == POLICY_MESSAGES["forbidden-global"]
    assert not first.text.startswith("2:")  # no location prefix
    assert second.text == POLICY_MESSAGES["forbidden-eval"]
    # The usability rules have no template.
    assert check_source("fetch('x')").findings[0].template is None


def test_static_gate_labels_are_the_rules_and_check_gives_the_findings() -> None:
    gate = static_gate(SourcePolicy(forbid_eval=True, forbidden_globals={"process"}))
    source = "eval('1'); process.env"
    with pytest.raises(GateDenied) as info:
        gate_check(gate, source)
    assert info.value.top_label == "forbidden-eval"
    findings = gate.check(source).findings
    assert [f.rule for f in findings] == list(info.value.labels)
    assert [f.text for f in findings] == [
        POLICY_MESSAGES["forbidden-eval"],
        POLICY_MESSAGES["forbidden-global"].format(name="process"),
    ]


# ---------------------------------------------------------------------------
# computed access on a global object
# ---------------------------------------------------------------------------

COMPUTED = SourcePolicy(forbid_computed_global_access=True)
COMPUTED_PRECISE = SourcePolicy(
    forbid_computed_global_access=True, ignore_strings_and_comments=True
)
COMPUTED_MODES = pytest.mark.parametrize(
    "policy", [COMPUTED, COMPUTED_PRECISE], ids=["default", "precise"]
)


@pytest.mark.parametrize(
    "code",
    [
        "globalThis['ev' + 'al']('1')",
        "globalThis[k]",
        "this[k]",
        "self[name]()",
        "window[`${a}b`]",
        "global[x.y]",
        "globalThis?.[k]",
        "(() => {}).constructor[k]",
        "Function[k]",
        "globalThis[`a${''}`]",
    ],
    ids=[
        "concat",
        "variable",
        "this",
        "self",
        "template-substitution",
        "global",
        "optional",
        "constructor-chain",
        "function",
        "template-with-empty-substitution",
    ],
)
@COMPUTED_MODES
def test_computed_global_access_is_flagged(code: str, policy: SourcePolicy) -> None:
    findings = check_source(code, policy=policy).findings
    assert [f.rule for f in findings] == ["forbidden-computed-global-access"]
    assert findings[0].text == POLICY_MESSAGES["forbidden-computed-global-access"]


@pytest.mark.parametrize(
    "code",
    [
        "Reflect.get(globalThis, k)",
        "const {[k]: e} = globalThis",
        "Object.values(globalThis)",
        "const g = globalThis; g[k]",
        "globalThis['Math']",
        "f(this)",
    ],
    ids=["reflect", "destructure", "values", "alias", "literal-key", "this-argument"],
)
def test_the_default_mode_also_flags_globals_used_as_values(code: str) -> None:
    assert "forbidden-computed-global-access" in rules(code, COMPUTED)


@pytest.mark.parametrize(
    "code",
    [
        "obj[k]",
        "arr[i + 1]",
        "rows[0][col]",
        "x.self[k]",
        "globalThis.cache[k]",
        "globalThis.Math.max(1, 2)",
        "this.x = 1",
        "const [a, b] = pair",
    ],
    ids=[
        "object",
        "array",
        "nested",
        "property-named-self",
        "global-property",
        "dotted",
        "this-property",
        "destructuring",
    ],
)
@COMPUTED_MODES
def test_ordinary_bracket_access_is_not_flagged(
    code: str, policy: SourcePolicy
) -> None:
    assert check_source(code, policy=policy).findings == []


@pytest.mark.parametrize(
    "code",
    [
        "globalThis['Math']",
        "globalThis[`Math`]",
        "globalThis[0]",
        "// globalThis[k]\n1",
        "'globalThis[k]'",
    ],
    ids=["string-key", "template-key", "number", "comment", "string"],
)
def test_literal_keys_strings_and_comments_pass_only_in_precise_mode(code: str) -> None:
    assert rules(code, COMPUTED_PRECISE) == []
    assert rules(code, COMPUTED) == ["forbidden-computed-global-access"]


def test_computed_global_access_is_off_by_default() -> None:
    assert check_source("globalThis[k]", policy=SourcePolicy()).findings == []
    with pytest.raises(TypeError):
        SourcePolicy(forbid_computed_global_access="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the gate thread pool (sync gates called from async code)
# ---------------------------------------------------------------------------


def test_a_hung_sync_gate_does_not_keep_the_interpreter_alive() -> None:
    code = r"""
import asyncio, threading, time
from pydeno import GateUnavailable, Verdict, async_gate_check

def hangs(source, context):
    time.sleep(3600)
    return Verdict(True, "")

async def main():
    try:
        await async_gate_check(hangs, "1", timeout=0.2)
    except GateUnavailable:
        print("unavailable")
    threads = [t for t in threading.enumerate() if t.name.startswith("pydeno-gate")]
    print(len(threads), all(t.daemon for t in threads))

asyncio.run(main())
"""
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["unavailable", "1", "True"]
    assert (
        time.monotonic() - started < 30
    )  # exited without waiting for the gate's thread


def test_the_gate_thread_count_is_configurable() -> None:
    from pydeno import _gate

    assert pydeno.set_gate_threads is _gate.set_gate_threads
    before = _gate.gate_threads()
    try:
        _gate.set_gate_threads(2)
        assert _gate.gate_threads() == 2
        _gate.set_gate_threads(256)
        assert pydeno.gate_threads() == 256
        for bad in (0, -1, 257, True, 1.5):
            with pytest.raises((TypeError, ValueError)):
                _gate.set_gate_threads(bad)  # type: ignore[arg-type]
    finally:
        _gate.set_gate_threads(before)


async def test_hung_gates_starve_later_ones_into_unavailable_not_a_hang() -> None:
    from pydeno import _gate

    release = threading.Event()

    def hangs(source: str, context: GateContext) -> Verdict:
        release.wait(30)
        return ALLOW

    before = _gate.gate_threads()
    _gate.set_gate_threads(2)
    try:
        held = [
            asyncio.ensure_future(async_gate_check(hangs, "1", timeout=0.3))
            for _ in range(2)
        ]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(GateUnavailable, match="gate_timeout"):
            await async_gate_check(allow, "1", timeout=0.3)  # queued behind them
        assert time.monotonic() - started < 3
        for task in held:
            with pytest.raises(GateUnavailable):
                await task
        release.set()
        await asyncio.sleep(0.1)
        assert await async_gate_check(allow, "1", timeout=5) == ALLOW  # recovered
    finally:
        release.set()
        _gate.set_gate_threads(before)


async def test_a_burst_of_sync_gates_gets_threads_while_one_is_idle() -> None:
    """With one gate thread already idle, a burst of slow sync gates must each get a thread (up
    to the cap), not queue behind that one thread and time out."""
    from pydeno import _gate

    assert (
        await async_gate_check(allow, "warm", timeout=5) == ALLOW
    )  # leaves a thread idle
    await asyncio.sleep(0.05)

    def slow(source: str, context: GateContext) -> Verdict:
        time.sleep(0.5)
        return ALLOW

    before = _gate.gate_threads()
    _gate.set_gate_threads(16)
    try:
        started = time.monotonic()
        results = await asyncio.gather(
            *(async_gate_check(slow, f"{i}", timeout=4.0) for i in range(8)),
            return_exceptions=True,
        )
        assert results == [ALLOW] * 8, results
        assert time.monotonic() - started < 3.5  # in parallel, not 8 x 0.5 s in a row
        alive = [t for t in threading.enumerate() if t.name.startswith("pydeno-gate")]
        assert len(alive) >= 8
    finally:
        _gate.set_gate_threads(before)


def test_a_policy_message_table_is_read_only() -> None:
    from types import MappingProxyType

    assert isinstance(POLICY_MESSAGES, MappingProxyType)
    assert POLICY_MESSAGES["forbidden-eval"].startswith("eval is not allowed")
    assert "forbidden-eval" in POLICY_MESSAGES and len(POLICY_MESSAGES) == len(
        dict(POLICY_MESSAGES)
    )
    with pytest.raises(TypeError):
        POLICY_MESSAGES["forbidden-eval"] = "x"  # type: ignore[index]


def test_the_gate_type_alias_admits_the_one_argument_form() -> None:
    import typing

    params = [typing.get_args(t)[0] for t in typing.get_args(pydeno.Gate)]
    assert [str] in params
    assert [str, GateContext] in params
