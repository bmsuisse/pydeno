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

The scan itself is native (`src/scanner/`): a pure function of the text and the options that runs
with the GIL released. Its specification is the Python implementation in `_preflight_reference`,
and the two give identical findings (`tests/test_scanner_differential.py`).

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
so it does not depend on telling a regex from a division; the opt-in precise mode
(``ignore_strings_and_comments=True``) uses a tokenizer and has known bypasses. Both run in
linear time. It is still a heuristic (it cannot see a name built at run time), so it errs towards
reporting: a local variable, an object key or a word in a comment with a forbidden name is a
finding. Every policy finding's message is a fixed template from `POLICY_MESSAGES`, filled only
with host-chosen names and numbers, never with text from the code: that wording is a public
contract.
"""

from __future__ import annotations

# PEP 810 (Python 3.15): these stdlib modules are loaded on first use, not at import. A plain
# list, so it is inert on 3.10-3.14. Never list what the isolation worker imports before it
# applies its sandbox (`_worker`, `_sandbox`, `_wire`, `_wasm`, `_awaitable`): a lazy import
# there would run after the sandbox closed the filesystem.
__lazy_modules__ = ["unicodedata"]

import unicodedata
from types import MappingProxyType
from dataclasses import dataclass, field

from ._pydeno import _scan_source

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
            (see `_method_definition` in `_preflight_reference`; inside ``with`` a bare ``constructor`` is Function).
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


#: Most findings one policy scan lists (per reading of the text): the code is refused anyway,
#: and collecting 200 000 of them would cost more than the scan.
MAX_POLICY_FINDINGS = 1000

# --------------------------------------------------------------------------- the usability rules

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

# --------------------------------------------------------------------------- the native scan

#: The most UTF-8 bytes the native scanner takes (`src/scanner/mod.rs`, the gate's own cap).
_NATIVE_MAX_SOURCE_BYTES = 16 * 1024 * 1024


def _native_unicode() -> str | None:
    """This interpreter's Unicode version if the native tables cover it (they cover every CPython
    pydeno supports); otherwise every scan uses the reference implementation."""
    try:
        _scan_source("", unicodedata.unidata_version)
    except ValueError:
        return None
    return unicodedata.unidata_version


_UNICODE = _native_unicode()

# `_scan_source`'s policy flags.
_P_DYNAMIC_IMPORT, _P_EVAL, _P_FUNCTION, _P_WEBASSEMBLY = 1, 2, 4, 8
_P_COMPUTED, _P_PREFLIGHT_RULES, _P_PRECISE = 16, 32, 64

#: The order of the globals and of `_NAMES` in `src/scanner/mod.rs` (`GLOBALS`, `NAMES`).
_NATIVE_GLOBALS = ("globalThis", "self", "window", "global")
_NATIVE_NAMES = tuple(_NAMES)
_TIMER_NAMES = ("setTimeout", "setInterval")

#: The fixed findings of the native scan by code (`src/scanner/mod.rs`): (rule, message, severity).
_FIXED: dict[int, tuple[str, str, str]] = {
    3: (
        "forbidden-dynamic-import",
        POLICY_MESSAGES["forbidden-dynamic-import"],
        "error",
    ),
    4: ("forbidden-eval", POLICY_MESSAGES["forbidden-eval"], "error"),
    6: (
        "forbidden-function-constructor",
        POLICY_MESSAGES["forbidden-function-constructor"],
        "error",
    ),
    7: ("forbidden-webassembly", POLICY_MESSAGES["forbidden-webassembly"], "error"),
    8: (
        "forbidden-computed-global-access",
        POLICY_MESSAGES["forbidden-computed-global-access"],
        "error",
    ),
    22: ("proto", "'__proto__' is a prototype-pollution vector.", "warning"),
    23: ("proto", "__proto__ is a prototype-pollution vector.", "warning"),
    24: (
        "constructor-constructor",
        ".constructor.constructor reaches the Function constructor.",
        "warning",
    ),
    25: (
        "static-export",
        "export statements are for modules; evaluate plain scripts, or use "
        "eval_module and allow_import=True.",
        "error",
    ),
    26: (
        "dynamic-import",
        "import(...) loads modules at run time, which the sandbox does not allow "
        "unless the host registered them.",
        "error",
    ),
    27: ("static-import", "import.meta is only available in modules.", "error"),
    28: (
        "static-import",
        "import statements are for modules; evaluate plain scripts, or use "
        "eval_module and allow_import=True.",
        "error",
    ),
    30: (
        "eval",
        "eval() compiles strings at run time: hard to review, and an EvalError "
        "in a runtime started with strict_eval=True.",
        "info",
    ),
    31: (
        "eval",
        "new Function(...) compiles strings at run time: hard to review, and an "
        "EvalError in a runtime started with strict_eval=True.",
        "info",
    ),
}


def _native_check(
    code: str,
    allow_import: bool,
    allow_dynamic_import: bool,
    report_eval: bool,
    policy: SourcePolicy | None,
) -> PreflightResult | None:
    """The native scan's result, or None when it does not take this text (see `_UNICODE` and
    `_NATIVE_MAX_SOURCE_BYTES`)."""
    if _UNICODE is None or len(code) > _NATIVE_MAX_SOURCE_BYTES:
        return None
    identifiers: tuple[str, ...] = ()
    globals_: tuple[str, ...] = ()
    spec = None
    if policy is not None:
        identifiers = tuple(policy.forbidden_identifiers)
        globals_ = tuple(policy.forbidden_globals)
        flags = (
            _P_DYNAMIC_IMPORT * policy.forbid_dynamic_import
            | _P_EVAL * policy.forbid_eval
            | _P_FUNCTION * policy.forbid_function
            | _P_WEBASSEMBLY * policy.forbid_webassembly
            | _P_COMPUTED * policy.forbid_computed_global_access
            | _P_PREFLIGHT_RULES * policy.include_preflight_rules
            | _P_PRECISE * policy.ignore_strings_and_comments
        )
        spec = (list(identifiers), list(globals_), flags)
    try:
        rows = _scan_source(
            code, _UNICODE, spec, allow_import, allow_dynamic_import, report_eval
        )
    except ValueError:  # over the native cap in UTF-8 bytes
        return None
    findings = []
    for kind, line, column, arg in rows:
        fixed = _FIXED.get(kind)
        if fixed is not None:
            rule, message, severity = fixed
        elif kind == 1:
            rule, severity = "forbidden-identifier", "error"
            message = POLICY_MESSAGES[rule].format(name=identifiers[arg])
        elif kind == 2:
            rule, severity = "forbidden-global", "error"
            message = POLICY_MESSAGES[rule].format(name=globals_[arg])
        elif kind == 5:
            rule, severity = "forbidden-string-timer", "error"
            message = POLICY_MESSAGES[rule].format(name=_TIMER_NAMES[arg])
        elif kind == 20:
            rule, severity = "global-alias", "error"
            base, name = _NATIVE_GLOBALS[arg // 16], _NATIVE_NAMES[arg % 16]
            message = (
                f"{base}[{name!r}] reaches {name}, which does not exist in the sandbox."
            )
        elif kind == 21:
            rule, severity = "global-alias", "error"
            message = _NAMES[_NATIVE_NAMES[arg]][2]
        elif kind == 29:
            rule, _needs, message = _NAMES[_NATIVE_NAMES[arg]]
            severity = "error"
        else:  # pragma: no cover - the native scanner and this table disagree
            raise AssertionError(f"unknown native finding {kind}")
        findings.append(Finding(rule, message, line, column, severity))
    return PreflightResult(
        ok=not any(f.severity == "error" for f in findings), findings=findings
    )


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
    if type(code) is not str:
        code = str.__str__(code)  # the underlying text, whatever a subclass overrides
    result = _native_check(
        code, allow_import, allow_dynamic_import, report_eval, policy
    )
    if result is not None:
        return result
    from . import _preflight_reference

    return _preflight_reference.check_source(
        code,
        allow_import=allow_import,
        allow_dynamic_import=allow_dynamic_import,
        report_eval=report_eval,
        policy=policy,
    )
