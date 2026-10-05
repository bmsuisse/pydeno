"""The static scanner under a `SourcePolicy`: it fails closed and runs in linear time.

Every shape in `BYPASSES` hid code from an earlier tokenizer-only scan (a regex, a template, an HTML
comment or a hashbang the tokenizer read differently from the engine, an escape it did not decode,
a key built at run time) and ran a forbidden function end to end. The default mode reads the whole
decoded text and must deny each one. The opt-in precise mode (`ignore_strings_and_comments=True`)
is best effort: which shapes it misses is pinned here and listed in the guide. The end-to-end runs
through `Pydeno` and `IsolatedRuntime` are in `tests/test_gate_hooks.py`.
"""

from __future__ import annotations

import time

import pytest

from pydeno import SourcePolicy, check_source

POLICY = SourcePolicy(
    forbid_eval=True,
    forbid_function=True,
    forbid_dynamic_import=True,
    forbid_webassembly=True,
    forbid_computed_global_access=True,
)
PRECISE = SourcePolicy(
    forbid_eval=True,
    forbid_function=True,
    forbid_dynamic_import=True,
    forbid_webassembly=True,
    forbid_computed_global_access=True,
    ignore_strings_and_comments=True,
)

#: name -> code that runs `eval` (or reaches the Function constructor) in V8.
BYPASSES = {
    "await-regex": 'await /`/\neval("1")\n// `',
    "yield-regex": 'function* g(){ yield /`/ }\neval("1")\n// `',
    "of-regex": 'for (const x of /`/.source) {}\neval("1")\n// `',
    "html-comment": 'x = 1 <!-- `\neval("1")\n// `',
    "html-comment-import": 'x = 1 <!-- `\nimport("data:text/javascript,1")\n// `',
    "hashbang": '#! `\neval("1")\n// `',
    "function-expression": 'x = function(){} / eval("1") / 2',
    "class-expression": 'x = class {} / eval("1") / 2',
    "await-object": 'await {} / eval("1") / 1',
    "spread-object": '[...{} / eval("1") / 1]',
    "arrow-then-regex": "f = x => {}\n/`/\neval(1)//`",
    "octal-escape": 'globalThis["\\145val"]("1")',
    "identity-escape": 'globalThis["\\ev\\al"]("1")',
    "line-continuation": 'globalThis["ev\\\nal"]("1")',
    "padded-brace-escape": '\\u{0000000065}val("1")',
    "parenthesised-key": 'globalThis[("eval")]("1")',
    "comma-key": 'globalThis[0, "eval"]("1")',
    "reflect": 'Reflect.get(globalThis, "eval")("1")',
    "computed-destructuring": 'const {["ev" + "al"]: e} = globalThis; e("1")',
    "constructor-destructuring": 'const {constructor: F} = function(){}; F("return 1")()',
    "constructor-after-comment": 'f.\n// note\nconstructor("return 1")()',
}

#: The shapes the precise mode does not catch (documented in docs/guides/gate.md).
PRECISE_MISSES = {
    # a regex, template or comment read differently from the engine
    "await-regex",
    "yield-regex",
    "of-regex",
    "html-comment",
    "html-comment-import",
    "hashbang",
    "function-expression",
    "class-expression",
    "await-object",
    "spread-object",
    # an escape the tokenizer does not decode
    "octal-escape",
    "padded-brace-escape",
    # a name reached without writing it
    "reflect",
    "computed-destructuring",
    "constructor-destructuring",
}


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_every_known_bypass_is_denied_by_default(name: str) -> None:
    result = check_source(BYPASSES[name], policy=POLICY)
    assert not result.ok, name
    assert all(f.template is not None for f in result.findings)


@pytest.mark.parametrize(
    "name",
    sorted(
        n
        for n, code in BYPASSES.items()
        if "eval" in code.replace("\\", "") or "\\145" in code
    ),
)
def test_eval_shapes_are_denied_with_only_forbid_eval(name: str) -> None:
    rules = {
        f.rule
        for f in check_source(
            BYPASSES[name], policy=SourcePolicy(forbid_eval=True)
        ).findings
    }
    assert rules == {"forbidden-eval"}, name


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_the_precise_mode_is_best_effort_as_documented(name: str) -> None:
    missed = check_source(BYPASSES[name], policy=PRECISE).ok
    assert missed == (name in PRECISE_MISSES), name


def test_a_hidden_forbidden_tool_is_denied_by_default() -> None:
    policy = SourcePolicy(forbidden_globals={"secretTool"})
    for code in (
        'await /`/\nsecretTool("x")\n// `',
        'let x = 1 <!-- `\nsecretTool("x")\n// `',
        'let y = function(){} / secretTool("x") / 2',
        'globalThis["secr\\145tTool"]()',
    ):
        assert [f.rule for f in check_source(code, policy=policy).findings] == [
            "forbidden-global"
        ], code


def test_findings_point_at_the_original_text() -> None:
    result = check_source('let a = 1;\n  globalThis["\\145val"]("1")', policy=POLICY)
    found = {(f.rule, f.line, f.column) for f in result.findings}
    assert ("forbidden-eval", 2, 15) in found


# ---------------------------------------------------------------------------
# linear time (H2): each must finish well inside the default 10 s gate timeout
# ---------------------------------------------------------------------------

_ORDINARY = (
    "function f(a,b){var c=a.map(x=>x*2).filter(y=>y%3===0);"
    "return c.length>0?`n=${c.length}`:/a+b/.test(b)?'s':\"d\"}"
    "const o={k:1,'q':[1,2,3],m(){return this.k/2}};// comment\n"
)
# Seconds, for a slow CI runner (a laptop needs 0.1-2.5 s); the quadratic behaviour these inputs
# used to trigger takes minutes to hours.
_LIMIT = 8.0


def _timed(code: str, policy: SourcePolicy | None) -> float:
    started = time.perf_counter()
    if policy is None:
        check_source(code)
    else:
        check_source(code, policy=policy)
    return time.perf_counter() - started


@pytest.mark.parametrize(
    "policy",
    [None, POLICY, PRECISE],
    ids=["no-policy", "default", "precise"],
)
@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("/[" * (8 * 1024), id="unclosed-regex-classes-16k"),
        pytest.param("\\u{" * (700 * 1024), id="unclosed-brace-escapes-2m"),
        pytest.param("\\u{" + "0" * (2 * 1024 * 1024), id="one-long-brace-escape"),
        pytest.param("/" * (1024 * 1024), id="slashes-1m"),
        pytest.param(_ORDINARY * (1024 * 1024 // len(_ORDINARY)), id="ordinary-1m"),
    ],
)
def test_the_scan_is_linear(shape: str, policy: SourcePolicy | None) -> None:
    if policy is not None:
        # Over the default cap the scan is refused at once (below); up to it, it must be fast.
        cap = policy.max_source_bytes
        assert cap is not None
        over = shape * (2 * cap // len(shape) + 1)
        shape = shape[:cap]
        started = time.perf_counter()
        assert check_source(over, policy=policy).findings[0].rule == "source-too-large"
        assert time.perf_counter() - started < 0.5
    assert _timed(shape, policy) < _LIMIT
