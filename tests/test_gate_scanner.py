"""The static scanner under a `SourcePolicy`: it fails closed and runs in linear time.

Every shape in `BYPASSES` hid code from an earlier tokenizer-only scan (a regex, a template, an HTML
comment or a hashbang the tokenizer read differently from the engine, an escape it did not decode,
a key built at run time) and ran a forbidden function end to end. The default mode reads the whole
decoded text and must deny each one. The opt-in precise mode (`ignore_strings_and_comments=True`)
is best effort: which shapes it misses is pinned here and listed in the guide. The end-to-end runs
through `Pydeno` and `IsolatedRuntime` are in `tests/test_gate_hooks.py`.
"""

from __future__ import annotations

import functools
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
    # A backslash before a line break inside a comment is comment text: the comment still ends
    # at the break (a decoder that joined the lines read `xeval`).
    "comment-backslash-lf": '//x\\\neval("1")',
    "comment-backslash-crlf": '//x\\\r\neval("1")',
    "comment-backslash-u2028": '//x\\ eval("1")',
    "hashbang-backslash": '#!x\\\neval("1")',
    "html-comment-backslash": 'let q = 1 <!--x\\\neval("1")',
    # Inside `with (fn)`, a bare `constructor` is fn.constructor: the Function constructor.
    "with-constructor": 'with (()=>0) { constructor("return 6*7")() }',
    "with-constructor-statement": 'with (()=>0) { 0; constructor("return 6*7")() }',
    "with-extends-constructor": (
        'with (()=>0) { class A extends constructor("return 1") {}; new A() }'
    ),
    "with-constructor-paren-in-string": 'with (()=>0) { constructor("x//){")() }',
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
    # `constructor` inside `with`: precise mode reports only `.constructor(` and `["constructor"](`
    "with-constructor",
    "with-constructor-statement",
    "with-extends-constructor",
    "with-constructor-paren-in-string",
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


@pytest.mark.parametrize(
    "code",
    [
        '//x\\\nsecretTool("lc")',
        '//x\\\r\nsecretTool("crlf")',
        '//x\\ secretTool("u2028")',
        '#!x\\\nsecretTool("hb")',
        'let q = 1 <!--x\\\nsecretTool("html")',
    ],
    ids=["lf", "crlf", "u2028", "hashbang", "html"],
)
def test_a_backslash_ending_a_comment_line_hides_no_name(code: str) -> None:
    policy = SourcePolicy(forbidden_globals={"secretTool"})
    assert [f.rule for f in check_source(code, policy=policy).findings] == [
        "forbidden-global"
    ]


@pytest.mark.parametrize(
    ("code", "reported"),
    [
        ("class A { constructor(x) { this.x = x } }", False),
        ("class A {\n  constructor(a, b = [1, 2]) {\n  }\n}", False),
        ("class A { m() {}\n  // set up the state\n  constructor() {} }", False),
        ("const o = { constructor(a) { return a } }", False),
        # not provably a definition: reported (fail closed)
        ("class A {\n  constructor(x)\n  {}\n}", True),
        ('class A { constructor(s = ")") {} }', False),  # strings are matched
        ("class A { constructor(r = /[)]/) {} }", True),  # a regex: not provable
        ("class A { /* c */ constructor() {} }", True),
        ("constructor(1)", True),
        ("x.constructor(1)", True),
    ],
)
def test_only_a_provable_constructor_definition_is_allowed(
    code: str, reported: bool
) -> None:
    findings = check_source(code, policy=SourcePolicy(forbid_function=True)).findings
    assert bool(findings) == reported, code


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
_MIB = 1024 * 1024


