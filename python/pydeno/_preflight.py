"""A static source pre-check: fast, readable rejections of code that cannot work in the sandbox.

**This is a usability aid. It is NEVER a security boundary.** The sandbox does not depend on it and
nothing here is consulted by `Runtime`, `IsolatedRuntime` or `AgentSandbox`. A guest can evade any
static check (`globalThis['req' + 'uire']`, `(0, eval)('...')`, a string built at run time, code
fetched later), and `check_source` does not try to stop that: it only reads the text it is given.
What stops a guest from reaching `require`, `fetch`, `process`, a socket or a file is that none of
them exist in the isolate, and that the worker process has no authority (`docs/guides/advanced/isolation.md`).

What it is for: an LLM-written snippet that starts with `import fs from "fs"` or calls `fetch(...)`
will fail inside the isolate with a `ReferenceError` or a syntax error. Catching the common cases
before any runtime starts gives the author (or the model) a precise line and column and a sentence
that says what to do instead.

The scanner is a small tokenizer that understands strings, template literals (including the code
inside `${...}`), comments and regular-expression literals, so a word inside any of those is not a
finding. It is not a JavaScript parser: it does not know scopes, so a local variable called
`process` is reported like the global one.

Rules and severities:

- error: `static-import`, `static-export` (unless `allow_import`), `dynamic-import` (unless
  `allow_dynamic_import`), `require`, `fetch`, `xhr`, `websocket`, `process`, `deno`,
  `child-process`, `global-alias` (`globalThis.fetch`, `globalThis["require"]`)
- warning: `proto` (`__proto__`), `constructor-constructor`
- info: `eval` (`eval(` and `new Function(`; off with `report_eval=False`)

`ok` is true when there is no finding of severity "error"; warnings and notes are advice.

**A `SourcePolicy`** (``check_source(code, policy=...)``) replaces those rules with the host's own:
forbidden identifiers and globals, flags against `import()`, `eval`, the `Function` constructor
and `WebAssembly`, and a size cap (1 MiB by default). By default that scan fails closed: it
decodes every escape wherever it appears and reads the whole text, comments and strings included,
so it does not depend on telling a regex from a division (`_scan_text`); the opt-in precise mode
(``ignore_strings_and_comments=True``) uses the tokenizer below and has known bypasses. Both run in
linear time. It is still a heuristic (it cannot see a name built at run time), so it errs towards
reporting: a local variable, an object key or a word in a comment with a forbidden name is a
finding. Every policy finding's message is a fixed template from `POLICY_MESSAGES`, filled only
with host-chosen names and numbers, never with text from the code: that wording is a public
contract.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = [
    "POLICY_MESSAGES",
    "Finding",
    "PreflightResult",
    "SourcePolicy",
    "check_source",
]


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    line: int  # 1-based
    column: int  # 1-based
    severity: str = "error"  # "error" | "warning" | "info"

    @property
    def text(self) -> str:
        """The bare message, without a location: what to show the author (or a model) as the
        next step. For a `SourcePolicy` finding it is ``template`` filled in with host-chosen
        values only."""
        return self.message

    @property
    def template(self) -> str | None:
        """``POLICY_MESSAGES[rule]`` for a `SourcePolicy` finding, else None."""
        return POLICY_MESSAGES.get(self.rule)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def format(self) -> str:
        """One line per finding, `line:column severity [rule] message`."""
        return "\n".join(
            f"{f.line}:{f.column} {f.severity} [{f.rule}] {f.message}"
            for f in self.findings
        )


#: The message of every `SourcePolicy` rule. Public contract: shown to the author of the code
#: (often a model) as the next step, so the wording only changes in a documented release. The
#: placeholders are host-chosen (a name from the policy, `setTimeout`/`setInterval`, numbers).
_POLICY_MESSAGES: dict[str, str] = {
    "source-too-large": (
        "The code is {size} bytes, over the limit of {limit} bytes. Send a shorter program."
    ),
    "forbidden-identifier": "`{name}` is not allowed here. Rewrite the code without it.",
    "forbidden-global": "The global `{name}` is not allowed here. Rewrite the code without it.",
    "forbidden-dynamic-import": (
        "import(...) is not allowed here. Use only the functions you were given."
    ),
    "forbidden-eval": (
        "eval is not allowed here. Write the code directly instead of building it from strings."
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
        "Looking up a global by a computed name is not allowed here. Use the name directly."
    ),
}
#: Read-only: the templates are a public contract, not a setting.
POLICY_MESSAGES: MappingProxyType[str, str] = MappingProxyType(_POLICY_MESSAGES)


_EXACT_SIZE_UP_TO = 16 * 1024 * 1024
#: `SourcePolicy.max_source_bytes`' default.
DEFAULT_POLICY_MAX_SOURCE_BYTES = 1024 * 1024


def _names(field_name: str, value: object) -> frozenset[str]:
    if isinstance(value, str):
        raise TypeError(
            f"SourcePolicy.{field_name} takes a collection of names, not a str"
        )
    try:
        names = frozenset(value)  # type: ignore[call-overload]
    except TypeError:
        raise TypeError(
            f"SourcePolicy.{field_name} takes a collection of names"
        ) from None
    if not all(isinstance(n, str) and n for n in names):
        raise TypeError(f"SourcePolicy.{field_name} takes non-empty str names")
    return names


@dataclass(frozen=True)
class SourcePolicy:
    """What `check_source(code, policy=...)` and `static_gate(policy)` refuse.

    **Two modes.** By default the scan fails closed: it decodes every escape (``\\u``,
    ``\\u{...}``, ``\\x``, legacy octal, identity escapes, line continuations) wherever it
    appears and reads the *whole* text, comments, strings, templates and regular expressions
    included, so a name is found however the code hides it from a tokenizer; it reports names
    in strings and comments too. ``ignore_strings_and_comments=True`` selects the precise mode:
    a tokenizer that skips strings and comments, with fewer false positives but known bypasses
    (see ``docs/guides/gate.md``). Neither is a parser; both are heuristics.

    Args:
        forbidden_identifiers: Names refused wherever they appear (as an identifier, a property
            ``x.name`` or a string key ``x["name"]``; by default in any text).
        forbidden_globals: Names refused as globals. By default any occurrence (an alias such as
            ``g.name`` may be the global); in precise mode only bare or on a global object
            (``globalThis.name``, ``self["name"]``).
        forbid_dynamic_import: Refuse ``import(...)`` (``import`` followed by anything but what
            a static ``import`` or ``import.meta`` starts with).
        forbid_eval: Refuse ``eval`` (called or merely named, as in ``(0, eval)``) and
            ``setTimeout`` / ``setInterval`` called with a string literal.
        forbid_function: Refuse the ``Function`` constructor: the name ``Function``, and
            ``constructor`` anywhere but a provable ``constructor(...) {`` method definition
            (see `_method_definition`; inside ``with`` a bare ``constructor`` is Function).
            Precise mode: ``.constructor(...)`` and ``["constructor"](...)`` only.
        forbid_webassembly: Refuse ``WebAssembly``.
        forbid_computed_global_access: Refuse reaching a global by a computed name. By default:
            ``globalThis``, ``self``, ``window``, ``global`` or ``this`` used other than as
            ``name.property`` (``globalThis[k]``, ``= globalThis``, ``f(this)``, ``...self``),
            ``Reflect``, and ``constructor[`` / ``Function[``. Precise mode: only bracket access
            with a non-literal key. Best effort either way (an alias made elsewhere is not
            followed), and ``this`` in methods is reported too. Off by default.
        max_source_bytes: Refuse code longer than this many UTF-8 bytes (checked first; nothing
            else is scanned then). Default 1 MiB, so a scan stays well inside a gate's default
            timeout; ``None`` removes the cap.
        include_preflight_rules: Also apply `check_source`'s usability rules (``require``,
            ``fetch``, static ``import``, ...), which are off under a policy by default.
        ignore_strings_and_comments: The precise, tokenizer-based mode (opt-in, best effort).

    Heuristic, never the boundary: pair it with ``strict_eval=True`` and the isolated worker.
    """

    forbidden_identifiers: frozenset[str] = frozenset()
    forbidden_globals: frozenset[str] = frozenset()
    forbid_dynamic_import: bool = False
    forbid_eval: bool = False
    forbid_function: bool = False
    forbid_webassembly: bool = False
    max_source_bytes: int | None = DEFAULT_POLICY_MAX_SOURCE_BYTES
    include_preflight_rules: bool = False
    forbid_computed_global_access: bool = False
    ignore_strings_and_comments: bool = False

    def __post_init__(self) -> None:
        for name in ("forbidden_identifiers", "forbidden_globals"):
            object.__setattr__(self, name, _names(name, getattr(self, name)))
        for name in (
            "forbid_dynamic_import",
            "forbid_eval",
            "forbid_function",
            "forbid_webassembly",
            "include_preflight_rules",
            "forbid_computed_global_access",
            "ignore_strings_and_comments",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"SourcePolicy.{name} must be a bool")
        limit = self.max_source_bytes
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError(
                "SourcePolicy.max_source_bytes must be a positive int or None"
            )


# --------------------------------------------------------------------------- tokenizer

_ID, _NUM, _STR, _TPL, _RE, _P = "id", "num", "str", "tpl", "regex", "punct"
_Tok = tuple[str, str, int]  # kind, text, offset

_REGEX_AFTER_WORD = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split()
)
_ID_START = frozenset("_$")
# Under a policy: only words that are always keywords (never a variable or a property name).
_POLICY_REGEX_AFTER_WORD = frozenset(
    "return typeof instanceof in new delete void throw case do else".split()
)
_CONTROL_HEADS = frozenset({"if", "while", "for", "with"})


def _is_id_char(ch: str) -> bool:
    return ch.isalnum() or ch in _ID_START or ord(ch) > 127 and ch.isidentifier()


_HEX = frozenset("0123456789abcdefABCDEF")
_MAX_BRACE_DIGITS = 8
_SIMPLE_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}
_LINE_ENDS = "\n\r  "


def _unicode_escape(src: str, j: int) -> tuple[str | None, int]:
    """A backslash-u escape (four hex digits, or hex digits in braces) starting at `j`:
    (the character, the index after it), or (None, j) when there is none."""
    if src.startswith("\\u{", j):
        # Bounded: an unclosed `\u{` must not make every escape scan to the end of the text.
        end = src.find("}", j + 3, j + 3 + _MAX_BRACE_DIGITS + 1)
        digits = src[j + 3 : end] if end > 0 else ""
        if digits and len(digits) <= 8 and all(c in _HEX for c in digits):
            value = int(digits, 16)
            if value <= 0x10FFFF:
                return chr(value), end + 1
        return None, j
    digits = src[j + 2 : j + 6]
    if src.startswith("\\u", j) and len(digits) == 4 and all(c in _HEX for c in digits):
        return chr(int(digits, 16)), j + 6
    return None, j


def _cooked(raw: str) -> str:
    """A string literal's (or a template's) text as the engine reads it, best effort."""
    if "\\" not in raw:
        return raw
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        hex2 = raw[i + 2 : i + 4]
        if nxt == "u":
            decoded, end = _unicode_escape(raw, i)
            if decoded is not None:
                out.append(decoded)
                i = end
                continue
        elif nxt == "x" and len(hex2) == 2 and all(c in _HEX for c in hex2):
            out.append(chr(int(hex2, 16)))
            i += 4
            continue
        elif nxt in _LINE_ENDS:  # a line continuation
            i += 3 if raw.startswith("\\\r\n", i) else 2
            continue
        out.append(_SIMPLE_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _tokenize(src: str, policy: bool = False) -> list[_Tok]:
    """`policy=True` (a `SourcePolicy` scan): identifiers written with backslash-u escapes are
    decoded, every Unicode space is a space, and a template without substitutions keeps its
    text. Without it the tokens are exactly what `check_source` always saw."""
    toks: list[_Tok] = []
    n = len(src)
    i = 0
    stack: list[
        str
    ] = []  # "b" for `{` ("o": an object literal, policy only), "t" for `${`
    # Policy only: whether each open `(` heads a control statement (`if (...)`), and the offsets
    # of the `}` / `)` tokens after which a `/` is division / starts a regex despite the default.
    parens: list[bool] = []
    no_regex_before = 0
    closes_expression: set[int] = set()
    closes_control: set[int] = set()

    def regex_allowed() -> bool:
        if not toks:
            return True
        kind, text, off = toks[-1]
        if kind in (_NUM, _STR, _TPL, _RE):
            return False
        if kind == _ID:
            if not policy:
                return text in _REGEX_AFTER_WORD
            # `obj.in / x / 2` divides; `of`, `yield` and `await` can be plain variables.
            return text in _POLICY_REGEX_AFTER_WORD and not after_access(len(toks) - 1)
        if policy:
            if (
                text in "+-"
                and len(toks) > 1
                and toks[-2][1] == text
                and toks[-2][2] == off - 1
            ):
                return False  # `a++ / x / 2`: a postfix operator, then a division
            if text == "}":
                return off not in closes_expression  # `{} / x / 2` divides
            if text == ")":
                return off in closes_control  # `if (a) /re/.test(s)` starts a regex
        return text not in (")", "]")

    def after_access(k: int) -> bool:
        return k > 0 and toks[k - 1][0] == _P and toks[k - 1][1] in _ACCESS

    def expression_expected() -> bool:
        """Policy only: would a `{` here open an object literal (rather than a block)?"""
        if not toks:
            return False
        kind, text, off = toks[-1]
        if kind == _ID:
            return text in _POLICY_REGEX_AFTER_WORD and not after_access(len(toks) - 1)
        if kind != _P:
            return False
        if (
            text == ">"
            and len(toks) > 1
            and toks[-2][1] == "="
            and toks[-2][2] == off - 1
        ):
            return False  # `=> {`: an arrow function's body
        return text in "([,=:?!~+-*%&|^<>/"

    def scan_template(j: int) -> tuple[int, bool]:
        """From just after a backtick or a closing `}`; returns (index, hit_closing_backtick)."""
        while j < n:
            ch = src[j]
            if ch == "\\":
                j += 2
            elif ch == "`":
                return j + 1, True
            elif ch == "$" and src.startswith("${", j):
                return j + 2, False
            else:
                j += 1
        return n, True  # unterminated: stop quietly

    while i < n:
        ch = src[i]
        if ch in " \t\r\n\f\v ﻿  " or (policy and ch.isspace()):
            i += 1
        elif src.startswith("//", i):
            j = i
            while j < n and src[j] not in "\n\r  ":
                j += 1
            i = j
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch in "'\"":
            j = i + 1
            while j < n and src[j] != ch and src[j] not in "\n\r":
                j += 2 if src[j] == "\\" else 1
            toks.append((_STR, src[i + 1 : j], i))
            i = min(j + 1, n)
        elif ch == "`":
            j, closed = scan_template(i + 1)
            toks.append((_TPL, src[i + 1 : j - 1] if policy and closed else "", i))
            if not closed:
                stack.append("t")
            i = j
        elif ch == "{":
            stack.append("o" if policy and expression_expected() else "b")
            toks.append((_P, ch, i))
            i += 1
        elif ch == "}":
            if stack and stack[-1] == "t":
                stack.pop()
                j, closed = scan_template(i + 1)
                if not closed:
                    stack.append("t")
                i = j
            else:
                if stack and stack.pop() == "o":
                    closes_expression.add(i)
                toks.append((_P, ch, i))
                i += 1
        elif policy and ch in "()":
            if ch == "(":
                head = len(toks) - 1
                parens.append(
                    head >= 0
                    and toks[head][0] == _ID
                    and toks[head][1] in _CONTROL_HEADS
                    and not after_access(head)
                )
            elif parens and parens.pop():
                closes_control.add(i)
            toks.append((_P, ch, i))
            i += 1
        elif ch == "/" and i >= no_regex_before and regex_allowed():
            j, in_class = i + 1, False
            while j < n and src[j] not in "\n\r":
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                j += 1
            if j < n and src[j] == "/":
                j += 1
                while j < n and _is_id_char(src[j]):  # flags
                    j += 1
                toks.append((_RE, "", i))
                i = j
            else:  # not a regex after all (ran into a newline): a division sign
                # No `/` before that line end can start one either: not retrying keeps a line of
                # `/[/[/[...` linear instead of quadratic.
                no_regex_before = j
                toks.append((_P, "/", i))
                i += 1
        elif _is_id_char(ch) and not ch.isdigit() and not policy:
            j = i + 1
            while j < n and _is_id_char(src[j]):
                j += 1
            toks.append((_ID, src[i:j], i))
            i = j
        elif policy and (
            (_is_id_char(ch) and not ch.isdigit()) or src.startswith("\\u", i)
        ):
            j, parts = i, []
            while j < n:
                if _is_id_char(src[j]):
                    parts.append(src[j])
                    j += 1
                    continue
                decoded, end = _unicode_escape(src, j) if src[j] == "\\" else (None, j)
                if decoded is None:
                    break
                parts.append(decoded)
                j = end
            if j == i:  # a backslash that starts no valid escape
                toks.append((_P, ch, i))
                i += 1
            else:
                toks.append((_ID, "".join(parts), i))
                i = j
        elif ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i + 1
            while j < n and (_is_id_char(src[j]) or src[j] == "."):
                j += 1
            toks.append((_NUM, src[i:j], i))
            i = j
        elif src.startswith("?.", i) and not (i + 2 < n and src[i + 2].isdigit()):
            toks.append((_P, "?.", i))
            i += 2
        else:
            toks.append((_P, ch, i))
            i += 1
    return toks


# --------------------------------------------------------------------------- rules

_GLOBALS = frozenset({"globalThis", "self", "window", "global"})
_ACCESS = frozenset({".", "?."})

# identifier -> (rule, needs a following token, message)
_NAMES: dict[str, tuple[str, str | None, str]] = {
    "require": (
        "require",
        "(",
        "require() does not exist in the sandbox; modules are provided by the host "
        "(add_static_module or a resolver), not loaded from disk.",
    ),
    "fetch": (
        "fetch",
        "(",
        "fetch() does not exist in the sandbox (no network); bind a host function that "
        "does the request.",
    ),
    "XMLHttpRequest": (
        "xhr",
        None,
        "XMLHttpRequest does not exist in the sandbox (no network); bind a host function.",
    ),
    "WebSocket": (
        "websocket",
        None,
        "WebSocket does not exist in the sandbox (no network); bind a host function.",
    ),
    "process": (
        "process",
        ".",
        "process is Node.js, not part of the sandbox; pass values in as arguments or bindings.",
    ),
    "Deno": (
        "deno",
        ".",
        "The Deno namespace is not exposed to guest code; bind what the guest needs.",
    ),
    "child_process": (
        "child-process",
        None,
        "child_process does not exist in the sandbox; guests cannot start processes.",
    ),
}


def check_source(
    code: str,
    *,
    allow_import: bool = False,
    allow_dynamic_import: bool = False,
    report_eval: bool = True,
    policy: SourcePolicy | None = None,
) -> PreflightResult:
    """Scan `code` and list what will not work in the sandbox. Never raises and never runs `code`.

    Not a security boundary: see the module docstring. A guest can evade every rule here, and the
    sandbox denies those things regardless of what this function says.

    `allow_import` permits static `import`/`export` (for code you will run with
    `eval_module`); `allow_dynamic_import` permits `import(...)`; `report_eval=False` drops the
    informational note for `eval(` / `new Function(`.

    With a `policy` (a `SourcePolicy`), the result lists the policy's findings instead, each an
    error whose message is a `POLICY_MESSAGES` template; the rules above apply too only with
    ``policy.include_preflight_rules``. Without one, the result is exactly what it always was.
    """
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    if policy is not None:
        if not isinstance(policy, SourcePolicy):
            raise TypeError("policy must be a SourcePolicy")
        limit = policy.max_source_bytes
        if limit is not None:
            # Counted exactly unless the text is huge (then it is over any sane limit anyway,
            # and encoding it only to say by how much is not worth the time).
            size = (
                len(code.encode("utf-8", "surrogatepass"))
                if len(code) <= limit or len(code) <= _EXACT_SIZE_UP_TO
                else None
            )
            if size is None or size > limit:
                # Nothing else is scanned: the size is the finding, and the scan is not free.
                shown = size if size is not None else f"more than {limit}"
                message = POLICY_MESSAGES["source-too-large"].format(
                    size=shown, limit=limit
                )
                return PreflightResult(
                    ok=False, findings=[Finding("source-too-large", message, 1, 1)]
                )
    precise = policy is not None and policy.ignore_strings_and_comments
    usability = policy is None or policy.include_preflight_rules
    toks = _tokenize(code, policy=policy is not None) if usability or precise else []
    line_starts = [0] + [m.end() for m in _NEWLINE.finditer(code)]

    findings: list[Finding] = []
    seen: set[tuple[str, int]] = set()

    def add(rule: str, message: str, offset: int, severity: str = "error") -> None:
        if (rule, offset) in seen:
            return
        seen.add((rule, offset))
        line = bisect_right(line_starts, offset)
        findings.append(
            Finding(rule, message, line, offset - line_starts[line - 1] + 1, severity)
        )

    def at(k: int) -> _Tok | None:
        return toks[k] if 0 <= k < len(toks) else None

    for k, (kind, text, off) in enumerate(toks if usability else ()):
        prev, nxt = at(k - 1), at(k + 1)
        after_dot = prev is not None and prev[0] == _P and prev[1] in _ACCESS
        # `globalThis.fetch`, `self?.process`: a global's property, spelled with a dot.
        via_global = (
            after_dot
            and (p2 := at(k - 2)) is not None
            and p2[0] == _ID
            and p2[1] in _GLOBALS
        )

        if kind == _STR:
            # `globalThis["fetch"]`: only a plain literal is seen; `'fe' + 'tch'` is not.
            if (
                prev is not None
                and prev[1] == "["
                and (p2 := at(k - 2)) is not None
                and p2[0] == _ID
                and p2[1] in _GLOBALS
                and text in _NAMES
            ):
                add(
                    "global-alias",
                    f"{p2[1]}[{text!r}] reaches {text}, which does not exist in the sandbox.",
                    off,
                )
            if text == "__proto__":
                add(
                    "proto",
                    "'__proto__' is a prototype-pollution vector.",
                    off,
                    "warning",
                )
            continue
        if kind != _ID:
            continue

        if after_dot and not via_global:
            if text == "constructor" and prev is not None:
                p2 = at(k - 2)
                if p2 is not None and p2[0] == _ID and p2[1] == "constructor":
                    add(
                        "constructor-constructor",
                        ".constructor.constructor reaches the Function constructor.",
                        off,
                        "warning",
                    )
            if text == "__proto__":
                add(
                    "proto",
                    "__proto__ is a prototype-pollution vector.",
                    off,
                    "warning",
                )
            continue

        if text == "__proto__":
            add("proto", "__proto__ is a prototype-pollution vector.", off, "warning")
        elif text in ("import", "export"):
            if text == "export":
                if not allow_import:
                    add(
                        "static-export",
                        "export statements are for modules; evaluate plain scripts, or use "
                        "eval_module and allow_import=True.",
                        off,
                    )
            elif nxt is not None and nxt[1] == "(":
                if not allow_dynamic_import:
                    add(
                        "dynamic-import",
                        "import(...) loads modules at run time, which the sandbox does not allow "
                        "unless the host registered them.",
                        off,
                    )
            elif nxt is not None and nxt[1] == ".":
                if not allow_import:
                    add(
                        "static-import",
                        "import.meta is only available in modules.",
                        off,
                    )
            elif not allow_import:
                add(
                    "static-import",
                    "import statements are for modules; evaluate plain scripts, or use "
                    "eval_module and allow_import=True.",
                    off,
                )
        elif text in _NAMES:
            rule, needs, message = _NAMES[text]
            if (
                via_global
                or needs is None
                or (nxt is not None and nxt[1] in (needs, "?."))
            ):
                if via_global:
                    add("global-alias", message, off)
                else:
                    add(rule, message, off)
        elif text == "eval" and report_eval:
            if nxt is not None and nxt[1] == "(":
                add(
                    "eval",
                    "eval() compiles strings at run time: hard to review, and an EvalError "
                    "in a runtime started with strict_eval=True.",
                    off,
                    "info",
                )
        elif text == "Function" and report_eval:
            if (
                prev is not None
                and prev[0] == _ID
                and prev[1] == "new"
                and nxt is not None
                and nxt[1] == "("
            ):
                add(
                    "eval",
                    "new Function(...) compiles strings at run time: hard to review, and an "
                    "EvalError in a runtime started with strict_eval=True.",
                    off,
                    "info",
                )

    if precise:
        assert policy is not None
        _apply_policy(policy, toks, at, add)
    elif policy is not None:
        _scan_text(policy, code, add)

    findings.sort(key=lambda f: (f.line, f.column))
    return PreflightResult(
        ok=not any(f.severity == "error" for f in findings), findings=findings
    )


_GLOBAL_BASES = _GLOBALS | {"this"}
_CONSTRUCTOR_BASES = frozenset({"constructor", "Function"})


def _computed_global(
    toks: list[_Tok], k: int, at: Callable[[int], _Tok | None]
) -> bool:
    """`toks[k]` is a `[`: is it a computed key on a global object or the Function constructor?

    The base is the token before the `[` (or before a `?.` in front of it): a global's name
    (not itself a property, as in `x.self[k]`), or `constructor` / `Function`. The key is fine
    when it is one plain literal (a string, a template without substitutions, a number)."""
    base_at = k - 1
    before = at(base_at)
    if before is not None and before[0] == _P and before[1] == "?.":
        base_at -= 1
    base = at(base_at)
    if base is None or base[0] != _ID:
        return False
    if base[1] in _GLOBAL_BASES:
        prior = at(base_at - 1)
        if prior is not None and prior[0] == _P and prior[1] in _ACCESS:
            return False  # `x.self[k]`: a property that happens to be called `self`
    elif base[1] not in _CONSTRUCTOR_BASES:
        return False
    key, close = at(k + 1), at(k + 2)
    literal = (
        key is not None
        and key[0] in (_STR, _TPL, _NUM)
        and close is not None
        and close[0] == _P
        and close[1] == "]"
    )
    return not literal


_TIMERS = frozenset({"setTimeout", "setInterval"})
_TEXT = (_STR, _TPL)


def _apply_policy(
    policy: SourcePolicy,
    toks: list[_Tok],
    at: Callable[[int], _Tok | None],
    add: Callable[[str, str, int], None],
) -> None:
    """The policy's findings, each a `POLICY_MESSAGES` template with host-chosen values."""

    def report(rule: str, offset: int, **values: object) -> None:
        add(rule, POLICY_MESSAGES[rule].format(**values), offset)

    for k, (kind, text, off) in enumerate(toks):
        prev, nxt = at(k - 1), at(k + 1)
        p2 = at(k - 2)
        after_dot = prev is not None and prev[0] == _P and prev[1] in _ACCESS
        if kind == _P and text == "[":
            if policy.forbid_computed_global_access and _computed_global(toks, k, at):
                report("forbidden-computed-global-access", off)
            continue
        if kind in _TEXT:
            # A computed key, `x["name"]`, read as the engine reads the literal.
            if not (
                prev is not None
                and prev[1] == "["
                and nxt is not None
                and nxt[1] == "]"
            ):
                continue
            name = _cooked(text)
            on_global = p2 is not None and p2[0] == _ID and p2[1] in _GLOBALS
            called = (n2 := at(k + 2)) is not None and n2[1] == "("
        elif kind == _ID:
            name = text
            on_global = not after_dot or (
                p2 is not None and p2[0] == _ID and p2[1] in _GLOBALS
            )
            called = nxt is not None and nxt[1] == "("
        else:
            continue

        if name in policy.forbidden_identifiers:
            report("forbidden-identifier", off, name=name)
        if on_global and name in policy.forbidden_globals:
            report("forbidden-global", off, name=name)
        if policy.forbid_eval:
            if name == "eval":
                report("forbidden-eval", off)
            elif name in _TIMERS and on_global and called and kind == _ID:
                # `setTimeout("code", ...)`: the first argument is a string or a template.
                first = at(k + 2)
                if first is not None and first[0] in _TEXT:
                    report("forbidden-string-timer", off, name=name)
        if policy.forbid_function and (
            name == "Function"
            or (name == "constructor" and called and (after_dot or kind != _ID))
        ):
            report("forbidden-function-constructor", off)
        if policy.forbid_webassembly and name == "WebAssembly":
            report("forbidden-webassembly", off)
        if (
            policy.forbid_dynamic_import
            and kind == _ID
            and name == "import"
            and not after_dot
            and called
        ):
            report("forbidden-dynamic-import", off)


# --------------------------------------------------------------------------- the default scan
# Fails closed: every escape is decoded wherever it appears, and the whole decoded text is read,
# comments, strings, templates and regular expressions included. Nothing here depends on telling
# a regex from a division or a comment from code, which is where tokenizer-based scans go wrong.
# Linear time: one pass of compiled patterns without nested quantifiers, plus bounded look-arounds.

_NEWLINE = re.compile(r"\n")
_ESCAPE = re.compile(
    r"\\(?:u\{0*([0-9A-Fa-f]{1,6})\}"  # \u{...}, any number of leading zeros
    r"|u([0-9A-Fa-f]{4})"
    r"|x([0-9A-Fa-f]{2})"
    r"|([0-3][0-7]{0,2}|[4-7][0-7]?)"  # legacy octal
    r"|(\r\n|[\n\r\u2028\u2029])"  # a line continuation: nothing
    r"|(.))",  # an identity escape: the character itself (`\e` is `e`)
    re.S,
)
_WORDS = re.compile(r"[\w$]+")
_BRACKET = re.compile(r"\s*\[")
_STRING_ARGUMENT = re.compile(r"\s*\(\s*['\"`]")
_NEXT = re.compile(r"\s*(.?)", re.S)
_DOTTED = re.compile(r"\s*\??\.\s*[\w$]")
_STATIC_IMPORT_STARTS = re.compile(r"[\w${*\"'.]")
_GLOBAL_VALUES = frozenset({"globalThis", "self", "window", "global", "this"})
_TIMER_NAMES = frozenset({"setTimeout", "setInterval"})
#: The constructor test's bounds: how far back it looks (whitespace and at most two whole `//`
#: comment lines) and how long a parameter list it matches. Past either it reports: constant
#: cost per occurrence, so a text of nothing but `constructor(` stays linear.
_BACK = 64
#: Most findings one policy scan lists (per reading of the text): the code is refused anyway,
#: and collecting 200 000 of them would cost more than the scan.
MAX_POLICY_FINDINGS = 1000
_PARAMS = 1024
_LINE_TERMINATORS = "\n\r  "


def _decoded(code: str, continuation: str) -> tuple[str, list[int], list[int], bool]:
    """`code` with every escape decoded, anchors mapping decoded offsets back, and whether any
    backslash-line-terminator pair was seen. Such a pair is a line continuation inside a string
    (nothing) but plain text inside a `//`, `<!--` or hashbang comment, which still ends at the
    break: the caller scans both readings, `continuation` = "" and "\\n"."""
    pieces: list[str] = []
    norm_at: list[int] = [0]
    orig_at: list[int] = [0]
    last = 0
    size = 0
    continued = False
    for m in _ESCAPE.finditer(code):
        start, end = m.span()
        pieces.append(code[last:start])
        size += start - last
        norm_at.append(size)
        orig_at.append(start)
        brace, four, two, octal, newline, other = m.groups()
        if brace is not None or four is not None:
            value = int(brace if brace is not None else four, 16)
            text = chr(value) if value <= 0x10FFFF else "u"
        elif two is not None:
            text = chr(int(two, 16))
        elif octal is not None:
            text = chr(int(octal, 8))
        elif newline is not None:
            text = continuation
            continued = True
        else:
            text = _SIMPLE_ESCAPES.get(other, other)
        pieces.append(text)
        size += len(text)
        norm_at.append(size)
        orig_at.append(end)
        last = end
    pieces.append(code[last:])
    return "".join(pieces), norm_at, orig_at, continued


def _before(code: str, start: int) -> str:
    """The character before `start`, skipping whitespace and up to two whole `//` comment lines,
    within bounded windows ("" at the start of the text, "?" when a window runs out)."""
    i = start - 1
    for _hop in range(3):
        floor = max(-1, i - _BACK)
        while i > floor and code[i].isspace():
            i -= 1
        if i < 0:
            return ""
        if i == floor:
            return "?"
        window = max(0, i - _BACK)
        line_start = max(code.rfind(t, window, i + 1) for t in _LINE_TERMINATORS)
        if line_start < 0 or not code[line_start + 1 : i + 1].lstrip().startswith("//"):
            return code[i]
        i = line_start  # a whole comment line: look before it
    return "?"


def _parameters_end(code: str, i: int) -> int | None:
    """From the `(` at `i`: the index after its matching `)`, or None when that cannot be shown
    cheaply (a regex, comment or template inside, an unclosed string, too long)."""
    depth = 0
    limit = min(len(code), i + _PARAMS)
    j = i
    while j < limit:
        c = code[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j + 1 if c == ")" else None
            if depth < 0:
                return None
        elif c in "'\"":
            j += 1
            while j < limit and code[j] != c:
                if code[j] in _LINE_TERMINATORS:
                    return None
                j += 2 if code[j] == "\\" else 1
            if j >= limit:
                return None
        elif c in "/`":
            return None
        j += 1
    return None


def _method_definition(code: str, start: int, end: int) -> bool:
    """Is the `constructor` at `code[start:end]` (the original text) provably a method
    definition, `constructor(...) {`? Only when it is preceded by `{`, `}` or `;` (whitespace and
    whole `//` lines aside) and its balanced parameter list is followed, on the same line, by
    `{`: a call has no body there, a definition must. Inside `with (fn) { ... }` a bare
    `constructor(...)` call is the Function constructor, and so is `extends constructor(...)`;
    neither passes. Constant cost per occurrence; when in doubt it is not a definition, and the
    caller reports it."""
    if _before(code, start) not in ("{", "}", ";"):
        return False
    j = end
    limit = min(len(code), end + _BACK)
    while j < limit and code[j].isspace():
        j += 1
    if j >= limit or code[j] != "(":
        return False
    close = _parameters_end(code, j)
    if close is None:
        return False
    limit = min(len(code), close + _BACK)
    while close < limit and code[close] in " \t\v\f ﻿":
        close += 1
    return close < len(code) and code[close] == "{"


def _scan_text(
    policy: SourcePolicy, code: str, add: Callable[[str, str, int], None]
) -> None:
    text, norm_at, orig_at, continued = _decoded(code, "")
    _scan_view(policy, code, text, norm_at, orig_at, add)
    if continued:
        # A backslash at the end of a comment line is text, and the comment ends at the break:
        # read that way too, or `//x\` + newline + `name` would read as the one word `xname`.
        text, norm_at, orig_at, _ = _decoded(code, "\n")
        _scan_view(policy, code, text, norm_at, orig_at, add)


def _scan_view(
    policy: SourcePolicy,
    code: str,
    text: str,
    norm_at: list[int],
    orig_at: list[int],
    add: Callable[[str, str, int], None],
) -> None:
    def original(at: int) -> int:
        k = bisect_right(norm_at, at) - 1
        return orig_at[k] + at - norm_at[k]

    reported = 0

    def report(rule: str, at: int, **values: object) -> None:
        nonlocal reported
        reported += 1
        add(rule, POLICY_MESSAGES[rule].format(**values), original(at))

    identifiers = policy.forbidden_identifiers
    globals_ = policy.forbidden_globals
    computed = policy.forbid_computed_global_access
    interest = set(identifiers) | set(globals_)
    if policy.forbid_eval:
        interest |= {"eval"} | _TIMER_NAMES
    if policy.forbid_function:
        interest |= {"Function", "constructor"}
    if policy.forbid_webassembly:
        interest.add("WebAssembly")
    if policy.forbid_dynamic_import:
        interest.add("import")
    if computed:
        interest |= _GLOBAL_VALUES | {"Reflect", "constructor", "Function"}

    for m in _WORDS.finditer(text):
        word = m.group()
        if word not in interest:
            continue
        if reported >= MAX_POLICY_FINDINGS:
            return  # denied many times over; listing more only costs time
        start, end = m.span()
        after_dot = (
            start > 0 and text[start - 1] == "." and text[start - 3 : start] != "..."
        )
        if word in identifiers:
            report("forbidden-identifier", start, name=word)
        if word in globals_:
            report("forbidden-global", start, name=word)
        if policy.forbid_eval:
            if word == "eval":
                report("forbidden-eval", start)
            elif word in _TIMER_NAMES and _STRING_ARGUMENT.match(text, end):
                report("forbidden-string-timer", start, name=word)
        if policy.forbid_function and (
            word == "Function"
            or (
                word == "constructor"
                and not _method_definition(code, original(start), original(end))
            )
        ):
            report("forbidden-function-constructor", start)
        if policy.forbid_webassembly and word == "WebAssembly":
            report("forbidden-webassembly", start)
        if policy.forbid_dynamic_import and word == "import" and not after_dot:
            following = _NEXT.match(text, end).group(1)  # type: ignore[union-attr]
            if following and not _STATIC_IMPORT_STARTS.match(following):
                report("forbidden-dynamic-import", start)
        if computed and (
            (word in _GLOBAL_VALUES and not after_dot and not _DOTTED.match(text, end))
            or word == "Reflect"
            or (word in ("constructor", "Function") and _BRACKET.match(text, end))
        ):
            report("forbidden-computed-global-access", start)
