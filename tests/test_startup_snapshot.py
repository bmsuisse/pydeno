"""The built-in V8 startup snapshot (`build.rs`) changes how fast a runtime starts, nothing else.

It carries the set of built-in objects the bridge refuses to bind onto, collected once at build
time instead of in every runtime. Each case runs in a subprocess: whether a runtime starts from the
snapshot is a process-wide fact (V8 flags, `PYDENO_STARTUP_SNAPSHOT`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from pydeno import _pydeno
from pydeno._isolated import _HARDENING_V8_FLAGS

_PROBE = textwrap.dedent(
    """
    import json
    from pydeno import Runtime, RuntimeConfig
    from pydeno import _pydeno

    rt = Runtime(RuntimeConfig())
    out = {
        "snapshot": _pydeno._startup_snapshot_bytes(),
        # The hand-over global is gone before any guest code can see it.
        "handover": rt.eval("typeof globalThis.__pydeno_intrinsics"),
        "sum": rt.eval("1 + 1"),
    }
    # Built-ins the bridge must refuse as a namespace, and one object it must still accept.
    for name, expr in {
        "global": "globalThis",
        "object-prototype": "Object.prototype",
        "segments": "Object.getPrototypeOf(new Intl.Segmenter().segment('a'))",
        "own": "{}",
    }.items():
        rt.eval(f"globalThis.tools = {expr}; 0")
        try:
            rt.bind_object("tools", {"zz_host_tool": lambda: 1})
            out[name] = "bound"
        except Exception:
            out[name] = "refused"
        rt.eval("delete globalThis.tools; 0")
    print(json.dumps(out))
    """
)


def _probe(
    env: dict[str, str] | None = None, flags: list[str] | None = None
) -> dict[str, object]:
    import json

    code = _PROBE
    if flags is not None:
        code = (
            "from pydeno import _pydeno; "
            f"assert _pydeno._set_v8_flags({flags!r}) == []\n" + code
        )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


_SAME = {
    "handover": "undefined",
    "sum": 2,
    "global": "refused",
    "object-prototype": "refused",
    "segments": "refused",
    "own": "bound",
}


def _behaviour(result: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in result.items() if k != "snapshot"}


def test_a_default_worker_start_uses_the_snapshot_when_the_build_made_one() -> None:
    result = _probe(flags=list(_pydeno._startup_snapshot_flags()))
    assert _behaviour(result) == _SAME
    # 0 only for a build made without one (cross compiled, or PYDENO_NO_STARTUP_SNAPSHOT=1).
    assert isinstance(result["snapshot"], int) and result["snapshot"] >= 0


def test_the_opt_out_starts_without_it_and_behaves_the_same() -> None:
    result = _probe(
        {"PYDENO_STARTUP_SNAPSHOT": "0"}, flags=list(_pydeno._startup_snapshot_flags())
    )
    assert result["snapshot"] == 0
    assert _behaviour(result) == _SAME


@pytest.mark.parametrize(
    "flags",
    [
        None,  # a plain in-process Runtime gives V8 no flags
        [*_pydeno._startup_snapshot_flags(), "--no-opt"],
        ["--jitless"],
        ["--enable-experimental-regexp-engine-on-excessive-backtracks"],
    ],
)
def test_other_v8_flags_rule_the_snapshot_out_and_behave_the_same(
    flags: list[str] | None,
) -> None:
    # V8 refuses a snapshot under flags other than the ones it was made with.
    result = _probe(flags=flags)
    assert result["snapshot"] == 0
    assert _behaviour(result) == _SAME


def test_the_snapshot_is_made_under_the_flags_of_a_default_worker() -> None:
    assert list(_pydeno._startup_snapshot_flags()) == [
        "--jitless",
        *_HARDENING_V8_FLAGS,
    ]


def test_an_isolated_worker_starts_from_the_snapshot() -> None:
    # The worker's environment is empty, so this is the default path, end to end.
    code = (
        "from pydeno import IsolatedRuntime\n"
        "with IsolatedRuntime(sandbox='auto') as rt:\n"
        "    print(rt.eval('1 + 1'))\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "2"
