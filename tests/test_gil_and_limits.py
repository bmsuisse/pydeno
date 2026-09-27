"""Regression tests for two PyO3-boundary defects fixed in v0.3.

1. `JsFunction.__call__` held the GIL across a blocking round trip to the
   runtime thread, so every thread calling a JS function serialized against
   every other one even though each had its own `Runtime` and its own V8
   isolate. `Runtime.eval` already released the GIL and served as the control.

2. `convert_python_args` built a fresh `LimitTracker` per argument, so
   `max_serialization_bytes` was enforced per-argument instead of as an
   aggregate budget for the call -- N arguments each just under the limit
   transferred N times the intended budget.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from pydeno import Runtime, RuntimeConfig


def _assert_calls_overlap(entry: str) -> None:
    """Both isolates must enter Python before either callback can finish.

    Run in a subprocess because retaining the GIL makes the regression a hard
    deadlock. Unlike CPU-speed ratios, this also works on loaded CI runners.
    """
    code = textwrap.dedent(f"""
        import threading
        from pydeno import Runtime
        barrier = threading.Barrier(2, timeout=5)
        results = []
        errors = []
        def worker():
            try:
                with Runtime() as rt:
                    rt.bind_function("meet", lambda: barrier.wait())
                    if {entry!r} == "eval":
                        result = rt.eval("meet(); 42")
                    else:
                        function = rt.eval("() => {{ meet(); return 42; }}")
                        result = function()
                    results.append(result)
            except BaseException as error:
                errors.append(str(error))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert results == [42, 42], results
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


class TestGilIsReleasedAcrossBlockingCalls:
    def test_runtime_eval_overlaps_across_threads(self) -> None:
        _assert_calls_overlap("eval")

    def test_js_function_call_overlaps_across_threads(self) -> None:
        _assert_calls_overlap("function")

    def test_constructing_a_runtime_does_not_deadlock_on_a_logging_bootstrap(
        self,
    ) -> None:
        """The GIL hole in construction was a hard deadlock, not just slowness.

        `RuntimeHandle::spawn` blocks until the runtime thread has built its
        isolate. With the GIL held across that wait, an `on_console` callback
        fired by a `bootstrap` script could never acquire the GIL to run, and
        `Runtime(config)` hung forever. This test simply completing proves the
        fix; it would time out the suite otherwise.
        """
        records: list[tuple[str, list]] = []
        config = RuntimeConfig(
            on_console=lambda level, args: records.append((level, args)),
            bootstrap="console.log('bootstrap ran');",
        )
        with Runtime(config) as rt:
            assert rt.eval("1 + 1") == 2
        assert records == [("log", ["bootstrap ran"])]


