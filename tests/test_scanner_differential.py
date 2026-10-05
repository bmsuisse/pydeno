"""The native source scanner (`_pydeno._scan_source`) against its specification, the Python
scanner kept in `pydeno._preflight_reference`: identical findings (rule, message, line, column,
severity, order) for every input, in every mode.

The corpus: every string in the scanner and gate tests (the bypass shapes from the review rounds
included), the gate docs' examples, the security metric's gate probes, generated and mutated
JavaScript-like text (escapes, regex / template / comment boundaries, Unicode, line terminators,
lone surrogates), hostile repeats up to 1 MiB, a fixed-seed loop of 100 000 inputs and a
Hypothesis search. Then: the native scan is linear and fast, and refuses what it must.
"""

from __future__ import annotations

import ast
import functools
import random
import re
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pydeno import SourcePolicy
from pydeno import _preflight
from pydeno import _preflight_reference as reference
from pydeno._pydeno import _scan_source

ROOT = Path(__file__).resolve().parent.parent
_MIB = 1024 * 1024

_ALL = dict(
    forbid_eval=True,
    forbid_function=True,
    forbid_dynamic_import=True,
    forbid_webassembly=True,
    forbid_computed_global_access=True,
)
_NAMES = dict(
    forbidden_identifiers={"secretTool", "fetch", "a", "été", "x\ud800"},
    forbidden_globals={"process", "eval", "constructor", "self", "globalThis"},
)

#: Every policy shape the scanner distinguishes, with no size cap: the cap is checked by the same
#: Python code before either scan (`test_the_size_cap_is_checked_first` covers it).
POLICIES: dict[str, SourcePolicy] = {
    "default": SourcePolicy(**_ALL, max_source_bytes=None),
    "default-names": SourcePolicy(**_ALL, **_NAMES, max_source_bytes=None),
    "default-rules": SourcePolicy(
        **_ALL, **_NAMES, include_preflight_rules=True, max_source_bytes=None
    ),
    "precise": SourcePolicy(
        **_ALL, ignore_strings_and_comments=True, max_source_bytes=None
    ),
    "precise-names": SourcePolicy(
        **_ALL, **_NAMES, ignore_strings_and_comments=True, max_source_bytes=None
    ),
    "precise-rules": SourcePolicy(
        **_ALL,
        **_NAMES,
        ignore_strings_and_comments=True,
        include_preflight_rules=True,
        max_source_bytes=None,
    ),
    "eval-only": SourcePolicy(forbid_eval=True, max_source_bytes=None),
    "function-only": SourcePolicy(forbid_function=True, max_source_bytes=None),
    "import-only": SourcePolicy(forbid_dynamic_import=True, max_source_bytes=None),
    "computed-only": SourcePolicy(
        forbid_computed_global_access=True, max_source_bytes=None
    ),
    "names-only": SourcePolicy(**_NAMES, max_source_bytes=None),
    "nothing": SourcePolicy(max_source_bytes=None),
}
#: `check_source` without a policy: its option combinations.
OPTIONS: dict[str, dict[str, bool]] = {
    "plain": {},
    "modules": {
        "allow_import": True,
        "allow_dynamic_import": True,
        "report_eval": False,
    },
    "static-only": {"allow_import": True},
}
MODES: list[tuple[SourcePolicy | None, dict[str, bool]]] = [
    (policy, {}) for policy in POLICIES.values()
] + [(None, options) for options in OPTIONS.values()]


def native(
    code: str, policy: SourcePolicy | None = None, **options: bool
) -> _preflight.PreflightResult:
    """The native scan's result, never the reference's (no fallback)."""
    result = _preflight._native_check(
        code,
        options.get("allow_import", False),
        options.get("allow_dynamic_import", False),
        options.get("report_eval", True),
        policy,
    )
    assert result is not None, "the native scanner declined this text"
    return result


