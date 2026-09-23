"""Every timeout path raises `RuntimeTimeout`, promptly, and leaves the runtime usable.

One row per path that can run JS under a deadline. Through 0.4.1 two of them
were broken while the rest were not, and nothing tested them side by side:

- a JS function call whose JS was still *running* at its deadline raised
  `JavaScriptError: Uncaught null` -- V8 reports a terminated `func.call` as a
  null exception, which the watchdog result mapping did not recognise;
- `fn.call_async(...)`, the awaited half of a `fn(...)` that returned a
  promise, and a sync `fn(timeout=...)` on a runtime with no
  `RuntimeConfig.timeout` armed no watchdog at all, so JS spinning there hung
  forever.

A new path belongs in this table. Each row runs in a subprocess so that a
regression to "hangs" fails the row instead of hanging the suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

TIMEOUT = 0.3
# Generous for a debug build on a loaded CI box, still far below "hung".
BOUND = 1.0

_HARNESS = """
import asyncio, time
from pydeno import Runtime, RuntimeConfig, RuntimeTimeout

async def slow_host_call():
    await asyncio.sleep(0.05)
    return 1

async def main():
    rt = Runtime(RuntimeConfig({config}))
    rt.bind_function("slowHostCall", slow_host_call)
    rt.add_static_module("spin", "while (true) {{}}\\nexport const x = 1;")
{setup}
    start = time.monotonic()
    try:
{call}
    except RuntimeTimeout as exc:
        assert isinstance(exc, RuntimeError), type(exc).__mro__
        elapsed = time.monotonic() - start
        assert elapsed < {bound}, f"raised after {{elapsed:.2f}}s"
    else:
        raise AssertionError("no RuntimeTimeout")
    # Still usable, sync and async, and closes.
    assert rt.eval("1 + 1") == 2
    assert await rt.eval_async("Promise.resolve(3)") == 3
    rt.close()
    print("OK")

asyncio.run(main())
"""

_SPIN = "while (true) {}"

# id -> (RuntimeConfig kwargs, setup lines, the timed-out call)
CASES = {
    "eval": (f"timeout={TIMEOUT}", "", f'rt.eval("{_SPIN}")'),
    "eval_async": (
        "",
        "",
        f'await rt.eval_async("{_SPIN}", timeout={TIMEOUT})',
    ),
    "eval_module": (f"timeout={TIMEOUT}", "", 'rt.eval_module("spin")'),
    "eval_module_async": (
        "",
        "",
        f'await rt.eval_module_async("spin", timeout={TIMEOUT})',
    ),
    "sync_call[timeout=]": (
        "",
        f'fn = rt.eval("(() => {{ {_SPIN} }})")',
        f"fn(timeout={TIMEOUT})",
    ),
    "sync_call[config]": (
        f"timeout={TIMEOUT}",
        f'fn = rt.eval("(() => {{ {_SPIN} }})")',
        "fn()",
    ),
    # Returns a promise, then spins in the microtask checkpoint that follows.
    "sync_call[spins-in-microtask]": (
        "",
        f'fn = rt.eval("(async () => {{ await 0; {_SPIN} }})")',
        f"fn(timeout={TIMEOUT})",
    ),
    "async_call[spins-before-await]": (
        "",
        f'fn = rt.eval("(async () => {{ {_SPIN} }})")',
        f"await fn.call_async(timeout={TIMEOUT})",
    ),
    "async_call[spins-after-await]": (
        "",
        f'fn = rt.eval("(async () => {{ await 0; {_SPIN} }})")',
        f"await fn.call_async(timeout={TIMEOUT})",
    ),
    "async_call[config]": (
        f"timeout={TIMEOUT}",
        f'fn = rt.eval("(async () => {{ await 0; {_SPIN} }})")',
        "await fn.call_async()",
    ),
    # The sync call returns a pending promise and is resumed as an async job
    # on the original clock; the gate opens 50ms in, during that job, and the
    # continuation spins. (The gate's host op is started by an `eval_async`,
    # since a host async op needs the asyncio context one establishes.)
    "resumed_call": (
        "",
        'await rt.eval_async("globalThis.gate = slowHostCall(); 0")\n'
        f'fn = rt.eval("(async () => {{ await gate; {_SPIN} }})")',
        f"await fn(timeout={TIMEOUT})",
    ),
}


@pytest.mark.parametrize(("config", "setup", "call"), CASES.values(), ids=CASES.keys())
def test_timeout_path(config: str, setup: str, call: str) -> None:
    source = _HARNESS.format(
        config=config,
        setup=textwrap.indent(setup, " " * 4),
        call=textwrap.indent(call, " " * 8),
        bound=BOUND,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", source],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("hung: the call (or close()) never returned")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("OK"), proc.stdout + proc.stderr
