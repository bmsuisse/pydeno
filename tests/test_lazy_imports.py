"""PEP 810 lazy imports (Python 3.15): the sync API must not pay for asyncio and friends.

`__lazy_modules__` is a plain list, so on 3.10-3.14 the imports stay eager and the tests expect
that. The worker's own imports are never lazy (it applies its sandbox after importing).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


LAZY = sys.version_info >= (3, 15)

HEAVY = (
    "asyncio",
    "concurrent.futures",
    "inspect",
    "subprocess",
    "tempfile",
    "logging",
)


def _run(code: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_importing_the_sync_api_loads_heavy_stdlib_only_where_imports_are_eager() -> (
    None
):
    out = _run(
        f"""
        import sys, pydeno
        pydeno.IsolatedRuntime
        print([m for m in {HEAVY!r} if m in sys.modules])
        """
    )
    # 3.15 honours `__lazy_modules__`; older versions ignore the plain list and import eagerly.
    assert out == "[]" if LAZY else out != "[]"


def test_lazy_modules_resolve_on_first_use() -> None:
    out = _run(
        """
        import sys, pydeno
        with pydeno.IsolatedRuntime() as rt:
            print(rt.eval("6 * 7"))
        """
    )
    assert out == "42"


def test_the_worker_imports_stay_eager() -> None:
    """The worker must have imported everything before it applies its sandbox."""
    from pathlib import Path

    import pydeno

    for name in ("_worker", "_sandbox", "_wire", "_wasm", "_awaitable"):
        source = (Path(pydeno.__file__).parent / f"{name}.py").read_text()
        assert "__lazy_modules__" not in source, name