def assert_same(code: str, policy: SourcePolicy | None = None, **options: bool) -> None:
    expected = reference.check_source(code, policy=policy, **options)
    got = native(code, policy, **options)
    if got != expected:  # a readable diff of the first difference
        pairs = zip(got.findings, expected.findings)
        first = next(((g, e) for g, e in pairs if g != e), None)
        raise AssertionError(
            f"{code!r} ({policy}, {options}): native {len(got.findings)} findings, "
            f"reference {len(expected.findings)}; first difference {first}"
        )


def assert_same_everywhere(code: str) -> None:
    for policy, options in MODES:
        assert_same(code, policy, **options)


def test_the_native_scanner_covers_this_python() -> None:
    assert _preflight._UNICODE is not None


# ---------------------------------------------------------------------------
# the corpus from the existing tests, docs and probes
# ---------------------------------------------------------------------------

_SOURCES = [
    "tests/test_gate_scanner.py",
    "tests/test_gate.py",
    "tests/test_gate_hooks.py",
    "tests/test_preflight.py",
    "scripts/autoresearch/metric_security.py",
]
_DOCS = [
    "docs/guides/gate.md",
    "docs/reference/gate.md",
    "docs/reference/preflight-and-status.md",
]


@functools.lru_cache(maxsize=None)
def corpus() -> tuple[str, ...]:
    """Every string literal in the gate and preflight tests and the security metric (the bypass
    shapes of every review round among them), every fenced block and inline code span of the gate
    docs, and every `"..." * n` shape from the tests at 1/64 of its size."""
    found: dict[str, None] = {}
    for name in _SOURCES:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found[node.value] = None
            elif (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Mult)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
            ):
                found[node.left.value * 64] = None
    for name in _DOCS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            found[block] = None
        for span in re.findall(r"`([^`\n]+)`", text):
            found[span] = None
    return tuple(found)


def test_the_corpus_holds_the_review_bypass_shapes() -> None:
    cases = set(corpus())
    for shape in (
        'globalThis["\\145val"]("1")',  # test_gate_scanner.BYPASSES
        'with (()=>0) { constructor("x//){")() }',
        '//x\\ eval("1")',
        "//constructor(\n" * 64,  # the linear-time shapes, scaled down
    ):
        assert shape in cases, shape


def test_every_corpus_case_gives_identical_findings() -> None:
    cases = corpus()
    assert len(cases) > 1000
    for code in cases:
        assert_same_everywhere(code)


# ---------------------------------------------------------------------------
# generated and mutated text
# ---------------------------------------------------------------------------

#: Pieces that steer a scanner into its edge cases.
FRAGMENTS = [
    *"eval Function constructor import export WebAssembly Reflect globalThis self window "
    "global this setTimeout setInterval require fetch process Deno XMLHttpRequest WebSocket "
    "child_process __proto__ secretTool a with if for while return typeof instanceof in of "
    "new delete void throw case do else await yield class extends static get set meta "
    "x y $ _ 0 1 7 9 0x1F 1e3 .5".split(),
    *"( ) [ ] { } ${ ` ' \" / // /* */ <!-- --> #! . ?. ?.5 ... ? : ; , = => + ++ - -- ! ~ * % "
    "& | ^ < >".split(),
    "\\",
    "\\u",
    "\\u{",
    "\\u{0",
    "}",
    "\\u0065",
    "\\u{65}",
    "\\u{0000000065}",
    "\\u{110000}",
    "\\u{1F600}",
    "\\uD800",
    "\\udc00",
    "\\x65",
    "\\x",
    "\\0",
    "\\1",
    "\\145",
    "\\377",
    "\\400",
    "\\7",
    "\\8",
    "\\e",
    "\\n",
    "\\\n",
    "\\\r\n",
    "\\\r",
    "\\ ",
    "\\ ",
    "u",
    "{",
    "0065",
    "D800",
    "65",
    " ",
    "  ",
    "\t",
    "\n",
    "\r",
    "\r\n",
    " ",
    " ",
    "\xa0",
    "﻿",
    "　",
    "\v",
    "\f",
    "\x1c",
    "\x85",
    "​",
    "é",
    "été",
    "ß",
    "١",
    "²",
    "Ⅻ",
    "\U0001d7d8",
    "ǅ",
    "̀",
    "℘",
    "℮",
    "゛",
    "\ud800",
    "\udfff",
    "x\ud800",
    "\U0001f600",
    "ā",
    "一",
    "０",
]
_SEEDS_FALLBACK = ["eval(1)", "class A { constructor() {} }"]


