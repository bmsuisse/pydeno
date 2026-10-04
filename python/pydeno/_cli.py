"""The ``pydeno`` command: evaluate JavaScript in a sandboxed worker and print the result as JSON.

``python -m pydeno '1 + 2'`` and the ``pydeno`` console script both land in `main`. Input runs in
an `IsolatedRuntime` with ``sandbox="require"`` (a worker process under the OS sandbox, ``--jitless``
V8, a memory cap and a deadline), never in the in-process `Runtime`: a one-liner is the kind of
place untrusted code ends up. ``--no-sandbox`` keeps the worker process and its limits but drops
the OS sandbox, and says so on stderr.

Only the standard library is imported at module level; pydeno itself is imported inside `main`,
after argument parsing, so ``pydeno --help`` and usage errors do not start anything.

Exit codes (see ``docs/guides/cli.md``):

* 0: success
* 1: the JavaScript threw or failed to compile
* 2: usage error (bad arguments, unreadable file)
* 3: timeout (the deadline or the worker's CPU cap)
* 4: the OS sandbox is unavailable here (``sandbox="require"`` refused to start)
* 5: any other runtime failure (memory limit, worker crash, ...)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

__all__ = ["main", "parse_size"]

EXIT_OK = 0
EXIT_JS_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_NO_SANDBOX = 4
EXIT_RUNTIME = 5

_KIND_EXIT = {
    "js_error": EXIT_JS_ERROR,
    "timeout": EXIT_TIMEOUT,
    "cpu_limit": EXIT_TIMEOUT,
    "sandbox_unavailable": EXIT_NO_SANDBOX,
    "limits_unmeasurable": EXIT_NO_SANDBOX,
}

DEFAULT_TIMEOUT = 30.0

_SIZE = re.compile(r"(\d+)\s*([kmgt]?)(i?b?)", re.IGNORECASE)
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(text: str) -> int:
    """``"256M"`` -> 268435456. Units are binary (K, M, G, T; ``MB``/``MiB`` accepted too)."""
    found = _SIZE.fullmatch(text.strip())
    if found is None or int(found.group(1)) <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid size {text!r} (use bytes or a K/M/G suffix, e.g. 256M)"
        )
    return int(found.group(1)) * _UNITS[found.group(2).lower()]


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be a positive number, got {text!r}")
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
            "4 OS sandbox unavailable, 5 other runtime failure"
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
        type=_positive_float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"deadline for the evaluation (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-memory",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="kill the worker above this resident memory, e.g. 256M (default 1G)",
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


def _read_code(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    given = [x for x in (args.code, args.command, args.file) if x is not None]
    if len(given) > 1:
        parser.error("give the code once: as CODE, with -c, or with -f")
    if args.command is not None:
        code = args.command
    elif args.file is not None:
        try:
            code = Path(args.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"pydeno: cannot read {args.file}: {exc}", file=sys.stderr)
            return None
    elif args.code == "-" or (args.code is None and not sys.stdin.isatty()):
        code = sys.stdin.read()
    else:
        code = args.code
    if not code or not code.strip():
        parser.error("no code given (pass CODE, -c, -f FILE, or pipe it on stdin)")
    return code


def _print_console(level: str, values: list[Any]) -> None:
    from ._result import format_console_arg

    line = " ".join(format_console_arg(v) for v in values)
    stream = sys.stdout if level in ("log", "info", "debug") else sys.stderr
    print(line, file=stream, flush=True)


def _render(value: Any, raw: bool) -> str | None:
    from ._pydeno import JsUndefined
    from ._result import to_jsonable

    if isinstance(value, JsUndefined):
        return None
    if raw and isinstance(value, str):
        return value
    return json.dumps(to_jsonable(value), ensure_ascii=False)


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
    from ._pydeno import RuntimeConfig

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
        text = _render(value, args.raw)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - every failure becomes an exit code
        info = classify_error(exc)
        print(f"pydeno: {info.kind}: {exc}", file=sys.stderr)
        if info.kind in ("sandbox_unavailable", "limits_unmeasurable"):
            print(
                "pydeno: the OS sandbox cannot be applied here; see "
                "`python -c 'import pydeno; print(pydeno.sandbox_status())'`, "
                "or pass --sandbox auto / --no-sandbox to run with less protection",
                file=sys.stderr,
            )
        return _KIND_EXIT.get(info.kind, EXIT_RUNTIME)
    if text is not None:
        print(text)
    return EXIT_OK
