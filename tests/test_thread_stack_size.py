"""Regression tests for the debug-build stack overflow (review finding M2).

Before the fix, `rt.eval("[" * 80 + "]" * 80)` killed the whole host process
with SIGBUS on a `make build-dev` (unoptimized) build. The cause was that
neither the runtime thread (`src/runtime/runner.rs`) nor the isolate-pool
worker (`src/runtime/pool.rs`) set `.stack_size()`, so both ran on Rust's
2 MiB default -- while the recursive V8->JSValue serializer descends once per
nesting level up to `MAX_JS_DEPTH = 100` and consumes a measured **28_336
bytes per level** in a debug build (~2.83 MB for a full descent).

A release build passed the same input because `-O` shrinks those frames, which
is why these tests matter specifically in **debug** builds: they are the only
place the library's "a result or a catchable exception, never a host crash"
invariant was actually being violated.

Two deliberate design choices:

1. Every case runs in a **fresh subprocess**. A stack overflow is a signal,
   not an exception -- run in-process, a regression would once again kill the
   pytest session mid-run and take every later test file with it silently
   (which is exactly how M2 hid). In a subprocess it is an ordinary assertion
   failure with the signal number in the message.
2. Nothing here is a Hypothesis test and nothing is behind a marker, so these
   run in every configuration, including the fuzz-disabled one.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Matches `MAX_JS_DEPTH` in `src/runtime/js_value.rs`. Deep enough to force a
# full-depth recursion in the serializer, so the stack budget is genuinely
# exercised rather than nibbled at.
MAX_JS_DEPTH = 100


def _run_child(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=300,
    )


def _assert_child_survived(completed: subprocess.CompletedProcess[str]) -> None:
    """The invariant: exit 0, and in particular never death by signal."""
    if completed.returncode < 0:
        pytest.fail(
            f"host process died with signal {-completed.returncode} "
            f"(stack overflow regression -- see RUNTIME_THREAD_STACK_SIZE):\n"
            f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
        )
    assert completed.returncode == 0, (
        f"child exited {completed.returncode}:\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )


@pytest.mark.parametrize("depth", [80, MAX_JS_DEPTH, 2 * MAX_JS_DEPTH, 400])
def test_deeply_nested_literals_never_kill_the_host_process(depth: int) -> None:
    """The exact M2 repro, across the depths that used to be fatal.

    Depth 80 was the smallest observed SIGBUS; 200 and 400 are past
    `MAX_JS_DEPTH`, where the serializer must still descend 100 levels before
    the depth guard refuses -- so they cost just as much stack as depth 100
    and were fatal too.
    """
    completed = _run_child(f"""
        from pydeno import Runtime

        with Runtime() as rt:
            for source in ("[" * {depth} + "]" * {depth},
                           "{{a:" * {depth} + "1" + "}}" * {depth}):
                try:
                    rt.eval(source)
                except Exception as exc:  # noqa: BLE001 -- any refusal is fine
                    print(f"refused: {{type(exc).__name__}}")
        print("survived")
    """)
    _assert_child_survived(completed)
    assert "survived" in completed.stdout


def test_the_depth_guard_refuses_before_recursing_past_the_limit() -> None:
    """The stack fix is only sufficient because the guard is genuinely *before*
    the recursion.

    `value_to_js_value_internal` calls `tracker.enter()?` as its very first
    statement, so a depth-N payload costs at most `min(N, max_depth)` frames.
    This pins that: with a low limit, a very deep payload must be refused with
    the configured number in the message, which can only happen if the guard
    ran on the way down.
    """
    completed = _run_child("""
        from pydeno import Runtime, RuntimeConfig

        with Runtime(RuntimeConfig(max_serialization_depth=5)) as rt:
            source = "let a={};let c=a;" \\
                     "for(let i=0;i<5000;i++){c.n={};c=c.n};a"
            try:
                rt.eval(source)
            except Exception as exc:
                print(f"refused: {exc}")
            else:
                raise AssertionError("depth 5000 accepted against a limit of 5")
        print("survived")
    """)
    _assert_child_survived(completed)
    assert "refused:" in completed.stdout
    # The configured limit, not a dependency's internal constant, is what
    # stopped it.
    assert "5" in completed.stdout


def test_a_deeply_nested_tool_argument_never_kills_the_host_process() -> None:
    """The inbound (JS->Python) direction runs on the same runtime thread."""
    completed = _run_child("""
        from pydeno import Runtime

        with Runtime() as rt:
            rt.bind_function("sink", lambda v: "ok")
            source = "let a={};let c=a;" \\
                     "for(let i=0;i<400;i++){c.n={};c=c.n};sink(a)"
            try:
                rt.eval(source)
            except Exception as exc:  # noqa: BLE001
                print(f"refused: {type(exc).__name__}")
        print("survived")
    """)
    _assert_child_survived(completed)
    assert "survived" in completed.stdout


def test_a_zero_column_syntax_error_does_not_kill_the_runtime_thread() -> None:
    """Pins the second thing the debug test suite found once it could run past
    test 46.

    `deno_core` 0.409.0 subtracts 1 from a V8-reported line number that can be
    zero (`source_map.rs:105`), so an unterminated template literal panicked
    the runtime thread in any build with overflow checks on -- turning a
    catchable `JavaScriptError` into `RuntimeError: Failed to receive eval
    result`. See the `[profile.dev.package."*"]` note in `Cargo.toml`.

    Deterministic on purpose: the fuzz suite only finds this input by chance,
    which would make the debug job flake red rather than fail honestly.
    """
    completed = _run_child(r"""
        from pydeno import Runtime

        with Runtime() as rt:
            for source in ("`\\", "`${", "'"):
                try:
                    rt.eval(source)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    assert "Failed to receive eval result" not in message, (
                        f"runtime thread died on {source!r}: {message}"
                    )
                    print(f"refused: {type(exc).__name__}")
                else:
                    raise AssertionError(f"{source!r} was accepted")
            # The runtime must still be usable afterwards.
            assert rt.eval("1 + 1") == 2
        print("survived")
    """)
    _assert_child_survived(completed)
    assert "survived" in completed.stdout


def test_a_deep_python_argument_from_a_small_thread_does_not_kill_the_host() -> None:
    """The one recursion `RUNTIME_THREAD_STACK_SIZE` cannot cover.

    `python_to_js_value` runs on whichever thread *called* -- it has to, it
    needs the caller's GIL and its objects -- so the 16 MiB reservation on the
    runtime thread does nothing for it. A `threading.Thread` gets 512 KB on
    macOS and `pydeno` cannot change that from inside the call.

    At the default `max_serialization_depth` of 100 there is roughly 9x of
    headroom: measured on a debug build, a 512 KB caller thread survives depth
    900 and dies at 1000 (~512 bytes per frame, far cheaper than the V8-side
    serializer's 28 KB). This pins the default case, which is the one every
    user is in, so a frame-size regression in the converter surfaces here
    rather than as a SIGBUS in somebody's worker thread.

    Raising `max_serialization_depth` past ~900 *and* converting from a small
    thread is still fatal and is documented as such on the setting; the real
    fix is to move the conversion onto the runtime thread, which is a
    GIL-crossing change and not this commit's business.
    """
    completed = _run_child(f"""
        import threading
        from pydeno import Runtime

        def deep(n):
            obj = {{"leaf": 1}}
            for _ in range(n):
                obj = {{"n": obj}}
            return obj

        def body():
            with Runtime() as rt:
                f = rt.eval("(v) => 1")
                for depth in ({MAX_JS_DEPTH}, 2 * {MAX_JS_DEPTH}, 400):
                    try:
                        f(deep(depth))
                    except Exception as exc:  # noqa: BLE001
                        print(f"refused: {{type(exc).__name__}}")

        # A plain threading.Thread, i.e. the 512 KB stack, not the main
        # thread's 8 MB.
        worker = threading.Thread(target=body)
        worker.start()
        worker.join()
        print("survived")
    """)
    _assert_child_survived(completed)
    assert "survived" in completed.stdout