def _random_text(rng: random.Random, pieces: int) -> str:
    out = []
    for _ in range(pieces):
        if rng.random() < 0.15:
            out.append(
                chr(rng.randrange(0x110000))
            )  # any code point, a lone surrogate too
        else:
            out.append(rng.choice(FRAGMENTS))
    return "".join(out)


def _mutated(rng: random.Random, seed: str) -> str:
    text = seed
    for _ in range(rng.randint(1, 4)):
        at = rng.randint(0, len(text))
        op = rng.random()
        if op < 0.45:
            text = text[:at] + rng.choice(FRAGMENTS) + text[at:]
        elif op < 0.65:
            text = text[:at] + text[at + rng.randint(1, 4) :]
        elif op < 0.8 and text:
            end = min(len(text), at + rng.randint(1, 12))
            text = text[:end] + text[at:end] * rng.randint(1, 3) + text[end:]
        else:
            text = text[:at] + chr(rng.randrange(0x110000)) + text[at:]
    return text


def test_a_fixed_seed_fuzz_of_100_000_inputs() -> None:
    rng = random.Random(107)
    seeds = [c for c in corpus() if len(c) < 400] or _SEEDS_FALLBACK
    modes = MODES
    for i in range(100_000):
        if i % 2:
            code = _mutated(rng, rng.choice(seeds))
        else:
            code = _random_text(rng, rng.randint(0, 40))
        policy, options = modes[i % len(modes)]
        assert_same(code, policy, **options)


_texts = st.lists(
    st.one_of(
        st.sampled_from(FRAGMENTS),
        st.integers(0, 0x10FFFF).map(chr),
        st.text(max_size=4),
    ),
    max_size=60,
).map("".join)


