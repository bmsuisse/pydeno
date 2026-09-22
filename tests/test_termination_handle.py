"""Proof that the TerminationHandle fix works: the exact watchdog-thread test
that broke unpatched pydeno. See `docs/contributing/upstream-divergence.md`
section 1 for the root cause. Run directly with the patched interpreter:

    python tests/test_termination_handle.py

Not wired into pytest/CI -- this is a standalone repro script by design, a
regression check for that one bug, not a full suite.
"""

from __future__ import annotations

import sys
import threading
import time

import pydeno


def test_fixed_termination_handle_kills_runaway_loop() -> None:
    """Watchdog thread calls TerminationHandle.terminate() after 2s on a
    runtime stuck in `while(true){}`. Must not panic, must actually stop the
    loop, and eval() must return/raise promptly."""
    runtime = pydeno.Runtime()
    handle = runtime.termination_handle()

    watchdog_error: list[BaseException] = []

    def watchdog() -> None:
        time.sleep(2.0)
        try:
            handle.terminate()
        except BaseException as exc:  # noqa: BLE001 - we want to see ANY panic/exception
            watchdog_error.append(exc)

    t = threading.Thread(target=watchdog, daemon=True)
    start = time.monotonic()
    t.start()

    raised: Exception | None = None
    try:
        runtime.eval("while(true){}")
    except Exception as exc:  # noqa: BLE001 - runtime.eval is expected to raise
        raised = exc

    elapsed = time.monotonic() - start
    t.join(timeout=5)

    assert not watchdog_error, f"watchdog thread raised: {watchdog_error!r}"
    assert raised is not None, "eval() should have raised after termination"
    assert elapsed < 10.0, f"eval() took too long to return: {elapsed:.2f}s"
    assert handle.is_terminated(), "handle should report terminated=True"

    print(f"    eval() returned after {elapsed:.2f}s, raised={raised!r}")
    runtime.close()


def test_regression_basic_eval() -> None:
    runtime = pydeno.Runtime()
    assert runtime.eval("1 + 1") == 2
    assert runtime.eval("'a' + 'b'") == "ab"
    runtime.close()


def test_regression_bind_function() -> None:
    runtime = pydeno.Runtime()
    calls = []

    def host_fn(x):
        calls.append(x)
        return x * 2

    runtime.bind_function("hostFn", host_fn)
    result = runtime.eval("hostFn(21)")
    assert result == 42, result
    assert calls == [21]
    runtime.close()


def test_regression_fs_network_blocked() -> None:
    runtime = pydeno.Runtime()
    for snippet in ("typeof require", "typeof process", "typeof fetch"):
        result = runtime.eval(snippet)
        assert result == "undefined", f"{snippet} -> {result!r}"
    runtime.close()


def test_regression_eval_async_timeout() -> None:
    import asyncio

    async def _run() -> str:
        runtime = pydeno.Runtime()
        try:
            await runtime.eval_async("while(true){}", timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            return repr(exc)
        finally:
            runtime.close()
        return "NO_ERROR_RAISED"

    start = time.monotonic()
    outcome = asyncio.run(_run())
    elapsed = time.monotonic() - start
    assert outcome != "NO_ERROR_RAISED", "eval_async should have raised on timeout"
    assert elapsed < 10.0, f"eval_async timeout took too long: {elapsed:.2f}s"
    print(f"    eval_async(timeout=2.0) raised after {elapsed:.2f}s: {outcome}")


def main() -> int:
    tests = [
        test_regression_basic_eval,
        test_regression_bind_function,
        test_regression_fs_network_blocked,
        test_regression_eval_async_timeout,
        test_fixed_termination_handle_kills_runaway_loop,
    ]
    failures = 0
    for fn in tests:
        print(f"[RUN ] {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc!r}")
        else:
            print(f"[PASS] {fn.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
