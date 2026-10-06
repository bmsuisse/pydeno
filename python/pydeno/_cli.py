"""The ``pydeno`` command: evaluate JavaScript in a sandboxed worker and print the result as JSON.

``python -m pydeno '1 + 2'`` and the ``pydeno`` console script both land in `main`. Input runs in
an `IsolatedRuntime` with ``sandbox="require"`` (a worker process under the OS sandbox, ``--jitless``
V8, a memory cap and a deadline), never in the in-process `Runtime`: a one-liner is the kind of
place untrusted code ends up. ``--no-sandbox`` keeps the worker process and its limits but drops
the OS sandbox, and says so on stderr.

This module imports only the standard library at module level. Running it (``python -m pydeno``
or the console script) still imports the `pydeno` package, which loads the native extension; what
is deferred until the arguments are parsed and the input is read is the worker machinery
(`IsolatedRuntime`, the wire, asyncio), so ``--help`` and usage errors start no worker process.

Everything that reaches the terminal is made inert first: guest text (console output, a ``--raw``
string, an error message) has control, format, separator and other invisible characters replaced
with ``?`` (see `_unsafe`), and JSON output escapes them as ``\\uXXXX`` (lossless). Input is read at most
`MAX_CODE_BYTES` (the worker's frame cap) plus one byte, so ``-f /dev/zero`` cannot fill memory.

Exit codes (see ``docs/guides/cli.md``):

* 0: success
* 1: the JavaScript threw or failed to compile
* 2: usage error (bad arguments, unreadable, oversized or non-UTF-8 input; oversized means over
  the frame cap once the code is encoded as a JSON string)
* 3: timeout (the deadline or the worker's CPU cap)
* 4: the OS sandbox is unavailable here (``sandbox="require"`` refused to start)
* 5: any other runtime failure (memory limit, worker crash, ...)
* 6: the code ran but its result cannot be converted to JSON (a circular structure, a ``Map``,
  ``Symbol``, ``Error`` or function, an invalid ``Date``)
"""

from __future__ import annotations

# PEP 810 (Python 3.15): these stdlib modules are loaded on first use, not at import. A plain
# list, so it is inert on 3.10-3.14. Never list what the isolation worker imports before it
# applies its sandbox (`_worker`, `_sandbox`, `_wire`, `_wasm`, `_awaitable`): a lazy import
# there would run after the sandbox closed the filesystem.
__lazy_modules__ = ["argparse", "unicodedata"]

import argparse
import json
import math
import re
import sys
import unicodedata
from typing import Any

__all__ = ["inert_text", "main", "parse_size", "to_json"]

EXIT_OK = 0
EXIT_JS_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_NO_SANDBOX = 4
EXIT_RUNTIME = 5
EXIT_RESULT = 6

_KIND_EXIT = {
    "js_error": EXIT_JS_ERROR,
    "timeout": EXIT_TIMEOUT,
    "cpu_limit": EXIT_TIMEOUT,
    "sandbox_unavailable": EXIT_NO_SANDBOX,
    "limits_unmeasurable": EXIT_NO_SANDBOX,
}

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 24 * 3600.0
MAX_MEMORY = 1024**4  # 1 TiB: anything above is a typo, not a limit
# The most code the worker accepts: one wire frame (`pydeno._wire.MAX_FRAME_BYTES`, kept equal by a
# test; not imported, so reading the input does not load the wire).
MAX_CODE_BYTES = 16 * 1024 * 1024

_SIZE = re.compile(r"(\d+)\s*(?:([kmgt])(?:i?b)?|b)?", re.IGNORECASE)
_UNITS = {None: 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}

# Anything but tab, newline and printable ASCII is looked at (`_unsafe`); the rest is left alone.
_CANDIDATE = re.compile("[^\t\n\x20-\x7e]")
# What a terminal acts on, or what hides or reorders text: controls (Cc: C0 except tab and newline,
# DEL, C1 such as the one-character CSI U+009B), format characters (Cf: soft hyphen, zero-width and
# direction marks, bidi embeddings, overrides and isolates, invisible operators, BOM, Arabic number
# signs, tag characters, ...), lone surrogates (Cs) and the line and paragraph separators (Zl, Zp).
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# Invisible characters in other categories: the combining grapheme joiner, Hangul fillers, and
# variation selectors (Mongolian, standard, supplementary).
_INVISIBLE = frozenset(
    [0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x180B, 0x180C, 0x180D, 0x180F]
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0100, 0xE01F0))
)
_MAX_SAFE_INT = 2**53 - 1
# Host-side failures to convert the guest's result (the code itself ran). Matched on pydeno's own
# message prefixes, never on a JavaScriptError, which is the guest's.
_RESULT_ERROR = re.compile(
    r"Evaluation failed: (?:Cannot serialize|Date value out of range)"
    r"|JsFunction cannot cross the isolation boundary"
)
# The parent's own encoder refusing the eval command: the code is over the frame cap once escaped.
_FRAME_CAP = re.compile(r"message of \d+ bytes exceeds the \d+ byte frame cap")
# Room for the rest of the eval command around the code.
_FRAME_OVERHEAD = 1024


