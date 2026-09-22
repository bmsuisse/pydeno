"""Termination must be bounded for *every* shape of stuck JavaScript.

The gap these tests exist for: `TerminationHandle.terminate()` could only flip
a flag and call V8's `terminate_execution()`, and that second half is a no-op
against a runtime parked on a pending promise -- V8 only trips a termination
when it next *enters* JavaScript, and a drained-but-pending event loop never
re-enters. Nothing in the dispatcher consulted the flag, so
`new Promise(() => {})` was unkillable: the caller blocked forever.

Two deliberate testing choices here, both about not letting this hole reopen
quietly:

* **Every wait is bounded and a hang is a FAILURE, not a hang.** The blocking
  call runs on a daemon thread joined with a timeout, so against the old code
  these tests fail in a few seconds with a readable message instead of wedging
  the suite (or the CI job) indefinitely. A test that hangs gets disabled; a
  test that fails gets fixed.
* **Nothing here is skippable.** There is no `skipif`, no platform gate and no
  reliance on machine speed beyond generous ceilings, because the absence of a
  test is exactly what let this gap survive two releases.
"""

from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any, Callable

import pytest

import pydeno

# Every parked shape: a promise nobody resolves, an `await` on one, and a
# `.then` chain built on one. The fix must be structural, not pattern-matched
# to the first of these.
PARKED_SHAPES = {
    "never_resolves": "new Promise(() => {})",
    "unresolved_await": "(async () => { await new Promise(() => {}); return 1; })()",
    "chained_on_parked": (
        "(() => { const p = new Promise(() => {}); "
        "return p.then(() => 1).then(() => 2); })()"
    ),
}

# Generous relative to the measured kill latencies (polite spin ~0.09ms,
# parked ~1.4ms median / 3.6ms worst, force-kill = grace + a few ms) so this
# cannot flake on a loaded CI box, but still tiny next to "unbounded".
KILL_CEILING_S = 2.0
# How long we let the worker thread live before declaring the bug reproduced.
JOIN_CEILING_S = 15.0

TERMINATE_AFTER_S = 0.1


def run_bounded(fn: Callable[[], Any], join_ceiling: float = JOIN_CEILING_S) -> Any:
    """Run `fn` on a daemon thread and require it to finish.

    Turns "this call never returns" into a test failure with a useful message.
    The thread is a daemon so a genuinely wedged runtime cannot stop the
    interpreter from exiting at the end of the session.

    `fn` must return plain data. A `Runtime` is an unsendable pyclass, so it
    has to be created *and* dropped on this worker thread; returning anything
    that keeps its frame alive (an exception object, for instance) would let
    it be garbage-collected on the main thread and raise
    "unsendable, but is being dropped on another thread".
    """
    out: queue.Queue = queue.Queue()

    def body() -> None:
        try:
            out.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - propagate anything
            out.put(("raised", f"{type(exc).__name__}: {exc}"))

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    thread.join(join_ceiling)
    assert not thread.is_alive(), (
        f"call did not return within {join_ceiling}s -- termination is unbounded. "
        "This is the parked-promise hang this module exists to prevent."
    )
    kind, value = out.get()
    if kind == "raised":
        raise AssertionError(f"worker thread raised: {value}")
    return value


def classify(exc: BaseException | None) -> tuple[type | None, str]:
    """Reduce an exception to (type, message) -- safe to hand to another thread."""
    if exc is None:
        return None, ""
    return type(exc), str(exc)


def _terminate_after(
    handle: pydeno.TerminationHandle, delay: float
) -> threading.Thread:
    def watchdog() -> None:
        time.sleep(delay)
        handle.terminate()

    thread = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("shape", sorted(PARKED_SHAPES))
def test_terminate_kills_runtime_parked_on_pending_promise(shape: str) -> None:
    """The core regression: terminate() must interrupt a parked promise.

    No `timeout=` is passed, so the *only* thing that can end this call is the
    termination request itself. Before the dispatcher consulted the termination
    flag this blocked forever for all three shapes.
    """
    script = PARKED_SHAPES[shape]

    async def amain() -> tuple[type | None, str, float]:
        runtime = pydeno.Runtime()
        handle = runtime.termination_handle()
        _terminate_after(handle, TERMINATE_AFTER_S)
        started = time.monotonic()
        caught: BaseException | None = None
        try:
            await runtime.eval_async(script)
        except BaseException as exc:  # noqa: BLE001
            caught = exc
        elapsed = time.monotonic() - started
        exc_type, message = classify(caught)
        del runtime, caught
        return exc_type, message, elapsed

    exc_type, message, elapsed = run_bounded(lambda: asyncio.run(amain()))

    assert exc_type is not None and issubclass(exc_type, pydeno.RuntimeTerminated), (
        f"expected RuntimeTerminated for {shape}, got {exc_type} ({message})"
    )
    kill_latency = elapsed - TERMINATE_AFTER_S
    assert kill_latency < KILL_CEILING_S, (
        f"{shape} took {kill_latency * 1000:.1f}ms to die after terminate()"
    )


