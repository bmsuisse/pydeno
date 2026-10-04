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
        encoding="utf-8",
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
    assert "no code given" in run().stderr
    both = run("1", "-c", "2")
    assert both.returncode == _cli.EXIT_USAGE
    assert "give the code once" in both.stderr
    missing = run("-f", str(tmp_path / "nope.js"))
    assert missing.returncode == _cli.EXIT_USAGE
    assert "cannot read" in missing.stderr
    assert run("--timeout", "0", "1").returncode == _cli.EXIT_USAGE
    assert run("--max-memory", "lots", "1").returncode == _cli.EXIT_USAGE
    for timeout in ("inf", "1e309", "nan", "-1", str(_cli.MAX_TIMEOUT + 1)):
        proc = run("--timeout", timeout, "1")
        assert proc.returncode == _cli.EXIT_USAGE, (timeout, proc.stderr)
    for size in ("99999999999999999999T", "2T", "5ib", "5x", "1.5G", "0"):
        proc = run("--max-memory", size, "1")
        assert proc.returncode == _cli.EXIT_USAGE, (size, proc.stderr)
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


@pytest.mark.parametrize(
    "text",
    ["", "0", "-1M", "1.5G", "M", "10X", "5ib", "5i", "5kk", "2T", "99999999999T"],
)
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


# ---------------------------------------------------------------- bounded input (C1)

_MAXRSS = (
    "import resource, subprocess, sys; "
    "p = subprocess.run([sys.executable, '-m', 'pydeno', *sys.argv[1:]], "
    "stdin=subprocess.DEVNULL, capture_output=True, timeout=60); "
    "sys.stderr.write(p.stderr.decode('utf-8', 'replace')); "
    "rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss; "
    "print(p.returncode, rss * (1 if sys.platform == 'darwin' else 1024))"
)


def _rss_of_cli(*args: str) -> tuple[int, int, str]:
    """Exit code and peak RSS (bytes) of the CLI, measured in a wrapper process of its own."""
    out = subprocess.run(
        [sys.executable, "-c", _MAXRSS, *args],
        capture_output=True,
        text=True,
        timeout=90,
    )
    code, rss = out.stdout.split()
    return int(code), int(rss), out.stderr


def test_input_limit_matches_the_wire_frame_cap() -> None:
    from pydeno import _wire

    assert _cli.MAX_CODE_BYTES == _wire.MAX_FRAME_BYTES


def test_endless_file_is_refused_with_bounded_memory() -> None:
    code, rss, err = _rss_of_cli("-f", "/dev/zero")
    assert code == _cli.EXIT_USAGE
    assert "larger than" in err
    assert rss < 200 * 1024 * 1024, rss


def test_endless_stdin_is_refused_with_bounded_memory() -> None:
    wrapper = _MAXRSS.replace(
        "stdin=subprocess.DEVNULL", "stdin=open('/dev/zero', 'rb')"
    )
    out = subprocess.run(
        [sys.executable, "-c", wrapper, "-"], capture_output=True, text=True, timeout=90
    )
    code, rss = (int(x) for x in out.stdout.split())
    assert code == _cli.EXIT_USAGE
    assert "larger than" in out.stderr
    assert rss < 200 * 1024 * 1024, rss


def test_oversized_file_and_stdin_exit_2(tmp_path) -> None:
    big = tmp_path / "big.js"
    big.write_bytes(b"1;" + b" " * (17 * 1024 * 1024))
    proc = run("-f", str(big))
    assert proc.returncode == _cli.EXIT_USAGE
    assert "larger than" in proc.stderr
    proc = run(stdin="1;" + " " * (17 * 1024 * 1024))
    assert proc.returncode == _cli.EXIT_USAGE
    assert "larger than" in proc.stderr


def test_input_that_is_not_utf8_exits_2(tmp_path) -> None:
    bad = tmp_path / "bad.js"
    bad.write_bytes(b"'\xff'")
    proc = run("-f", str(bad))
    assert proc.returncode == _cli.EXIT_USAGE
    assert "UTF-8" in proc.stderr


