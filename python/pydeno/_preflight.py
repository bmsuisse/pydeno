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
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

__all__ = ["Finding", "PreflightResult", "check_source"]


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


# --------------------------------------------------------------------------- tokenizer

_ID, _NUM, _STR, _TPL, _RE, _P = "id", "num", "str", "tpl", "regex", "punct"
_Tok = tuple[str, str, int]  # kind, text, offset

_REGEX_AFTER_WORD = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split()
)
_ID_START = frozenset("_$")


def _is_id_char(ch: str) -> bool:
    return ch.isalnum() or ch in _ID_START or ord(ch) > 127 and ch.isidentifier()


def _tokenize(src: str) -> list[_Tok]:
    toks: list[_Tok] = []
    n = len(src)
    i = 0
    stack: list[str] = []  # "b" for `{`, "t" for a template's `${`

    def regex_allowed() -> bool:
        if not toks:
            return True
        kind, text, _ = toks[-1]
        if kind in (_NUM, _STR, _TPL, _RE):
            return False
        if kind == _ID:
            return text in _REGEX_AFTER_WORD
        return text not in (")", "]")

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
        if ch in " \t\r\n\f\v ﻿  ":
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
            toks.append((_TPL, "", i))
            if not closed:
                stack.append("t")
            i = j
        elif ch == "{":
            stack.append("b")
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
                if stack:
                    stack.pop()
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
        elif _is_id_char(ch) and not ch.isdigit():
            j = i + 1
            while j < n and _is_id_char(src[j]):
                j += 1
            toks.append((_ID, src[i:j], i))
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
) -> PreflightResult:
    """Scan `code` and list what will not work in the sandbox. Never raises and never runs `code`.

    Not a security boundary: see the module docstring. A guest can evade every rule here, and the
    sandbox denies those things regardless of what this function says.

    `allow_import` permits static `import`/`export` (for code you will run with
    `eval_module`); `allow_dynamic_import` permits `import(...)`; `report_eval=False` drops the
    informational note for `eval(` / `new Function(`.
    """
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    toks = _tokenize(code)
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

    for k, (kind, text, off) in enumerate(toks):
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
                    "eval() compiles strings at run time; it works, but is hard to review.",
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
                    "new Function(...) compiles strings at run time; it works, but is hard to review.",
                    off,
                    "info",
                )

    findings.sort(key=lambda f: (f.line, f.column))
    return PreflightResult(
        ok=not any(f.severity == "error" for f in findings), findings=findings
    )