@pytest.mark.parametrize("shape", sorted(PARKED_SHAPES))
def test_timeout_still_fires_on_parked_promise(shape: str) -> None:
    """`timeout=` was already honest on parked promises; keep it that way.

    This is the counterpart to the test above: the job's own deadline check is
    a separate mechanism from the termination flag, and neither may regress the
    other.
    """
    timeout_s = 0.2

    async def amain() -> tuple[type | None, str, float]:
        runtime = pydeno.Runtime()
        started = time.monotonic()
        caught: BaseException | None = None
        try:
            await runtime.eval_async(PARKED_SHAPES[shape], timeout=timeout_s)
        except BaseException as exc:  # noqa: BLE001
            caught = exc
        elapsed = time.monotonic() - started
        exc_type, message = classify(caught)
        del runtime, caught
        return exc_type, message, elapsed

    exc_type, message, elapsed = run_bounded(lambda: asyncio.run(amain()))

    assert exc_type is not None, f"{shape} should have timed out"
    assert "timed out" in message.lower(), f"unexpected error: {exc_type} ({message})"
    assert elapsed < timeout_s + KILL_CEILING_S


def test_terminate_beats_a_long_timeout() -> None:
    """terminate() must win against a still-distant deadline.

    Guards a subtle false pass: if the dispatcher ignored the termination flag
    and only the job deadline could end the call, a test using a short timeout
    would still go green. Here the timeout is 50x the terminate delay, so
    passing requires the termination path to be what actually fires.
    """
    timeout_s = 5.0

    async def amain() -> tuple[type | None, str, float]:
        runtime = pydeno.Runtime()
        handle = runtime.termination_handle()
        _terminate_after(handle, TERMINATE_AFTER_S)
        started = time.monotonic()
        caught: BaseException | None = None
        try:
            await runtime.eval_async("new Promise(() => {})", timeout=timeout_s)
        except BaseException as exc:  # noqa: BLE001
            caught = exc
        elapsed = time.monotonic() - started
        exc_type, message = classify(caught)
        del runtime, caught
        return exc_type, message, elapsed

    exc_type, message, elapsed = run_bounded(lambda: asyncio.run(amain()))

    assert exc_type is not None and issubclass(exc_type, pydeno.RuntimeTerminated), (
        f"expected RuntimeTerminated, got {exc_type} ({message})"
    )
    assert elapsed < timeout_s / 2, (
        f"died after {elapsed:.2f}s -- looks like the {timeout_s}s deadline "
        "fired rather than the termination request"
    )


def test_spin_and_parked_kills_are_both_bounded_and_the_spin_path_stays_cheap() -> None:
    """The two polite tiers must stay distinct.

    `while(true){}` is killed by V8 unwinding JS the moment it re-enters
    (sub-millisecond). A parked promise is killed by the dispatcher noticing
    the flag between polls (~1-4ms). If the cheap route ever regressed into
    paying for the expensive one, this is where it shows up: the spin kill is
    asserted to be both fast in absolute terms and no slower than the parked
    kill.
    """

    def spin_kill() -> float:
        runtime = pydeno.Runtime()
        handle = runtime.termination_handle()
        _terminate_after(handle, TERMINATE_AFTER_S)
        started = time.monotonic()
        try:
            runtime.eval("while(true){}")
        except BaseException:  # noqa: BLE001
            pass
        elapsed = time.monotonic() - started - TERMINATE_AFTER_S
        del runtime
        return elapsed

    async def parked_kill_async() -> float:
        runtime = pydeno.Runtime()
        handle = runtime.termination_handle()
        _terminate_after(handle, TERMINATE_AFTER_S)
        started = time.monotonic()
        try:
            await runtime.eval_async("new Promise(() => {})")
        except BaseException:  # noqa: BLE001
            pass
        elapsed_inner = time.monotonic() - started - TERMINATE_AFTER_S
        del runtime
        return elapsed_inner

    spin = run_bounded(spin_kill)
    parked = run_bounded(lambda: asyncio.run(parked_kill_async()))

    assert spin < KILL_CEILING_S, f"spin kill took {spin * 1000:.2f}ms"
    assert parked < KILL_CEILING_S, f"parked kill took {parked * 1000:.2f}ms"
    # Not asserting a ratio: both are small enough that scheduler noise
    # dominates, and a flaky perf ratio is worse than no assertion. The
    # absolute ceiling is what protects the cheap path.
    assert spin < 0.5, (
        f"spin kill took {spin * 1000:.2f}ms -- the cheap V8 route looks like "
        "it is now waiting on the dispatcher's poll interval"
    )