# ---------------------------------------------------------------- terminal escapes (C2)

# OSC 52 (clipboard write), CSI clear-screen, a C1 CSI, DEL, carriage return, bidi overrides,
# zero-width and BOM characters, and a tag character.
HOSTILE = (
    "\x1b]52;c;ZXZpbA==\x07\x1b[2J\x9b31m\x7f\r\u202eevil\u202c\u200b\u2066\ufeff\U000e0041"
    # other format characters (Cf), line/paragraph separators, fillers, joiners, selectors
    "\u00ad\u180e\u2028\u2029\u115f\u1160\u3164\uffa0\ufe0f\U000e0100\u034f"
    "\u0600\u0605\u2061\u061c"
)
_JS_HOSTILE = json.dumps(HOSTILE)
# Every character of HOSTILE that is not printable ASCII must be gone from terminal output.
_FORBIDDEN = sorted({ch for ch in HOSTILE if not (" " <= ch <= "~")})


def _assert_inert(text: str) -> None:
    for ch in _FORBIDDEN:
        assert ch not in text, (repr(ch), repr(text))


@full_sandbox
@pytest.mark.parametrize("mode", ["--json", "--raw"])
def test_console_output_is_inert(mode: str) -> None:
    proc = run(
        mode,
        f"console.log({_JS_HOSTILE}); console.warn({_JS_HOSTILE}); "
        f"console.error({_JS_HOSTILE}); 1",
    )
    assert proc.returncode == 0, proc.stderr
    _assert_inert(proc.stdout)
    _assert_inert(proc.stderr)
    assert "evil" in proc.stdout and "evil" in proc.stderr
    assert "\n" in proc.stdout  # line structure kept


@full_sandbox
def test_raw_result_is_inert() -> None:
    proc = run("--raw", _JS_HOSTILE)
    assert proc.returncode == 0, proc.stderr
    _assert_inert(proc.stdout)
    assert "evil" in proc.stdout


@full_sandbox
def test_json_result_is_inert_and_lossless() -> None:
    proc = run(f"({{text: {_JS_HOSTILE}, [{_JS_HOSTILE}]: 1}})")
    assert proc.returncode == 0, proc.stderr
    _assert_inert(proc.stdout)
    assert json.loads(proc.stdout) == {"text": HOSTILE, HOSTILE: 1}


@full_sandbox
def test_error_text_is_inert() -> None:
    proc = run(f"throw new Error({_JS_HOSTILE})")
    assert proc.returncode == _cli.EXIT_JS_ERROR
    _assert_inert(proc.stderr)


def test_escape_helpers_unit() -> None:
    assert _cli.inert_text("a\nb\tc") == "a\nb\tc"
    _assert_inert(_cli.inert_text(HOSTILE))
    encoded = _cli.to_json(HOSTILE)
    _assert_inert(encoded)
    assert json.loads(encoded) == HOSTILE
    assert _cli.to_json("é ü 中") == '"é ü 中"'  # ordinary text stays readable


# ---------------------------------------------------------------- results (L3)


@full_sandbox
@pytest.mark.parametrize(
    "code",
    [
        "const a = {}; a.self = a; a",
        "new Map([[1, 2]])",
        "Symbol('x')",
        "new Error('e')",
        "(() => 1)",
        "new Date(NaN)",
        "Promise.resolve(new Set([new Map()]))",
    ],
)
def test_unconvertible_result_exits_6(code: str) -> None:
    proc = run(code)
    assert proc.returncode == _cli.EXIT_RESULT, proc.stderr
    assert proc.stdout == ""
    assert "result" in proc.stderr


@full_sandbox
def test_large_integers_print_as_strings() -> None:
    proc = run("[2n ** 100n, 2n, 9007199254740991n, -(2n ** 60n), 1.5]")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "1267650600228229401496703205376",
        2,
        9007199254740991,
        "-1152921504606846976",
        1.5,
    ]