@settings(
    max_examples=1500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(code=_texts, mode=st.integers(0, len(MODES) - 1))
def test_hypothesis_finds_no_difference(code: str, mode: int) -> None:
    policy, options = MODES[mode]
    assert_same(code, policy, **options)


@settings(max_examples=300, deadline=None)
@given(
    names=st.sets(
        st.text(
            st.characters(codec=None, exclude_categories=()), min_size=1, max_size=6
        ),
        max_size=4,
    ),
    code=_texts,
)
def test_any_forbidden_name_gives_identical_findings(
    names: set[str], code: str
) -> None:
    for precise in (False, True):
        policy = SourcePolicy(
            forbidden_identifiers=names,
            forbidden_globals=names,
            ignore_strings_and_comments=precise,
            max_source_bytes=None,
        )
        assert_same(code + " " + " ".join(names), policy)


def test_lone_surrogates_read_as_in_python() -> None:
    """A Python `str` can hold a lone surrogate (a Rust `String` cannot): the native scanner reads
    code points, so offsets, words and escapes come out as in Python."""
    for code in [
        "\ud800eval(1)",
        "eval\ud800(1)",
        "x\ud800 = 1; secretTool()",
        "\udc00\n\ud800 globalThis[k]",
        "'\\uD800' + eval",
        'globalThis["\\uD83D\\uDE00"]',
        "😀 eval",
        "a\ud800",
    ]:
        assert_same_everywhere(code)


def test_every_unicode_class_boundary_reads_as_in_python() -> None:
    """Probe each code point where a class this Python computes changes (and a random sample):
    `\\w` and `\\s` in the default mode, identifier, digit and space in the precise one."""
    rng = random.Random(13)
    edges: set[int] = set()
    prev = None
    for cp in range(0x80, 0x110000):
        ch = chr(cp)
        key = (ch.isalnum(), ch.isspace(), ch.isdigit(), ch.isidentifier())
        if key != prev:
            edges.update((cp - 1, cp))
            prev = key
    edges.update(rng.randrange(0x80, 0x110000) for _ in range(3000))
    default = SourcePolicy(
        forbidden_identifiers={"a"}, forbid_eval=True, max_source_bytes=None
    )
    precise = SourcePolicy(
        forbidden_identifiers={"a"},
        forbid_eval=True,
        ignore_strings_and_comments=True,
        max_source_bytes=None,
    )
    chars = [chr(cp) for cp in sorted(edges)]
    for k in range(0, len(chars), 64):
        chunk = chars[k : k + 64]
        code = "\n".join(
            f"a{c} {c}a setTimeout{c}('x') {c}\\u0061 x{c}/eval/ {c}" for c in chunk
        )
        assert_same(code, default)
        assert_same(code, precise)
        assert_same(code)


# ---------------------------------------------------------------------------
# large and hostile inputs (up to 1 MiB)
# ---------------------------------------------------------------------------

_ORDINARY = (
    "function f(a,b){var c=a.map(x=>x*2).filter(y=>y%3===0);"
    "return c.length>0?`n=${c.length}`:/a+b/.test(b)?'s':\"d\"}"
    "const o={k:1,'q':[1,2,3],m(){return this.k/2}};// comment\n"
)
HOSTILE = {
    "unclosed-regex-classes": "/[",
    "unclosed-brace-escapes": "\\u{",
    "slashes": "/",
    "comment-constructor-lines": "//constructor(\n",
    "constructor-calls": "constructor(",
    "comment-continuations": "//x\\\n",
    "forbidden-words": "eval;",
    "nested-parens": "(",
    "nested-templates": "`${",
    "nested-braces": "{",
    "backslashes": "\\",
    "octal-runs": "\\1\\12\\123",
    "class-constructors": "class A { constructor(a, b = 'x') { this.a = a } }\n",
    "ordinary": _ORDINARY,
}


def _sized(unit: str, size: int) -> str:
    return (unit * (size // len(unit) + 1))[:size]


@pytest.mark.parametrize("shape", sorted(HOSTILE))
def test_hostile_repeats_give_identical_findings(shape: str) -> None:
    code = _sized(HOSTILE[shape], 256 * 1024)
    for name in ("default-rules", "precise-rules", "function-only"):
        assert_same(code, POLICIES[name])
    assert_same(code)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("\\u{" + "0" * _MIB, id="one-long-brace-escape"),
        pytest.param("constructor(" + "(" * _MIB, id="constructor-deep-parens"),
        pytest.param(_sized(_ORDINARY, _MIB), id="ordinary"),
        pytest.param(_sized("eval;", _MIB), id="forbidden-words"),
    ],
)
def test_one_mib_inputs_give_identical_findings(code: str) -> None:
    assert_same(code, POLICIES["default-rules"])
    assert_same(code, POLICIES["precise"])
    assert_same(code)


def test_a_random_mib_gives_identical_findings() -> None:
    rng = random.Random(1)
    code = _random_text(rng, 380_000)[:_MIB]
    assert len(code) == _MIB
    assert_same(code, POLICIES["default-names"])
    assert_same(code, POLICIES["precise-rules"])


# ---------------------------------------------------------------------------
# the wiring: caps, fallbacks, no behaviour change
# ---------------------------------------------------------------------------


def test_check_source_uses_the_native_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the reference ran")

    monkeypatch.setattr(reference, "check_source", refuse)
    policy = SourcePolicy(forbid_eval=True)
    assert [
        f.rule for f in _preflight.check_source("eval(1)", policy=policy).findings
    ] == ["forbidden-eval"]
    assert _preflight.check_source("fetch(1)").findings[0].rule == "fetch"


def test_the_size_cap_is_checked_first() -> None:
    for policy in (SourcePolicy(), SourcePolicy(max_source_bytes=10)):
        for code in ("x" * (_MIB + 1), "é" * 6, "\ud800" * 4, "eval(1)"):
            assert _preflight.check_source(
                code, policy=policy
            ) == reference.check_source(code, policy=policy)


def test_a_text_over_16_mib_falls_back_to_the_reference() -> None:
    """The native scanner takes at most 16 MiB of UTF-8 (the gate's own cap). A longer text can
    reach `check_source` only without a policy or with ``max_source_bytes=None``; it is scanned by
    the reference implementation, exactly as before."""
    over = "x" * (16 * _MIB - 3) + "éé"  # 16 MiB + 1 byte in 16 MiB - 1 characters
    with pytest.raises(ValueError, match="too large"):
        _scan_source(over, _preflight._UNICODE)
    assert _preflight._native_check(over, False, False, True, None) is None
    assert _preflight.check_source(over + " fetch(1)").findings[0].rule == "fetch"


def test_the_native_function_validates_its_input() -> None:
    with pytest.raises(ValueError, match="Unicode version"):
        _scan_source("x", "0.0.0")
    with pytest.raises(TypeError):
        _scan_source(b"x", _preflight._UNICODE)  # type: ignore[arg-type]


def test_a_str_subclass_is_scanned_as_its_text() -> None:
    class Sneaky(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:  # type: ignore[override]
            return b""

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter("")

    code = Sneaky("eval('\ud800')")
    result = _preflight.check_source(code, policy=SourcePolicy(forbid_eval=True))
    assert [f.rule for f in result.findings] == ["forbidden-eval"]


# ---------------------------------------------------------------------------
# time: linear, and fast
# ---------------------------------------------------------------------------


def _raw_scan(code: str, policy: SourcePolicy | None) -> float:
    spec = None
    if policy is not None:
        spec = (
            list(policy.forbidden_identifiers),
            list(policy.forbidden_globals),
            _preflight._P_EVAL
            | _preflight._P_FUNCTION
            | _preflight._P_DYNAMIC_IMPORT
            | _preflight._P_WEBASSEMBLY
            | _preflight._P_COMPUTED
            | _preflight._P_PREFLIGHT_RULES * policy.include_preflight_rules
            | _preflight._P_PRECISE * policy.ignore_strings_and_comments,
        )
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        _scan_source(code, _preflight._UNICODE, spec)
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize("mode", ["default", "precise-rules", "plain"])
@pytest.mark.parametrize("shape", sorted(HOSTILE))
def test_the_native_scan_is_linear(shape: str, mode: str) -> None:
    """16 times the text takes at most a small multiple of 16 times as long (a quadratic scan
    would take 256 times), measured against itself in this process."""
    policy = POLICIES.get(mode)
    small = _raw_scan(_sized(HOSTILE[shape], 64 * 1024), policy)
    big = _raw_scan(_sized(HOSTILE[shape], _MIB), policy)
    assert big < 16 * 4 * small + 0.02, (small, big)


@pytest.mark.release_performance
def test_one_mib_of_ordinary_code_scans_well_under_100_ms() -> None:
    code = _sized(_ORDINARY, _MIB)
    for mode in ("default", "precise-rules", "plain"):
        assert _raw_scan(code, POLICIES.get(mode)) < 0.1, mode


def test_html_close_comment_dynamic_import_matches_reference() -> None:
    # https://portswigger.net/research/attacking-and-defending-javascript-sandboxes
    code = "import\n-->\n('loaded:html-close').then(m => m.value)"
    assert_same_everywhere(code)
    result = native(code, SourcePolicy(forbid_dynamic_import=True))
    assert {finding.rule for finding in result.findings} == {"forbidden-dynamic-import"}