def test_polite_kill_does_not_recreate_the_runtime() -> None:
    """A timeout must not cost the caller their runtime.

    The escalation tier abandons a runtime, which destroys bound host
    functions and accumulated state. That must never happen on the ordinary
    path, so: bind a function, get a script killed by the config timeout, and
    the same runtime must still answer with the same binding intact.
    """

    def body() -> Any:
        runtime = pydeno.Runtime(pydeno.RuntimeConfig(timeout=0.2))
        runtime.bind_function("marker", lambda: "still-here")
        runtime.eval("globalThis.kept = 7")
        with pytest.raises(Exception):  # noqa: B017 - any failure is fine
            runtime.eval("while(true){}")
        try:
            return runtime.eval("marker() + ':' + globalThis.kept")
        finally:
            runtime.close()

    assert run_bounded(body) == "still-here:7"


def test_unresolved_then_resolved_promise_still_works() -> None:
    """The flag check must not fire on a healthy runtime.

    A promise that is pending for a while and *then* resolves is the shape
    closest to the one we now abort. It must still resolve normally.
    """

    async def slow() -> str:
        await asyncio.sleep(0.15)
        return "resolved"

    async def amain() -> Any:
        runtime = pydeno.Runtime()
        # There is no `setTimeout` global here, so the pend-then-resolve comes
        # from a real async host op -- which also exercises the dispatcher's
        # waker path rather than just a microtask.
        runtime.bind_function("slow", slow)
        try:
            return await runtime.eval_async("slow()", timeout=5.0)
        finally:
            runtime.close()

    assert run_bounded(lambda: asyncio.run(amain())) == "resolved"


def test_terminate_settles_a_pending_js_function_call_async() -> None:
    """`JsFunction.call_async` is one of the surfaces the kill has to cover.

    The v0.2.0 review recorded this separately from the `eval_async` shapes
    above, because it is reached through a different job
    (`CallFunctionAsync`/`ResumeFunctionCall`, not `EvalAsync`) and it was not
    obvious from outside that the dispatcher fix covered it. It does -- the
    flag check runs between polls regardless of which job is active, and
    `cancel_all_jobs` answers whichever one it finds -- so this test exists to
    keep that true rather than to report a gap.

    A future that never settles is the failure mode: the caller would await
    forever with no exception and no result.
    """

    async def amain() -> tuple[type | None, str, float]:
        runtime = pydeno.Runtime()
        handle = runtime.termination_handle()
        js_func = runtime.eval("(() => new Promise(() => {}))")
        pending = asyncio.ensure_future(js_func.call_async())
        # Let the call reach the runtime thread and park before killing it.
        await asyncio.sleep(0.05)
        _terminate_after(handle, TERMINATE_AFTER_S)
        started = time.monotonic()
        caught: BaseException | None = None
        try:
            await asyncio.wait_for(pending, KILL_CEILING_S + 1.0)
        except BaseException as exc:  # noqa: BLE001
            caught = exc
        elapsed = time.monotonic() - started
        exc_type, message = classify(caught)
        del runtime, js_func, caught
        return exc_type, message, elapsed

    exc_type, message, elapsed = run_bounded(lambda: asyncio.run(amain()))

    assert exc_type is not None and not issubclass(exc_type, asyncio.TimeoutError), (
        f"call_async never settled after terminate(): {exc_type} ({message})"
    )
    assert issubclass(exc_type, pydeno.RuntimeTerminated), (
        f"expected RuntimeTerminated, got {exc_type} ({message})"
    )
    kill_latency = elapsed - TERMINATE_AFTER_S
    assert kill_latency < KILL_CEILING_S, (
        f"call_async took {kill_latency * 1000:.1f}ms to settle after terminate()"
    )