class TestSerializationBudgetIsAggregate:
    """`max_serialization_bytes` caps a whole call, not each argument.

    This class covers the **Python -> JS** direction (`JsFunction` arguments).
    Its inbound mirror lives in
    `TestSerializationLimitsAreSymmetric` below: covering only this half is
    what let the v0.2.0 review find the identical bug, unfixed, on the
    JS -> Python side.
    """

    # Small enough to make the arithmetic obvious, large enough to clear the
    # per-value overhead the tracker also counts.
    LIMIT = 40_000

    def _runtime(self) -> Runtime:
        return Runtime(RuntimeConfig(max_serialization_bytes=self.LIMIT))

    def test_a_single_argument_under_the_limit_is_accepted(self) -> None:
        with self._runtime() as rt:
            echo = rt.eval("(...args) => args.length")
            assert echo("x" * (self.LIMIT // 4)) == 1

    def test_a_single_argument_over_the_limit_is_rejected(self) -> None:
        with self._runtime() as rt:
            echo = rt.eval("(...args) => args.length")
            with pytest.raises(Exception, match="(?i)size|byte|limit|exceed"):
                echo("x" * (self.LIMIT * 2))

    def test_many_arguments_each_under_the_limit_are_rejected_in_aggregate(
        self,
    ) -> None:
        """The actual bug: this used to succeed.

        Eight arguments of ~LIMIT/4 bytes each are individually well under
        `max_serialization_bytes`, but together are roughly 2x it. With a
        per-argument tracker every one passed its own check and the call went
        through, transferring double the configured budget. With one shared
        tracker the call is refused.
        """
        chunk = "x" * (self.LIMIT // 4)
        with self._runtime() as rt:
            echo = rt.eval("(...args) => args.length")
            with pytest.raises(Exception, match="(?i)size|byte|limit|exceed"):
                echo(*([chunk] * 8))

    def test_the_aggregate_check_does_not_reject_a_legitimate_call(self) -> None:
        """Guard against over-correcting: a call comfortably inside the total
        budget must still succeed with several arguments."""
        chunk = "x" * (self.LIMIT // 40)
        with self._runtime() as rt:
            echo = rt.eval("(...args) => args.length")
            assert echo(*([chunk] * 5)) == 5

    def test_repeating_the_same_object_is_not_mistaken_for_a_cycle(self) -> None:
        """Cycle detection stays per-argument.

        Sharing the byte budget must not mean sharing the `seen` set: the same
        object legitimately passed twice is not a cycle.
        """
        shared = {"a": [1, 2, 3]}
        with Runtime() as rt:
            echo = rt.eval("(...args) => args.length")
            assert echo(shared, shared, shared) == 3


class TestSerializationLimitsAreSymmetric:
    """The inbound half: `max_serialization_bytes` / `max_serialization_depth`
    must bind on **JS -> Python** too, aggregately, and identically.

    Regression tests for the v0.2.0 review's finding M3. Both op entry points
    (`src/runtime/ops.rs`) converted each guest argument with no
    `LimitTracker` at all, so:

    - 37.7 MB was accepted in one call against the default 10 MB cap
      (four 9 MB strings; the guest picks the multiplier, so it scales
      linearly), while a legitimate 11 MB *host* payload was refused. The
      limit bound the trusted party and not the untrusted one.
    - A 10-deep object was accepted against a configured depth of 3, while the
      identical value was correctly refused as an `eval` result. What actually
      stopped runaway inbound depth was `serde_v8`'s own recursion constant,
      not the knob the user set.

    `max_heap_size` does not cover any of this: the cost is host-side Python
    and Rust allocation, outside the V8 heap.
    """

    LIMIT = 1 << 20  # 1 MiB, small enough to keep the test fast

    def _runtime(self) -> Runtime:
        return Runtime(RuntimeConfig(max_serialization_bytes=self.LIMIT, timeout=10.0))

    def test_a_single_inbound_argument_over_the_limit_is_rejected(self) -> None:
        with self._runtime() as rt:
            rt.bind_function("sink", lambda *a: sum(len(x) for x in a))
            with pytest.raises(Exception, match="(?i)size|byte|limit|exceed"):
                rt.eval(f"sink('x'.repeat({self.LIMIT * 2}))")

    def test_many_inbound_arguments_are_rejected_in_aggregate(self) -> None:
        """The measured escape: four arguments each under the cap.

        Against v0.2.0 this returned 37748736 with the default 10 MB limit.
        """
        each = (self.LIMIT * 3) // 4
        with self._runtime() as rt:
            rt.bind_function("sink", lambda *a: sum(len(x) for x in a))
            # Each argument is fine on its own ...
            assert rt.eval(f"sink('x'.repeat({each}))") == each
            # ... and four of them are not.
            with pytest.raises(Exception, match="(?i)size|byte|limit|exceed"):
                rt.eval(f"sink(...Array(4).fill('x'.repeat({each})))")

    def test_the_aggregate_check_does_not_reject_a_legitimate_inbound_call(
        self,
    ) -> None:
        """Guard against over-correcting, as the outbound half does."""
        each = self.LIMIT // 40
        with self._runtime() as rt:
            rt.bind_function("sink", lambda *a: len(a))
            assert rt.eval(f"sink(...Array(5).fill('x'.repeat({each})))") == 5

    def test_inbound_depth_respects_the_configured_limit(self) -> None:
        """The review's exact repro: depth 10 against a configured 3."""
        config = RuntimeConfig(max_serialization_depth=3)
        with Runtime(config) as rt:
            rt.bind_function("sink", lambda v: "ok")
            with pytest.raises(Exception, match="(?i)depth"):
                rt.eval("let a={};let c=a;for(let i=0;i<10;i++){c.n={};c=c.n};sink(a)")
            # The same value as an `eval` result was always refused; the point
            # of the fix is that the two now agree.
            with pytest.raises(Exception, match="(?i)depth"):
                rt.eval("let b={};let d=b;for(let i=0;i<10;i++){d.n={};d=d.n};b")
            # And a shallow value still passes both ways.
            assert rt.eval("sink({a: 1})") == "ok"

    def test_both_limit_messages_name_the_knob_that_rejected_the_call(self) -> None:
        """A tunable limit that does not name its knob reads as a hard wall."""
        with self._runtime() as rt:
            rt.bind_function("sink", lambda *a: 1)
            with pytest.raises(Exception, match="max_serialization_bytes"):
                rt.eval(f"sink('x'.repeat({self.LIMIT * 2}))")

        with Runtime(RuntimeConfig(max_serialization_depth=3)) as rt:
            rt.bind_function("sink", lambda v: 1)
            with pytest.raises(Exception, match="max_serialization_depth"):
                rt.eval("let a={};let c=a;for(let i=0;i<10;i++){c.n={};c=c.n};sink(a)")