@functools.lru_cache(maxsize=None)
def _baseline(mode: str) -> float:
    """Seconds to scan 1 MiB of ordinary code in this process, now: the bounds below are
    multiples of it (with a floor), so a loaded CI runner slows both sides alike. The inputs below
    used to take minutes to hours (quadratic); linear ones take a small multiple of this."""
    code = (_ORDINARY * (_MIB // len(_ORDINARY) + 1))[:_MIB]
    policy = {"no-policy": None, "default": POLICY, "precise": PRECISE}[mode]
    return min(_timed(code, policy) for _ in range(2))


def _bound(mode: str) -> float:
    # The default scan is a few compiled patterns: tight. The tokenizer modes are pure Python.
    return (
        max(2.0, 25 * _baseline(mode))
        if mode == "default"
        else max(4.0, 25 * _baseline(mode))
    )


def _timed(code: str, policy: SourcePolicy | None) -> float:
    started = time.perf_counter()
    if policy is None:
        check_source(code)
    else:
        check_source(code, policy=policy)
    return time.perf_counter() - started


MODES = {"no-policy": None, "default": POLICY, "precise": PRECISE}


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("/[" * (8 * 1024), id="unclosed-regex-classes-16k"),
        pytest.param("\\u{" * (700 * 1024), id="unclosed-brace-escapes-2m"),
        pytest.param("\\u{" + "0" * (2 * _MIB), id="one-long-brace-escape"),
        pytest.param("/" * _MIB, id="slashes-1m"),
        pytest.param(
            "//constructor(\n" * (_MIB // 15), id="comment-constructor-lines-1m"
        ),
        pytest.param("constructor(" * (_MIB // 12), id="constructor-calls-1m"),
        pytest.param("constructor(" + "(" * _MIB, id="constructor-deep-parens"),
        pytest.param("//x\\\n" * (_MIB // 5), id="comment-continuations-1m"),
        pytest.param("eval;" * (_MIB // 5), id="forbidden-words-1m"),
        pytest.param(_ORDINARY * (_MIB // len(_ORDINARY)), id="ordinary-1m"),
    ],
)
def test_the_scan_is_linear(shape: str, mode: str) -> None:
    policy = MODES[mode]
    if policy is not None:
        # Over the default cap the scan is refused at once (below); up to it, it must be fast.
        cap = policy.max_source_bytes
        assert cap is not None
        over = shape * (2 * cap // len(shape) + 1)
        shape = shape[:cap]
        started = time.perf_counter()
        assert check_source(over, policy=policy).findings[0].rule == "source-too-large"
        assert time.perf_counter() - started < 0.5
    took = _timed(shape, policy)
    assert took < _bound(mode), (took, _baseline(mode))


# ---------------------------------------------------------------------------
# the `constructor` rule's edges (default mode, forbid_function=True)
# ---------------------------------------------------------------------------

FUNCTION_ONLY = SourcePolicy(forbid_function=True)

#: (code, reported). "Reported" includes harmless definitions the cheap proof cannot accept: the
#: rule fails closed, so those are documented false positives, never the other way round.
CONSTRUCTOR_EDGES = {
    # definitions the rule accepts
    "class": ("class A { constructor(a) { this.a = a } }", False),
    "class-after-method": ("class A { m() {} constructor() {} }", False),
    "class-after-field": ("class A { x = 1; constructor() {} }", False),
    "class-after-comment-line": ("class A {\n  // init\n  constructor() {}\n}", False),
    "object-method": ("const o = { constructor() { return 1 } }", False),
    "string-in-parameters": ('class A { constructor(s = "(") {} }', False),
    # harmless, but not provable cheaply: reported (false positives)
    "static": ("class A { static constructor() {} }", True),
    "getter": ("const o = { get constructor() { return 1 } }", True),
    "setter": ("const o = { set constructor(v) {} }", True),
    "computed-string-member": ("class A { ['constructor']() {} }", True),
    "brace-on-next-line": ("class A {\n  constructor(a)\n  {\n  }\n}", True),
    "comment-line-before-brace": ("class A { constructor(a)\n// c\n{} }", True),
    "block-comment-before-paren": ("class A { constructor/*x*/(a) {} }", True),
    "template-in-parameters": ("class A { constructor(s = `x`) {} }", True),
    "regex-in-parameters": ("class A { constructor(r = /x/) {} }", True),
    "block-comment-before-name": ("class A { /* c */ constructor() {} }", True),
    # reaches (or may reach) the Function constructor: reported
    "object-value": ("const o = { constructor: Function }", True),
    "property": ("const F = (() => 0).constructor", True),
    "after-extends": ("class A extends constructor('return 1') {}", True),
    "after-new": ("new constructor('return 1')", True),
    "after-with": ("with (() => 0) constructor('return 1')()", True),
    "after-return": ("function f() { return constructor('return 1') }", True),
    "after-in": ("'x' in constructor", True),
    "after-of": ("for (const c of [constructor]) c('return 1')", True),
    "comment-then-call": ("with (() => 0) { constructor/*x*/('return 1')() }", True),
    "template-argument": ("with (() => 0) { constructor(`return 1`)() }", True),
    "at-start": ("constructor('return 1')", True),
    "at-end": ("x = constructor", True),
    "after-hashbang": ("#!x\nconstructor('return 1')", True),
    "destructuring": ("const { constructor: F } = () => 0", True),
}


@pytest.mark.parametrize("name", sorted(CONSTRUCTOR_EDGES))
def test_constructor_rule_edges(name: str) -> None:
    code, reported = CONSTRUCTOR_EDGES[name]
    rules = {f.rule for f in check_source(code, policy=FUNCTION_ONLY).findings}
    assert bool(rules) == reported, (name, rules)
    if reported:
        assert rules == {"forbidden-function-constructor"}


def test_a_constructor_name_built_at_run_time_is_not_seen() -> None:
    """A documented limit: no scan reads a name assembled at run time. `strict_eval=True` is
    what stops it (see the end-to-end test in tests/test_gate_hooks.py)."""
    code = '(() => 0)["constr" + "uctor"]("return 1")()'
    assert check_source(code, policy=FUNCTION_ONLY).ok


def test_many_allowed_class_constructors_scan_in_linear_time() -> None:
    unit = "class A { constructor(a, b = 'x') { this.a = a } }\n"
    code = unit * (_MIB // len(unit))
    took = _timed(code, FUNCTION_ONLY)
    assert check_source(code, policy=FUNCTION_ONLY).ok
    assert took < _bound("default"), (took, _baseline("default"))
