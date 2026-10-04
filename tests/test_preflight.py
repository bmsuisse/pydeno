"""`check_source`: a readability aid, never a security boundary.

The first half checks what it flags and, as importantly, what it does not (words inside strings,
templates, comments and regular expressions). The last class is the point of the module's
disclaimer: evasions are NOT detected, and the real denial is shown by running them in an
`IsolatedRuntime`.
"""

from __future__ import annotations

import pytest

from pydeno import IsolatedRuntime, JavaScriptError, RuntimeConfig
from pydeno._preflight import Finding, PreflightResult, check_source


def rules(code: str, **kw: bool) -> list[str]:
    return [f.rule for f in check_source(code, **kw).findings]


class TestFlags:
    @pytest.mark.parametrize(
        ("code", "rule"),
        [
            ('import fs from "fs"', "static-import"),
            ("import {a} from './x.js'", "static-import"),
            ("export const a = 1", "static-export"),
            ("const m = await import('x')", "dynamic-import"),
            ("const fs = require('fs')", "require"),
            ("fetch('http://x')", "fetch"),
            ("new XMLHttpRequest()", "xhr"),
            ("new WebSocket('ws://x')", "websocket"),
            ("process.env.HOME", "process"),
            ("Deno.readTextFile('x')", "deno"),
            ("const cp = child_process", "child-process"),
            ("globalThis.fetch('x')", "global-alias"),
            ("globalThis['require']('fs')", "global-alias"),
            ("self.process.exit()", "global-alias"),
            ("window?.fetch('x')", "global-alias"),
            ("x.__proto__.polluted = 1", "proto"),
            ("a.constructor.constructor('return 1')()", "constructor-constructor"),
            ("eval('1 + 1')", "eval"),
            ("new Function('return 1')", "eval"),
        ],
    )
    def test_each_rule(self, code: str, rule: str) -> None:
        assert rule in rules(code)

    def test_severities_and_ok(self) -> None:
        assert not check_source("require('x')").ok
        warn = check_source("a.__proto__")
        assert warn.ok and warn.findings[0].severity == "warning"
        info = check_source("eval('1')")
        assert info.ok and info.findings[0].severity == "info"
        assert not check_source("eval('1')", report_eval=False).findings

    def test_positions_are_one_based_and_sorted(self) -> None:
        result = check_source("let a = 1;\n  fetch('x');\nrequire('y')")
        assert [(f.rule, f.line, f.column) for f in result.findings] == [
            ("fetch", 2, 3),
            ("require", 3, 1),
        ]
        assert "2:3 error [fetch]" in result.format()

    def test_allow_flags(self) -> None:
        assert rules('import a from "a"; export const b = 1', allow_import=True) == []
        assert rules("import('x')", allow_dynamic_import=True) == []
        assert rules("import('x')", allow_import=True) == ["dynamic-import"]

    def test_result_types(self) -> None:
        result = check_source("1 + 1")
        assert isinstance(result, PreflightResult) and result.ok and bool(result)
        assert check_source("fetch('x')").findings[0].__class__ is Finding

    def test_a_non_string_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            check_source(b"1")  # type: ignore[arg-type]


class TestDoesNotFireInsideText:
    @pytest.mark.parametrize(
        "code",
        [
            "const s = \"require('fs') fetch(1) process.exit() import x from 'y'\"",
            "const s = 'Deno.exit(); eval(1); __proto__'",
            "const t = `require('x') and fetch(1)`",
            "// require('x'); fetch(1)\n1",
            "/* process.env\n   Deno.x\n   import a from 'b' */ 1",
            "const r = /fetch\\(|require\\(/g; r.test('x')",
            "const r = /[/]fetch\\(/; 1",
            "const x = a / b / c; // fetch(",
            "const s = 'it\\'s require(1)'",
            "const t = `${1 + 1} fetch(`",
            "obj.fetch(1); obj.require('x'); obj.process.x; obj.import",
            "const fetchData = 1; const requireX = 2; const processed = 3",
            "const o = { fetch: 1, require: 2 }; o.process",
        ],
    )
    def test_clean(self, code: str) -> None:
        assert check_source(code).findings == []

    def test_code_inside_a_template_substitution_is_still_code(self) -> None:
        assert rules("const t = `a ${ require('fs') } b`") == ["require"]
        assert rules("`${ `${ fetch('x') }` }`") == ["fetch"]
        assert rules("const t = `a ${ {k: 1}.k } ${ fetch('x') }`") == ["fetch"]

    def test_division_is_not_a_regex(self) -> None:
        assert rules("const a = 4 / 2; fetch('x'); const b = 6 / 3") == ["fetch"]

    def test_regex_after_return_and_open_paren(self) -> None:
        assert (
            check_source("function f(s) { return /require\\(/.test(s) }").findings == []
        )
        assert check_source("x.match(/fetch\\(/)").findings == []

    def test_unterminated_input_never_raises(self) -> None:
        for code in ("'abc", '"abc', "`abc ${", "/* open", "/abc", "a = /[", "`${`${`"):
            check_source(code)


class TestEvasionsAreNotDetectedAndTheRuntimeDeniesThem:
    """The disclaimer, proven. A static check reads text; a guest builds the text at run time."""

    EVASIONS = [
        "globalThis['req' + 'uire']",
        "globalThis['fe' + 'tch']",
        "(0, globalThis)['pro' + 'cess']",
        "Reflect.get(globalThis, ['req', 'uire'].join(''))",
        "((k) => globalThis[k + 'uire'])('req')",
    ]

    @pytest.mark.parametrize("code", EVASIONS)
    def test_the_check_does_not_see_them(self, code: str) -> None:
        assert check_source(code).findings == []

    @pytest.mark.parametrize("code", EVASIONS)
    def test_the_isolated_runtime_still_has_nothing_to_give(self, code: str) -> None:
        """Whatever the preflight said, the sandbox decides: none of these names exist."""
        with IsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
            assert rt.eval(f"typeof ({code})") == "undefined"

    def test_the_names_a_preflight_looks_for_are_absent_in_the_isolate(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
            for name in (
                "require",
                "fetch",
                "process",
                "Deno",
                "XMLHttpRequest",
                "WebSocket",
            ):
                assert rt.eval(f"typeof globalThis[{name!r}]") == "undefined", name
            with pytest.raises(JavaScriptError):
                rt.eval("require('fs')")
            with pytest.raises(JavaScriptError):
                rt.eval("fetch('http://127.0.0.1:9')")

    def test_a_clean_preflight_proves_nothing(self) -> None:
        code = "globalThis['req' + 'uire']('fs')"
        assert check_source(code).ok  # the check passes it...
        with IsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
            with pytest.raises(JavaScriptError):  # ...the runtime does not
                rt.eval(code)
