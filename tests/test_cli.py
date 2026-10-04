"""`python -m pydeno` / the `pydeno` console script (`pydeno._cli`).

The subprocess tests run the real command. The default is ``sandbox="require"``, so the ones that
use it are `full_sandbox` (deselected where the container matrix hands over a kernel that lacks a
layer); the ``--no-sandbox`` ones run everywhere the worker does. POSIX only (the worker is), so
the file is in the Windows `collect_ignore` list.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pytest

from pydeno import _cli

full_sandbox = pytest.mark.full_sandbox


def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pydeno", *args],
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        timeout=120,
    )


@full_sandbox
def test_expression_prints_json() -> None:
    proc = run("({a: [1, 2], b: 'x'})")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"a": [1, 2], "b": "x"}
    assert proc.stderr == ""


@full_sandbox
def test_promise_is_awaited_and_console_goes_to_the_streams() -> None:
    proc = run("-c", "console.log('out', 1); console.error('err'); Promise.resolve(42)")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["out 1", "42"]
    assert proc.stderr.splitlines() == ["err"]


@full_sandbox
def test_code_from_stdin_and_from_a_file(tmp_path) -> None:
    assert run(stdin="[1, 2].map(x => x * 2)").stdout.strip() == "[2, 4]"
    assert run("-", stdin="'a' + 'b'").stdout.strip() == '"ab"'
    script = tmp_path / "script.js"
    script.write_text("const n = 6;\nn * 7\n", encoding="utf-8")
    assert run("-f", str(script)).stdout.strip() == "42"


@full_sandbox
def test_raw_prints_strings_unquoted_json_is_the_default() -> None:
    assert run("'héllo'").stdout == '"héllo"\n'
    assert run("--raw", "'héllo'").stdout == "héllo\n"
    assert run("--raw", "[1]").stdout == "[1]\n"
    assert run("--json", "'x'").stdout == '"x"\n'


@full_sandbox
def test_undefined_prints_nothing() -> None:
    proc = run("undefined")
    assert proc.returncode == 0
    assert proc.stdout == ""


@full_sandbox
def test_javascript_error_exits_1_with_the_error_on_stderr() -> None:
    proc = run("throw new TypeError('boom')")
    assert proc.returncode == _cli.EXIT_JS_ERROR
    assert proc.stdout == ""
    assert "TypeError: boom" in proc.stderr
    assert "js_error" in proc.stderr


@full_sandbox
def test_syntax_error_exits_1() -> None:
    proc = run("1 +")
    assert proc.returncode == _cli.EXIT_JS_ERROR
    assert "SyntaxError" in proc.stderr


@full_sandbox
def test_timeout_exits_3() -> None:
    proc = run("--timeout", "0.5", "while (true) {}")
    assert proc.returncode == _cli.EXIT_TIMEOUT
    assert "timeout" in proc.stderr


@full_sandbox
def test_max_memory_kills_the_worker() -> None:
    proc = run(
        "--max-memory",
        "64M",
        "const a = []; while (true) a.push(new Array(1e6).fill(1));",
    )
    assert proc.returncode == _cli.EXIT_RUNTIME
    assert "memory_limit" in proc.stderr


@full_sandbox
def test_no_os_access_by_default() -> None:
    proc = run("typeof require + ' ' + typeof Deno + ' ' + typeof process")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == "undefined undefined undefined"


def test_no_sandbox_warns_on_stderr() -> None:
    proc = run("--no-sandbox", "2 + 2")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "4\n"
    assert "WITHOUT the OS sandbox" in proc.stderr


def test_usage_errors_exit_2(tmp_path) -> None:
    assert run().returncode == _cli.EXIT_USAGE  # nothing on stdin
    assert run("-c", "  ").returncode == _cli.EXIT_USAGE
    both = run("1", "-c", "2")
    assert both.returncode == _cli.EXIT_USAGE
    assert "give the code once" in both.stderr
    missing = run("-f", str(tmp_path / "nope.js"))
    assert missing.returncode == _cli.EXIT_USAGE
    assert "cannot read" in missing.stderr
    assert run("--timeout", "0", "1").returncode == _cli.EXIT_USAGE
    assert run("--max-memory", "lots", "1").returncode == _cli.EXIT_USAGE
    assert run("--sandbox", "off", "1").returncode == _cli.EXIT_USAGE
    assert run("--no-sandbox", "--sandbox", "auto", "1").returncode == _cli.EXIT_USAGE


def test_sandbox_unavailable_exits_4(monkeypatch, capsys) -> None:
    """The refusal ``sandbox="require"`` reports when a layer is missing maps to its own code."""
    from pydeno import _isolated

    def refuse(*args: object, **kwargs: object) -> None:
        raise _isolated.WorkerCrashed(
            "worker failed to start: an OS sandbox is required but Landlock is unavailable"
        )

    monkeypatch.setattr(_isolated, "IsolatedRuntime", refuse)
    assert _cli.main(["1"]) == _cli.EXIT_NO_SANDBOX
    err = capsys.readouterr().err
    assert "sandbox_unavailable" in err
    assert "sandbox_status" in err


def test_default_is_sandbox_require(monkeypatch) -> None:
    from pydeno import _isolated

    seen: dict[str, object] = {}

    class Fake:
        def __init__(self, config: object, **options: object) -> None:
            seen.update(options)

        async def eval_async(self, code: str, *, timeout: float) -> int:
            seen["timeout"] = timeout
            return 1

        def close(self) -> None:
            pass

    monkeypatch.setattr(_isolated, "IsolatedRuntime", Fake)
    assert _cli.main(["1"]) == 0
    assert seen == {"sandbox": "require", "timeout": _cli.DEFAULT_TIMEOUT}
    seen.clear()
    assert _cli.main(["--sandbox", "auto", "--max-memory", "1G", "1"]) == 0
    assert seen == {"sandbox": "auto", "max_memory": 1024**3, "timeout": 30.0}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1024", 1024),
        ("256M", 256 * 1024**2),
        ("256m", 256 * 1024**2),
        ("1G", 1024**3),
        ("512KiB", 512 * 1024),
        ("2MB", 2 * 1024**2),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert _cli.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "0", "-1M", "1.5G", "M", "10X"])
def test_parse_size_refuses(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _cli.parse_size(text)


def test_cli_module_imports_nothing_heavy() -> None:
    """`pydeno._cli` defers the worker machinery until `main` runs."""
    code = (
        "import sys, pydeno._cli; "
        "print(sorted(m for m in ('pydeno._isolated', 'pydeno._agent', 'asyncio') "
        "if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