# ---------------------------------------------------------------- the real default (no mocks)


def _spy(monkeypatch) -> list[str]:
    from pydeno import _isolated

    seen: list[str] = []
    real = _isolated.IsolatedRuntime

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            seen.append(self.sandbox)

    monkeypatch.setattr(_isolated, "IsolatedRuntime", Spy)
    return seen


@full_sandbox
def test_default_run_applies_the_complete_os_sandbox(monkeypatch, capsys) -> None:
    seen = _spy(monkeypatch)
    assert _cli.main(["6 * 7"]) == 0
    assert capsys.readouterr().out == "42\n"
    expected = "seatbelt" if sys.platform == "darwin" else "landlock+seccomp"
    assert seen == [expected]


def test_no_sandbox_run_really_has_none(monkeypatch, capsys) -> None:
    seen = _spy(monkeypatch)
    assert _cli.main(["--no-sandbox", "1"]) == 0
    assert seen == ["none"]
    assert "WITHOUT the OS sandbox" in capsys.readouterr().err


# ---------------------------------------------------------------- what is imported when (L4)


def test_usage_errors_start_no_worker_machinery() -> None:
    code = (
        "import sys\n"
        "from pydeno import _cli\n"
        "try:\n"
        "    _cli.main(['--timeout', 'inf', '1'])\n"
        "except SystemExit as e:\n"
        "    assert e.code == 2, e.code\n"
        "print(sorted(m for m in ('pydeno._isolated', 'pydeno._agent', 'pydeno._wire', "
        "'asyncio') if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------- re-review lows


def test_frame_cap_after_escaping_exits_2(tmp_path) -> None:
    """Under the 16 MiB read cap, but over the frame cap once JSON-escaped for the worker."""
    newlines = run(stdin="1" + "\n" * (9 * 1024 * 1024))
    assert newlines.returncode == _cli.EXIT_USAGE, newlines.stderr
    assert "too large" in newlines.stderr
    full = tmp_path / "full.js"
    full.write_bytes(b"1" + b" " * (_cli.MAX_CODE_BYTES - 1))
    proc = run("-f", str(full))
    assert proc.returncode == _cli.EXIT_USAGE, proc.stderr
    assert "too large" in proc.stderr


def test_no_arguments_on_a_terminal_says_why(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(SystemExit) as exited:
        _cli.main([])
    assert exited.value.code == _cli.EXIT_USAGE
    assert "no code given" in capsys.readouterr().err


def test_argv_that_is_not_utf8_exits_2() -> None:
    code = b"'\xff'".decode("utf-8", "surrogateescape")
    proc = subprocess.run(
        [sys.executable, "-m", "pydeno", code],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == _cli.EXIT_USAGE, proc.stderr
    assert "UTF-8" in proc.stderr


@pytest.mark.parametrize(
    "ch",
    [
        "\u00ad",
        "\u180e",
        "\u2028",
        "\u2029",
        "\u3164",
        "\ufe0f",
        "\U000e0100",
        "\u034f",
        "\u0600",
        "\ud800",
    ],
    ids=lambda c: f"U+{ord(c):04X}",
)
def test_every_invisible_class_is_handled(ch: str) -> None:
    assert _cli.inert_text(f"a{ch}b") == "a?b"
    encoded = _cli.to_json(f"a{ch}b")
    assert ch not in encoded
    assert json.loads(encoded) == f"a{ch}b"


def test_frame_cap_refusal_from_the_encoder_is_also_exit_2(
    tmp_path, monkeypatch, capsys
) -> None:
    """If the size estimate is ever short, the encoder's own refusal still maps to a usage error."""
    monkeypatch.setattr(_cli, "_encoded_size", lambda code: 0)
    newlines = tmp_path / "newlines.js"
    newlines.write_bytes(b"1" + b"\n" * (9 * 1024 * 1024))
    assert _cli.main(["--no-sandbox", "-f", str(newlines)]) == _cli.EXIT_USAGE
    assert "too large" in capsys.readouterr().err
