"""Regression tests for the stack-headroom check (`LimitTracker::enter`,
`src/runtime/js_value.rs`).

Before this fix, `LimitTracker` only counted *logical* nesting levels against
`max_serialization_depth`. That configuration knob is explicitly supported up
to any value a caller chooses (`RuntimeConfig(max_serialization_depth=...)`),
and raising it far enough let a deeply nested value exhaust the runtime
thread's *real* native/V8 stack budget before the depth counter ever
objected -- corrupting the process instead of raising a catchable Python
exception.

Confirmed on this checkout, reproducible on `main` @ a51a0a4 (pre-fix): with
`max_serialization_depth=10**6` and no headroom check, serializing a plain
object chain (`{n: {n: {n: ...}}}`) triggers a real, uncatchable V8 fatal
error (`Check failed: IsOnCentralStack()`, aborting the whole process) at
depth ~40 in an `unoptimized + debuginfo` build, and at a much deeper but
still real boundary (~depth 1100-1200) in a `release` build.

`LimitTracker::enter` now also tracks how much native stack has been consumed
since a per-runtime-thread anchor recorded before `JsRuntime::new`, and
raises a catchable `RuntimeError` well before that real boundary in either
profile -- see `STACK_HEADROOM_BYTES` for the empirical tuning. Every case
here runs in a subprocess: a regression is a process death, not an
assertion, and must not be able to take the rest of the pytest session with
it (the same reasoning as `tests/test_thread_stack_size.py`).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_CHAIN_SCRIPT = textwrap.dedent(
    """
    import sys
    from pydeno import Runtime, RuntimeConfig

    depth = {depth}
    js = (
        "let a = {{}}; let cur = a; "
        "for (let i = 0; i < " + str(depth) + "; i++) {{ cur.n = {{}}; cur = cur.n; }} "
        "a;"
    )
    with Runtime(RuntimeConfig(max_serialization_depth=10**6)) as rt:
        try:
            rt.eval(js)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is the point
            print("CLEAN_ERROR:" + type(exc).__name__ + ":" + str(exc))
            sys.exit(0)
        else:
            print("NO_ERROR")
            sys.exit(0)
        # A crash (SIGBUS/SIGABRT/V8 fatal) shows up as a nonzero/negative
        # returncode to the parent process, never reaches either branch above.
    """
)


def _run_chain_depth(depth: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHAIN_SCRIPT.format(depth=depth)],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("depth", [36, 500, 5000])
def test_deeply_nested_object_never_crashes_the_process(depth: int) -> None:
    """With the depth limit effectively disabled, a value nested this deep
    must never take the process down (SIGBUS/SIGABRT/a V8 fatal abort) --
    the process must exit cleanly either way, whether that means the
    conversion completed or the headroom check rejected it.

    Note on the three depths: `640 KiB` of headroom (`STACK_HEADROOM_BYTES`)
    is tuned against the *debug* build's much larger per-frame cost, where it
    trips around depth ~22 -- comfortably before debug's own real, uncatchable
    crash boundary of ~40 confirmed on this checkout. An optimized release
    build's frames are far smaller, so its real crash boundary is much deeper
    (~1100-1200 on this checkout) and depths 36/500 complete without ever
    approaching it -- there is nothing to reject at that depth in release,
    and "no error" is the correct outcome there, not a gap in the check.
    Depth 5000 is deep enough to matter in both profiles (past release's
    ~1100-1200 real boundary too), so it is the one depth asserted to
    actually trip the headroom check in every profile, below.
    """
    completed = _run_chain_depth(depth)

    assert completed.returncode == 0, (
        f"process died at depth {depth} (returncode={completed.returncode}); "
        f"stderr tail:\n{completed.stderr[-2000:]}"
    )
    assert "CLEAN_ERROR:" in completed.stdout or "NO_ERROR" in completed.stdout, (
        f"expected either a clean, catchable error or a completed conversion "
        f"at depth {depth}, got: {completed.stdout!r} / "
        f"stderr tail: {completed.stderr[-500:]!r}"
    )


def test_headroom_check_actually_trips_past_every_profiles_real_boundary() -> None:
    """Depth 5000 is deeper than the real (uncatchable-crash) boundary
    measured in *either* build profile on this checkout (~40 in debug, ~1100
    -1200 in release), so the headroom check must be the thing that stops it
    -- a plain completion here would mean the check silently stopped
    covering release builds."""
    completed = _run_chain_depth(5000)
    assert completed.returncode == 0, (
        f"process died (returncode={completed.returncode}); "
        f"stderr tail:\n{completed.stderr[-2000:]}"
    )
    assert "CLEAN_ERROR:" in completed.stdout, (
        f"expected the headroom check to trip at depth 5000 in this build "
        f"profile, got: {completed.stdout!r}"
    )
    assert "stack headroom exhausted" in completed.stdout, completed.stdout


def test_process_survives_and_runtime_still_usable_after_headroom_error() -> None:
    """A headroom rejection must not leave the runtime (or the process) in a
    broken state -- same runtime, same process, still usable immediately
    after."""
    from pydeno import Runtime, RuntimeConfig

    depth = 5000
    js = (
        "let a = {}; let cur = a; "
        f"for (let i = 0; i < {depth}; i++) {{ cur.n = {{}}; cur = cur.n; }} "
        "a;"
    )
    with Runtime(RuntimeConfig(max_serialization_depth=10**6)) as rt:
        with pytest.raises(Exception, match="stack headroom exhausted"):
            rt.eval(js)
        # The runtime itself must still be usable after the rejection.
        assert rt.eval("2 + 2") == 4
        assert rt.eval("[1, 2, 3]") == [1, 2, 3]


def test_genuine_cycle_is_still_rejected() -> None:
    """The headroom check is layered on top of cycle detection, not instead
    of it -- an actual cycle must still be caught (and caught *before* it
    could ever consume unbounded stack, since it's O(depth) not O(never))."""
    from pydeno import Runtime, RuntimeConfig

    with Runtime(RuntimeConfig()) as rt:
        with pytest.raises(Exception, match="circular reference"):
            rt.eval("let a = {}; a.self = a; a;")
        # Still usable afterward.
        assert rt.eval("1 + 1") == 2


def test_distinct_acyclic_siblings_do_not_spuriously_collide() -> None:
    """Regression guard for the identity-hash version this replaced: two
    distinct, unrelated objects sharing a V8 identity hash must not be
    mistaken for a cycle. A path-based check (`Vec<Local<Object>>` +
    `strict_equals`) only ever compares an object against its own current
    ancestors, so this cannot false-positive regardless of hash collisions."""
    from pydeno import Runtime, RuntimeConfig

    with Runtime(RuntimeConfig()) as rt:
        result = rt.eval(
            "JSON.parse(JSON.stringify("
            "{a: {}, b: {}, c: {d: {}, e: {}}, f: [{}, {}, {}]}"
            "))"
        )
        assert result == {
            "a": {},
            "b": {},
            "c": {"d": {}, "e": {}},
            "f": [{}, {}, {}],
        }


_DEFAULT_CONFIG_SCRIPT = textwrap.dedent(
    """
    import sys
    from pydeno import Runtime, RuntimeConfig

    depth = {depth}
    js = (
        "(() => {{ let a = {{}}; let cur = a; "
        "for (let i = 0; i < " + str(depth) + "; i++) {{ cur.n = {{}}; cur = cur.n; }} "
        "return a; }})()"
    )
    with Runtime(RuntimeConfig()) as rt:
        try:
            value = rt.eval(js)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is the point
            print("REJECTED:" + str(exc))
            sys.exit(0)
        got = 0
        while isinstance(value, dict) and "n" in value:
            value = value["n"]
            got += 1
        print("CONVERTED:" + str(got))
        sys.exit(0)
    """
)


@pytest.mark.parametrize("depth", [22, 40, 60, 99])
def test_default_config_nesting_under_the_depth_limit(depth: int) -> None:
    """The coverage gap that hid O1 from the 0.4.0 review.

    Every depth here is legal at the *default* ``max_serialization_depth`` of
    100, and nothing in the suite exercised the 22..99 band, so neither
    profile's behaviour there was pinned. Two things must hold whatever the
    build profile is:

    * the process survives -- the headroom backstop turns what would be a V8
      ``Check failed: IsOnCentralStack()`` abort into a catchable error;
    * whatever comes back is honest. An optimized build converts the value
      exactly (its backstop sits around depth 743). An unoptimized build
      cannot: its serializer frames are ~33x larger, so ~984 KB of V8 stack
      runs out around depth 22, and the rejection must say *that* -- naming
      the build profile -- rather than reading as a fault in the caller's
      data or in a setting that cannot lift it.

    Released wheels are optimized builds, so only the first branch is what
    users of pydeno see. See ``STACK_HEADROOM_BYTES`` in
    ``src/runtime/js_value.rs`` for the measurements.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _DEFAULT_CONFIG_SCRIPT.format(depth=depth)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, (
        f"process died converting a depth-{depth} value at the default config "
        f"(returncode={completed.returncode}); stderr tail:\n"
        f"{completed.stderr[-2000:]}"
    )

    if completed.stdout.startswith("CONVERTED:"):
        assert completed.stdout.strip() == f"CONVERTED:{depth}", (
            f"depth-{depth} chain did not round-trip intact: {completed.stdout!r}"
        )
        return

    assert completed.stdout.startswith("REJECTED:"), completed.stdout
    message = completed.stdout
    assert "stack headroom exhausted" in message, message
    # The rejection must point at the build, not at the caller: naming the
    # configured depth limit here would send a reader to a knob that cannot
    # help them.
    assert "unoptimized" in message, (
        f"a depth-{depth} value is legal at the default limit of 100, so a "
        f"rejection has to explain that this build's frames are the ceiling: "
        f"{message!r}"
    )
    assert "RuntimeConfig(max_serialization_depth=...)" not in message, message