class TestForceKillEscalation:
    """The escalation tier, for a runtime whose *thread* is wedged.

    A synchronous host callback that never returns leaves the dispatcher stuck
    inside `eval_sync` -> V8 -> Python, so it never reaches the termination
    flag and V8 never re-enters JS. Neither polite tier can reach it.
    """

    def test_disabled_by_default(self) -> None:
        assert pydeno.RuntimeConfig().force_kill_grace is None, (
            "force_kill_grace must stay opt-in: enabling it slows every "
            "synchronous call by ~10%"
        )

    def test_configurable(self) -> None:
        assert pydeno.RuntimeConfig(force_kill_grace=0.25).force_kill_grace == 0.25
        config = pydeno.RuntimeConfig()
        config.force_kill_grace = pydeno.SUGGESTED_FORCE_KILL_GRACE
        assert config.force_kill_grace == pytest.approx(0.1)

    def test_force_killed_is_a_kind_of_terminated(self) -> None:
        """Existing `except RuntimeTerminated` handlers must keep working."""
        assert issubclass(pydeno.RuntimeForceKilled, pydeno.RuntimeTerminated)

    def test_escalation_kills_a_wedged_host_callback(self) -> None:
        """A runtime wedged in a host callback is abandoned, not waited on.

        Run in a subprocess on purpose. A force-killed runtime is *leaked* by
        design -- its thread is still parked in the Python callback and its
        isolate can never be reclaimed -- so there is no correct thread on
        which to drop the `Runtime`, and leaving one behind in the test session
        makes the interpreter complain at GC time about an unsendable pyclass
        being dropped on the wrong thread. A subprocess gives the leak a
        natural boundary, and also proves the caller really is released rather
        than merely unblocked by some later test's activity.
        """
        grace = 0.15
        script = textwrap.dedent(
            f"""
            import threading, time, json, pydeno

            release = threading.Event()
            runtime = pydeno.Runtime(pydeno.RuntimeConfig(force_kill_grace={grace}))
            runtime.bind_function("blockForever", lambda: release.wait(60))
            handle = runtime.termination_handle()

            def watchdog():
                time.sleep({TERMINATE_AFTER_S})
                handle.terminate()

            threading.Thread(target=watchdog, daemon=True).start()

            started = time.monotonic()
            name = None
            try:
                runtime.eval("blockForever()")
            except BaseException as exc:
                name = type(exc).__name__
            elapsed = time.monotonic() - started
            release.set()
            print("RESULT" + json.dumps({{"name": name, "elapsed": elapsed}}))
            """
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=JOIN_CEILING_S,
        )
        line = next(
            (
                ln[len("RESULT") :]
                for ln in completed.stdout.splitlines()
                if ln.startswith("RESULT")
            ),
            None,
        )
        assert line is not None, (
            "subprocess produced no result -- the caller was never released, "
            f"i.e. the force-kill escalation did not fire.\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr[-2000:]!r}"
        )
        payload = json.loads(line)

        assert payload["name"] == "RuntimeForceKilled", (
            f"expected RuntimeForceKilled, got {payload['name']}"
        )
        assert payload["elapsed"] >= TERMINATE_AFTER_S + grace, (
            f"gave up after {payload['elapsed']:.3f}s, before the {grace}s grace "
            "elapsed -- a runtime that might still have died politely was abandoned"
        )
        assert payload["elapsed"] < TERMINATE_AFTER_S + grace + KILL_CEILING_S

    def test_enabling_it_does_not_break_the_ordinary_paths(self) -> None:
        """With the escalation armed, healthy and politely-killed work is
        unchanged -- no spurious ForceKilled."""

        def body() -> Any:
            runtime = pydeno.Runtime(
                pydeno.RuntimeConfig(timeout=0.2, force_kill_grace=0.1)
            )
            runtime.bind_function("double", lambda x: x * 2)
            doubled = runtime.eval("double(21)")
            # Capture only the type name: a `pytest.raises` ExceptionInfo (or
            # any retained exception) keeps this frame -- and with it the
            # unsendable `Runtime` -- alive past the end of this thread.
            killed_as = None
            try:
                runtime.eval("while(true){}")
            except BaseException as exc:  # noqa: BLE001
                killed_as = type(exc).__name__
            runtime.close()
            del runtime
            return doubled, killed_as

        doubled, killed_as = run_bounded(body)
        assert doubled == 42
        assert killed_as is not None, "while(true){} should have been killed"
        assert killed_as != "RuntimeForceKilled", (
            "a runtime that died politely must not report ForceKilled"
        )
