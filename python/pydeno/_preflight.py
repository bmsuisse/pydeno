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
and `WebAssembly`, and a size cap. In that mode the scanner also reads identifiers and string keys
the way the engine does (``\\u0065val`` is ``eval``, and so is ``globalThis["\\x65val"]``) and
treats every Unicode space as a space. It is still a heuristic (it cannot see a name built at run
time), so it errs towards reporting: a local variable or an object key with a forbidden name is a
finding. Every policy finding's message is a fixed template from `POLICY_MESSAGES`, filled only
with host-chosen names and numbers, never with text from the code: that wording is a public
contract.
"""

from __future__ import annotations

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
POLICY_MESSAGES: dict[str, str] = {
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
}


_EXACT_SIZE_UP_TO = 16 * 1024 * 1024


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

    Args:
        forbidden_identifiers: Names refused wherever they appear as an identifier, a property
            (``x.name``) or a string key (``x["name"]``).
        forbidden_globals: Names refused as a bare identifier or as a property of a global object
            (``globalThis.name``, ``self["name"]``); ``x.name`` on another object is allowed.
        forbid_dynamic_import: Refuse ``import(...)``.
        forbid_eval: Refuse ``eval`` (called or merely named, as in ``(0, eval)``) and
            ``setTimeout`` / ``setInterval`` called with a string.
        forbid_function: Refuse the ``Function`` constructor, by name or as ``.constructor(...)``.
        forbid_webassembly: Refuse ``WebAssembly``.
        max_source_bytes: Refuse code longer than this many UTF-8 bytes (checked first; nothing
            else is scanned then).
        include_preflight_rules: Also apply `check_source`'s usability rules (``require``,
            ``fetch``, static ``import``, ...), which are off under a policy by default.

    Heuristic, never the boundary: pair it with ``strict_eval=True`` and the isolated worker.
    """

    forbidden_identifiers: frozenset[str] = frozenset()
    forbidden_globals: frozenset[str] = frozenset()
    forbid_dynamic_import: bool = False
    forbid_eval: bool = False
    forbid_function: bool = False
    forbid_webassembly: bool = False
    max_source_bytes: int | None = None
    include_preflight_rules: bool = False

    def __post_init__(self) -> None:
        for name in ("forbidden_identifiers", "forbidden_globals"):
            object.__setattr__(self, name, _names(name, getattr(self, name)))
        for name in (
            "forbid_dynamic_import",
            "forbid_eval",
            "forbid_function",
            "forbid_webassembly",
            "include_preflight_rules",
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
        end = src.find("}", j + 3)
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
        elif ch == "/" and regex_allowed():
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
    toks = _tokenize(code, policy=policy is not None)
    line_starts = [0]
    for idx, ch in enumerate(code):
        if ch == "\n":
            line_starts.append(idx + 1)

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

    usability = policy is None or policy.include_preflight_rules
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

    if policy is not None:
        _apply_policy(policy, toks, at, add)

    findings.sort(key=lambda f: (f.line, f.column))
    return PreflightResult(
        ok=not any(f.severity == "error" for f in findings), findings=findings
    )


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
