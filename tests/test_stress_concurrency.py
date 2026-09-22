"""Stress/load tests: many concurrent runtimes.

RSS is measured in a **fresh subprocess** per scenario, deliberately.
`resource.getrusage` reports *peak* RSS, which is monotonic for the life of a
process -- so measuring in-process would let an earlier, unrelated test's
allocations mask a real regression and turn this into a false pass.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

from peno import Runtime

# Pre-fix growth at 2,000 cycles was ~340MB. Post-fix it is under 1MB. 50MB
# is far above the real figure and far below the regression, so this fails
# loudly on a reintroduced leak without flaking on allocator jitter.
MAX_GROWTH_MB = 50.0

_RSS_HELPER = """
import resource, sys
def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024
"""


def _run_in_fresh_process(body: str) -> dict[str, float]:
    """Run `body` in a clean interpreter and parse its `key=value` output."""
    script = _RSS_HELPER + textwrap.dedent(body)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"stress child failed ({completed.returncode}):\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    out = completed.stdout.strip().splitlines()[-1]
    return {k: float(v) for k, v in (part.split("=") for part in out.split())}


# `Runtime` churn is much more expensive per cycle than pool churn (a fresh
# runtime is ~3ms), so this uses 300 cycles in blocks of 30 -- about a second,
# and enough that a per-runtime leak of even a few KB shows up as a straight
# line instead of a plateau.
RUNTIME_CYCLES = 300
RUNTIME_BLOCK = 30


class TestRuntimeChurnWithOpsDoesNotLeak:
    """The regression test the op-registry leak never had.

    The pre-0.3 leak was the shared `OpRegistry` `Rc` stashed in V8 embedder
    slot 0: it had to be converted back to an `Rc` and dropped when the
    isolate went away, and it was not. The v0.2.0 review found that nothing
    covered it -- `test_rapid_checkout_release_churn_does_not_grow_rss` churns
    *pool checkouts*, and `test_no_global_state_leaks_across_reused_isolate` is
    about state visibility, not bytes. Neither creates and closes a `Runtime`
    **with ops registered**, which is exactly where the leak lived. That made
    it the one already-found bug in this library that could come back in
    silence.

    **Why this asserts on a plateau and not on a drop.** Freeing a runtime
    returns its memory to the process allocator, not to the OS, so RSS after
    a close does not fall -- a "RSS must go back down" assertion would fail
    against *correct* code. The measurement here is `ru_maxrss`, which is
    *peak* RSS and therefore monotonic by construction: it can only be pushed
    up by a new high-water mark. That makes it precisely a ratchet detector.
    A leak ratchets the peak once per cycle and the growth per block stays
    flat and non-zero; a fixed runtime reaches a steady state in the first
    block or two and every later block adds nothing. So the assertion is
    "later blocks add ~nothing", which is a property a leak cannot satisfy and
    a plateau always can.

    Measured on this tree: 0.09MB after the first 30 cycles, then 0.12MB flat
    through cycle 300, i.e. the whole steady state is reached immediately and
    the remaining 270 cycles are free.

    The assertions were checked against a negative control -- the same loop
    with the runtimes retained instead of closed, standing in for "the
    per-runtime allocation is never released". That grows the peak by 576MB
    total with 122MB of it in the second half, failing both assertions by
    ~11x and ~24x. So these numbers are not merely satisfied by the current
    code; they are out of reach for a leaking one.
    """

    def test_create_close_churn_with_bound_ops_does_not_ratchet_rss(self) -> None:
        result = _run_in_fresh_process(f"""
            from peno import Runtime

            def cycle():
                rt = Runtime()
                rt.bind_function("add", lambda a, b: a + b)
                assert rt.eval("add(1, 2)") == 3
                rt.close()

            for _ in range(20):                   # warm up; not measured
                cycle()

            base = rss_mb()
            half = {RUNTIME_CYCLES} // 2
            for _ in range(half):
                cycle()
            mid = rss_mb() - base
            for _ in range(half):
                cycle()
            total = rss_mb() - base
            print(f"first_half={{mid}} total={{total}} late={{total - mid}}")
        """)

        # An absolute ceiling first, so a catastrophic reintroduction fails on
        # the obvious number rather than on the shape.
        assert result["total"] < MAX_GROWTH_MB, (
            f"Runtime churn with ops leaked: peak RSS grew {result['total']:.1f}MB "
            f"over {RUNTIME_CYCLES} create/close cycles (limit {MAX_GROWTH_MB}MB). "
            "This is the pre-0.3 embedder-slot-0 op-registry leak; see "
            "src/runtime/runner.rs."
        )

        # The shape: the second half must add ~nothing. A per-cycle leak grows
        # the peak at a constant rate, so its two halves are equal; a plateau
        # puts essentially all of its growth in the first half.
        assert result["late"] < 5.0, (
            f"peak RSS is still ratcheting after {RUNTIME_CYCLES // 2} cycles: "
            f"first half +{result['first_half']:.2f}MB, second half "
            f"+{result['late']:.2f}MB. A fixed runtime plateaus; a leaking one "
            "grows the second half as fast as the first."
        )
        assert result["late"] <= result["first_half"] + 1.0, (
            f"growth accelerated rather than plateaued: first half "
            f"+{result['first_half']:.2f}MB, second half +{result['late']:.2f}MB"
        )


class TestConcurrentRuntimes:
    def test_many_runtimes_across_threads_shut_down_cleanly(self) -> None:
        """Extends `test_isolate_state_does_not_leak_between_runtimes` to the
        scale an agent server actually runs at.

        Note on what this does and does not prove: each `Runtime` pins its own
        *Rust* OS thread, which `threading.active_count()` cannot see, so the
        Python thread count only confirms the test's own workers are gone.
        Leaked runtime threads would instead show up as RSS growth, which
        `test_many_runtime_lifecycles_do_not_grow_rss` covers.
        """
        threads_before = threading.active_count()
        errors: list[BaseException] = []
        results: list[int] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            try:
                with Runtime() as rt:
                    rt.bind_function("double", lambda x: x * 2)
                    value = rt.eval(f"double({n})")
                    with lock:
                        results.append(value)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                with lock:
                    errors.append(exc)

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=120)

        assert not errors, f"concurrent runtimes raised: {errors[:3]}"
        assert sorted(results) == [i * 2 for i in range(32)]
        assert not any(t.is_alive() for t in workers), "a worker thread hung"
        assert threading.active_count() <= threads_before + 1

    def test_many_runtime_lifecycles_do_not_grow_rss(self) -> None:
        """Creating and closing many runtimes must not leak isolates or
        threads -- either would show up here."""
        result = _run_in_fresh_process("""
            from peno import Runtime
            for _ in range(10):
                with Runtime() as rt:
                    rt.eval("1")
            base = rss_mb()
            for _ in range(200):
                with Runtime() as rt:
                    rt.bind_function("f", lambda: 1)
                    rt.eval("f()")
            print(f"growth={rss_mb() - base} base={base}")
        """)

        assert result["growth"] < MAX_GROWTH_MB, (
            f"Runtime create/close cycles leaked {result['growth']:.1f}MB"
        )