def parse_size(text: str) -> int:
    """``"256M"`` -> 268435456. Units are binary: K, M, G, T, optionally followed by ``B`` or
    ``iB``; a bare number or ``B`` is bytes. At most `MAX_MEMORY`."""
    found = _SIZE.fullmatch(text.strip())
    if found is None or int(found.group(1)) <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid size {text!r} (use bytes or a K/M/G/T suffix, e.g. 256M)"
        )
    unit = found.group(2).lower() if found.group(2) else None
    value = int(found.group(1)) * _UNITS[unit]
    if value > MAX_MEMORY:
        raise argparse.ArgumentTypeError(f"size {text!r} is over the maximum of 1T")
    return value


def _timeout(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= MAX_TIMEOUT):
        raise argparse.ArgumentTypeError(
            f"must be a number of seconds above 0 and at most {MAX_TIMEOUT:g}, got {text!r}"
        )
    return value


def _unsafe(ch: str) -> bool:
    return ord(ch) in _INVISIBLE or unicodedata.category(ch) in _UNSAFE_CATEGORIES


def inert_text(text: str) -> str:
    """``text`` with every character a terminal could act on (or hide text with) replaced by
    ``?``. Tab and newline are kept."""
    return _CANDIDATE.sub(lambda m: "?" if _unsafe(m.group()) else m.group(), text)


def _escape(found: re.Match[str]) -> str:
    ch = found.group()
    if not _unsafe(ch):
        return ch
    code = ord(ch)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def to_json(value: Any) -> str:
    """``value`` as JSON that is safe to print: non-ASCII text stays readable, but the characters
    `inert_text` replaces are escaped as ``\\uXXXX``, so the output is still lossless."""
    # Outside strings json.dumps emits only ASCII punctuation, digits and letters, so every match
    # is inside a string, where a \u escape is valid.
    return _CANDIDATE.sub(_escape, json.dumps(value, ensure_ascii=False))


def _large_ints_as_strings(value: Any) -> Any:
    """Integers a JSON reader cannot hold exactly (past 2**53 - 1: a BigInt, or a Number that has
    already lost precision) as decimal strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _MAX_SAFE_INT else value
    if isinstance(value, list):
        return [_large_ints_as_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _large_ints_as_strings(v) for k, v in value.items()}
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pydeno",
        description=(
            "Evaluate JavaScript in a sandboxed pydeno worker and print the result as JSON. "
            "Code comes from the CODE argument, -c, -f FILE, or stdin ('-', or when stdin is "
            "not a terminal). Promises are awaited."
        ),
        epilog=(
            "exit codes: 0 ok, 1 JavaScript error, 2 usage, 3 timeout, "
            "4 OS sandbox unavailable, 5 other runtime failure, "
            "6 result cannot be converted to JSON"
        ),
    )
    parser.add_argument(
        "code",
        nargs="?",
        help="JavaScript to evaluate ('-' reads it from stdin)",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-c", "--command", dest="command", help="JavaScript to evaluate"
    )
    source.add_argument(
        "-f", "--file", dest="file", help="read the JavaScript from this file"
    )
    parser.add_argument(
        "--timeout",
        type=_timeout,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"deadline for the evaluation (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-memory",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="kill the worker above this resident memory, e.g. 256M (default 1G, at most 1T)",
    )
    sandbox = parser.add_mutually_exclusive_group()
    sandbox.add_argument(
        "--sandbox",
        choices=("require", "auto"),
        default="require",
        help=(
            "require: refuse to run without the complete OS sandbox (default); "
            "auto: apply whatever this platform offers"
        ),
    )
    sandbox.add_argument(
        "--no-sandbox",
        action="store_true",
        help="run the worker without the OS sandbox (prints a warning)",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        dest="raw",
        action="store_false",
        help="print the result as JSON (default)",
    )
    output.add_argument(
        "--raw",
        dest="raw",
        action="store_true",
        help="print a string result as plain text instead of a JSON string",
    )
    parser.set_defaults(raw=False)
    return parser


def _decode(data: bytes, where: str) -> str | None:
    """Bounded input as text, or None (after saying why) when it is too large or not UTF-8."""
    if len(data) > MAX_CODE_BYTES:
        print(
            f"pydeno: {where} is larger than {MAX_CODE_BYTES} bytes (16 MiB), "
            "the most the worker accepts",
            file=sys.stderr,
        )
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"pydeno: {where} is not valid UTF-8: {exc}", file=sys.stderr)
        return None


def _read_code(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    given = [x for x in (args.code, args.command, args.file) if x is not None]
    if len(given) > 1:
        parser.error("give the code once: as CODE, with -c, or with -f")
    code: str | None
    if args.command is not None:
        code = args.command
    elif args.file is not None:
        where = inert_text(args.file)
        try:
            # Bounded: a FIFO or /dev/stdin is fine, an endless one stops at the cap.
            with open(args.file, "rb") as source:
                code = _decode(source.read(MAX_CODE_BYTES + 1), where)
        except OSError as exc:
            print(
                f"pydeno: cannot read {where}: {inert_text(str(exc))}", file=sys.stderr
            )
            return None
    elif args.code == "-" or (args.code is None and not sys.stdin.isatty()):
        code = _decode(sys.stdin.buffer.read(MAX_CODE_BYTES + 1), "stdin")
    elif args.code is not None:
        code = args.code
    else:
        parser.error("no code given (pass CODE, -c, -f FILE, or pipe it on stdin)")
    if code is None:
        return None
    try:
        code.encode("utf-8")
    except UnicodeEncodeError:
        # Bytes that are not UTF-8 on the command line arrive as lone surrogates.
        print("pydeno: the code is not valid UTF-8", file=sys.stderr)
        return None
    if _encoded_size(code) > MAX_CODE_BYTES:
        _too_large()
        return None
    if not code.strip():
        parser.error("no code given (pass CODE, -c, -f FILE, or pipe it on stdin)")
    return code


def _encoded_size(code: str) -> int:
    """About what the eval command carrying ``code`` takes on the wire: the code as a JSON string
    (a newline or a control character takes 2 to 6 bytes) plus the rest of the command."""
    return len(json.dumps(code, ensure_ascii=False).encode("utf-8")) + _FRAME_OVERHEAD


def _too_large() -> None:
    print(
        f"pydeno: the code is too large: the worker accepts {MAX_CODE_BYTES} bytes (16 MiB) "
        "once encoded as a JSON string, where a newline or a control character takes 2 to 6 bytes",
        file=sys.stderr,
    )


def _print_console(level: str, values: list[Any]) -> None:
    from ._result import format_console_arg

    line = inert_text(" ".join(format_console_arg(v) for v in values))
    stream = sys.stdout if level in ("log", "info", "debug") else sys.stderr
    print(line, file=stream, flush=True)


def _render(value: Any, raw: bool) -> str | None:
    from ._pydeno import JsUndefined
    from ._result import to_jsonable

    if isinstance(value, JsUndefined):
        return None
    if raw and isinstance(value, str):
        return inert_text(value)
    return to_json(_large_ints_as_strings(to_jsonable(value)))


def _unconvertible(detail: str) -> int:
    print(
        f"pydeno: the result cannot be converted to JSON: {inert_text(detail)}",
        file=sys.stderr,
    )
    return EXIT_RESULT


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with ``argv`` (default ``sys.argv[1:]``) and return the exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    code = _read_code(args, parser)
    if code is None:
        return EXIT_USAGE

    import asyncio

    from ._errors import classify_error
    from ._isolated import IsolatedRuntime
    from ._pydeno import JavaScriptError, RuntimeConfig

    sandbox = "off" if args.no_sandbox else args.sandbox
    if sandbox == "off":
        print(
            "pydeno: warning: --no-sandbox: the worker runs WITHOUT the OS sandbox; "
            "only run code you trust",
            file=sys.stderr,
        )
    options: dict[str, Any] = {"sandbox": sandbox}
    if args.max_memory is not None:
        options["max_memory"] = args.max_memory

    async def run() -> Any:
        rt = IsolatedRuntime(RuntimeConfig(on_console=_print_console), **options)
        try:
            return await rt.eval_async(code, timeout=args.timeout)
        finally:
            rt.close()

    try:
        value = asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - every failure becomes an exit code
        if not isinstance(exc, JavaScriptError) and _RESULT_ERROR.match(str(exc)):
            return _unconvertible(str(exc))
        info = classify_error(exc)
        if info.kind == "invalid_input" and _FRAME_CAP.fullmatch(str(exc)):
            _too_large()
            return EXIT_USAGE
        print(f"pydeno: {info.kind}: {inert_text(str(exc))}", file=sys.stderr)
        if info.kind in ("sandbox_unavailable", "limits_unmeasurable"):
            print(
                "pydeno: the OS sandbox cannot be applied here; see "
                "`python -c 'import pydeno; print(pydeno.sandbox_status())'`, "
                "or pass --sandbox auto / --no-sandbox to run with less protection",
                file=sys.stderr,
            )
        return _KIND_EXIT.get(info.kind, EXIT_RUNTIME)
    try:
        text = _render(value, args.raw)
    except (TypeError, ValueError, RecursionError) as exc:
        return _unconvertible(str(exc))
    if text is not None:
        print(text)
    return EXIT_OK
