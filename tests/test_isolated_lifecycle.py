"""`IsolatedRuntime` lifecycle and robustness: leaks, concurrency, closing mid-flight, limits.

A process boundary is only worth having if the processes behind it are cleaned up and cannot
bleed into each other. These tests check the plumbing, not the sandbox: no zombies, no leaked
descriptors, no shared state between runtimes, and sane behaviour at the size limits.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    RuntimeConfig,
    RuntimeTimeout,
    WorkerCrashed,
)

MIB = 1024 * 1024


def _iso(**kwargs: object) -> IsolatedRuntime:
    cfg = RuntimeConfig(timeout=kwargs.pop("timeout", 10.0))  # type: ignore[arg-type]
    return IsolatedRuntime(cfg, **kwargs)  # type: ignore[arg-type]


class TestNoLeaks:
    """Run in a fresh interpreter: pytest and its plugins own children and descriptors too."""

    PRELUDE = textwrap.dedent(
        """
        import os, sys
        from pydeno import IsolatedRuntime, RuntimeConfig, WorkerCrashed, RuntimeTimeout
        import pydeno._isolated as _impl

        # The pre-started spare worker is a deliberate extra child; leak accounting is about
        # the workers a run creates, so keep the spare out of it (it has its own tests).
        _impl._refill_spare = lambda: None

        def open_fds():
            for d in ("/proc/self/fd", "/dev/fd"):
                if os.path.isdir(d):
                    return len(os.listdir(d))

        # warm up one-time allocations (imports, thread pools, tempfile) before counting
        with IsolatedRuntime(RuntimeConfig(timeout=5)) as warm:
            warm.eval("1")
        before = open_fds()

        def one_cycle():
        """
    )
    EPILOGUE = textwrap.dedent(
        """
        for _ in range(CYCLES):
            one_cycle()
        def settle_templates():
            # Fork-template mode (PYDENO_FORK_TEMPLATE=1): workers are children of the template,
            # not of this process. Check none is left under it, then reap the template itself so
            # the child check below only sees what really leaked. No-op in the default mode.
            import time
            from pydeno import _template

            def live(pid):
                try:
                    kids = ""
                    for task in os.listdir(f"/proc/{pid}/task"):
                        with open(f"/proc/{pid}/task/{task}/children") as f:
                            kids += f.read()
                    return kids.split()
                except OSError:
                    return []

            manager = _template.MANAGER
            templates = [t for t in (manager._current, *manager._retired) if t is not None]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(live(t.proc.pid) for t in templates):
                time.sleep(0.05)
            left = any(live(t.proc.pid) for t in templates)
            manager.shutdown()
            return left

        after = open_fds()
        left = settle_templates()
        try:
            os.waitpid(-1, os.WNOHANG)
            children = "a-child-is-still-waiting-to-be-reaped"
        except ChildProcessError:
            children = "none"
        if left:
            children = "a-worker-is-still-alive-under-the-template"
        print(after - before, children)
        """
    )

    def _run(self, body: str, cycles: int = 25) -> tuple[int, str]:
        script = (
            self.PRELUDE
            + textwrap.indent(textwrap.dedent(body), "    ")
            + self.EPILOGUE.replace("CYCLES", str(cycles))
        )
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=240
        )
        assert done.returncode == 0, done.stderr
        grew, children = done.stdout.split()
        return int(grew), children

    def test_closing_leaves_no_zombies_and_no_descriptors(self) -> None:
        grew, children = self._run(
            """
            rt = IsolatedRuntime(RuntimeConfig(timeout=5))
            rt.eval("1 + 1")
            rt.close()
            """
        )
        assert grew == 0, f"{grew} descriptors leaked"
        assert children == "none"

    def test_a_context_manager_exit_is_equally_clean(self) -> None:
        grew, children = self._run(
            """
            with IsolatedRuntime(RuntimeConfig(timeout=5)) as rt:
                rt.eval("1 + 1")
            """
        )
        assert grew == 0
        assert children == "none"

    def test_workers_killed_by_a_hard_deadline_are_reaped(self) -> None:
        grew, children = self._run(
            """
            rt = IsolatedRuntime(RuntimeConfig(timeout=0.2), timeout_grace=0.3)
            try:
                rt.eval("for (;;) {}; ")
            except (RuntimeTimeout, WorkerCrashed, RuntimeError):
                pass
            rt.close()
            """,
            cycles=5,
        )
        assert grew == 0, f"{grew} descriptors leaked"
        assert children == "none"

    def test_workers_that_crashed_are_reaped(self) -> None:
        grew, children = self._run(
            """
            rt = IsolatedRuntime(RuntimeConfig(timeout=5))
            os.kill(rt._proc.pid, 9)
            try:
                rt.eval("1")
            except WorkerCrashed:
                pass
            rt.close()
            """,
            cycles=10,
        )
        assert grew == 0, f"{grew} descriptors leaked"
        assert children == "none"


def _process_state(pid: int) -> str | None:
    """'R', 'S', 'Z', ... or None if the process does not exist."""
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return None
    done = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()[:1] or None


class TestOrphans:
    """If the parent dies without cleaning up (the OOM killer, `kill -9`), the worker must not be
    left spinning on a guest's infinite loop forever: it sees its pipe close and exits."""

    PARENT = textwrap.dedent(
        """
        import sys, threading, time
        from pydeno import IsolatedRuntime, RuntimeConfig

        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=None, max_memory=None)
        print(rt._proc.pid, flush=True)
        threading.Thread(target=lambda: rt.eval("for (;;) {}"), daemon=True).start()
        time.sleep(600)
        """
    )

    def test_a_worker_exits_when_its_parent_is_killed_hard(self) -> None:
        parent = subprocess.Popen(
            [sys.executable, "-c", self.PARENT], stdout=subprocess.PIPE, text=True
        )
        try:
            assert parent.stdout is not None
            worker_pid = int(parent.stdout.readline())
            time.sleep(0.5)  # let the worker get properly stuck in the loop
            assert _process_state(worker_pid) not in (None, "Z"), (
                "the worker should be running"
            )
            parent.kill()
            parent.wait(10)
            deadline = time.monotonic() + 15
            state = _process_state(worker_pid)
            while state not in (None, "Z") and time.monotonic() < deadline:
                time.sleep(0.2)
                state = _process_state(worker_pid)
            assert state in (None, "Z"), (
                f"the worker (pid {worker_pid}) outlived its parent in state {state!r}"
            )
        finally:
            parent.kill()
            parent.wait(10)


class TestIsolationBetweenRuntimes:
    def test_global_state_is_not_shared(self) -> None:
        with _iso() as a, _iso() as b:
            a.eval("globalThis.secret = 'a-only'")
            assert b.eval("typeof globalThis.secret") == "undefined"
            b.eval("globalThis.secret = 'b-only'")
            assert a.eval("secret") == "a-only"

    def test_prototype_pollution_does_not_cross_runtimes(self) -> None:
        with _iso() as a, _iso() as b:
            a.eval("Object.prototype.polluted = true; Array.prototype.polluted = 1")
            assert (
                b.eval("({}).polluted === undefined && [].polluted === undefined")
                is True
            )

    def test_state_persists_within_one_runtime(self) -> None:
        with _iso() as rt:
            rt.eval("globalThis.n = 0")
            for _ in range(20):
                rt.eval("n += 1")
            assert rt.eval("n") == 20

    def test_a_crash_in_one_runtime_does_not_touch_another(self) -> None:
        with _iso() as healthy:
            healthy.eval("globalThis.alive = true")
            doomed = IsolatedRuntime(RuntimeConfig(), request_timeout=1.0)
            with pytest.raises((RuntimeTimeout, WorkerCrashed)):
                doomed.eval("const a = []; a[2 ** 32 - 2] = 1; a.sort()")
            assert healthy.eval("alive") is True
            assert not healthy.is_closed()

    def test_host_functions_are_per_runtime(self) -> None:
        with _iso() as a, _iso() as b:
            a.bind_function("who", lambda: "a")
            b.bind_function("who", lambda: "b")
            assert a.eval("who()") == "a"
            assert b.eval("who()") == "b"

    def test_one_runtimes_token_is_useless_against_another(self) -> None:
        with _iso() as a, _iso() as b:
            token = a.bind_function("secret_tool", lambda: "a's secret")
            assert b.revoke_op(token) in (
                True,
                False,
            )  # a number, not authority over `a`
            assert a.eval("secret_tool()") == "a's secret"


class TestConcurrency:
    def test_many_threads_each_with_their_own_runtime(self) -> None:
        results: dict[int, object] = {}
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                with _iso() as rt:
                    rt.bind_function("echo", lambda x: x)
                    rt.eval(f"globalThis.me = {i}")
                    results[i] = (rt.eval("me"), rt.eval(f"echo({i}) * 2"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert not errors, errors
        assert results == {i: (i, i * 2) for i in range(8)}

    def test_one_runtime_serialises_concurrent_callers(self) -> None:
        """Two threads sharing a runtime take turns; neither sees a half-finished command."""
        with _iso() as rt:
            rt.eval("globalThis.log = []")
            errors: list[BaseException] = []

            def work(tag: str) -> None:
                try:
                    for _ in range(25):
                        rt.eval(f"log.push({tag!r})")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            ts = [threading.Thread(target=work, args=(t,)) for t in ("a", "b")]
            for t in ts:
                t.start()
            for t in ts:
                t.join(60)
            assert not errors
            assert rt.eval("log.length") == 50

    def test_async_runtimes_overlap_in_one_event_loop(self) -> None:
        async def go() -> float:
            runtimes = [_iso() for _ in range(4)]
            try:
                start = time.monotonic()
                await asyncio.gather(
                    *(
                        r.eval_async(
                            "new Promise(res => { let n = 0; while (n < 3e6) n++; res(n) })"
                        )
                        for r in runtimes
                    )
                )
                return time.monotonic() - start
            finally:
                for r in runtimes:
                    r.close()

        assert asyncio.run(go()) < 60


class TestLimitsHoldUnderConstantTraffic:
    """The parent checks its deadlines between frames. If it only checked when the pipe went
    quiet, a guest that never lets it go quiet (an endless stream of cheap host calls) would
    switch every limit off."""

    def _run_bounded(
        self, rt: IsolatedRuntime, js: str, wall: float = 20.0
    ) -> BaseException | str:
        outcome: list[BaseException | str] = []

        def run() -> None:
            try:
                rt.eval(js)
                outcome.append("returned")
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(wall)
        if t.is_alive():
            rt._kill()  # noqa: SLF001 - the very failure under test; clean up before reporting it
            t.join(10)
            pytest.fail(
                f"the limit never fired: {js!r} was still running after {wall}s"
            )
        return outcome[0]

    def test_the_hard_deadline_fires_under_a_stream_of_cheap_host_calls(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.5)
        rt.bind_function("tick", lambda: None)
        start = time.monotonic()
        result = self._run_bounded(rt, "for (;;) tick()")
        assert isinstance(result, RuntimeTimeout), result
        assert time.monotonic() - start < 15
        assert rt.is_closed()

    def test_the_memory_ceiling_fires_under_a_stream_of_cheap_host_calls(self) -> None:
        if sys.platform not in ("linux", "darwin"):
            pytest.skip("RSS reading is implemented for Linux and macOS")
        rt = IsolatedRuntime(RuntimeConfig(), max_memory=300 * MIB, request_timeout=60)
        rt._max_memory = 300 * MIB  # noqa: SLF001
        rt.bind_function("tick", lambda: None)
        # grow while hammering the host, so no quiet gap ever lets a poll-only check run
        js = "const a = []; for (;;) { tick(); a.push(new Array(10000).fill(1)); }"
        result = self._run_bounded(rt, js, wall=40)
        assert isinstance(result, WorkerCrashed) and "max_memory" in str(result), result

    def test_a_slow_host_function_is_still_not_charged_to_the_deadline(self) -> None:
        def slow() -> str:
            time.sleep(1.2)
            return "done"

        with IsolatedRuntime(RuntimeConfig(), request_timeout=1.0) as rt:
            rt.bind_function("slow", slow)
            assert rt.eval("slow() + slow()") == "donedone"

    def test_the_deadline_still_counts_guest_time_between_host_calls(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.5)
        rt.bind_function("tick", lambda: None)
        result = self._run_bounded(
            rt, "for (;;) { tick(); for (let i = 0; i < 2e5; i++) {} }"
        )
        assert isinstance(result, RuntimeTimeout), result


class TestClosingMidFlight:
    def test_closing_from_another_thread_ends_a_running_eval_promptly(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=60)
        outcome: list[BaseException | str] = []

        def run() -> None:
            try:
                rt.eval("for (;;) {}")
                outcome.append("returned")
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.4)
        started = time.monotonic()
        rt.close()
        t.join(15)
        assert not t.is_alive(), "the evaluating thread was left hanging"
        assert time.monotonic() - started < 10
        assert len(outcome) == 1 and isinstance(outcome[0], WorkerCrashed), outcome

    def test_use_after_close_is_a_clean_error(self) -> None:
        rt = _iso()
        rt.close()
        for call in (
            lambda: rt.eval("1"),
            lambda: rt.bind_function("f", lambda: 1),
            lambda: rt.add_static_module("m", "export default 1"),
        ):
            with pytest.raises(WorkerCrashed, match="closed"):
                call()

    def test_use_after_close_in_async(self) -> None:
        async def go() -> None:
            rt = _iso()
            rt.close()
            with pytest.raises(WorkerCrashed, match="closed"):
                await rt.eval_async("1")

        asyncio.run(go())

    def test_an_exception_inside_the_with_block_still_closes_the_worker(self) -> None:
        proc = None
        with pytest.raises(ZeroDivisionError):
            with _iso() as rt:
                proc = rt._proc  # noqa: SLF001
                1 / 0  # noqa: B018
        assert proc is not None and proc.poll() is not None

    def test_a_dead_runtime_can_be_replaced(self) -> None:
        for _ in range(4):
            rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.0)
            with pytest.raises((RuntimeTimeout, WorkerCrashed)):
                rt.eval("const a = []; a[2 ** 32 - 2] = 1; a.map(x => x)")
            with _iso() as fresh:
                assert fresh.eval("21 * 2") == 42


class TestValuesAtTheEdges:
    def test_special_floats_through_a_host_function(self) -> None:
        seen: list[float] = []
        with _iso() as rt:
            rt.bind_function("keep", lambda x: seen.append(x) or x)
            for js in (
                "NaN",
                "Infinity",
                "-Infinity",
                "-0",
                "5e-324",
                "1.7976931348623157e308",
            ):
                rt.eval(f"keep({js})")
        assert math.isnan(seen[0])
        assert seen[1] == math.inf and seen[2] == -math.inf
        assert str(seen[3]) == "-0.0"
        assert seen[4] == 5e-324 and seen[5] == 1.7976931348623157e308

    def test_integers_around_the_safe_boundary(self) -> None:
        seen: list[int] = []
        with _iso() as rt:
            rt.bind_function("keep", lambda x: seen.append(x) or x)
            for js in (
                "2n ** 53n",
                "2n ** 53n + 1n",
                "-(2n ** 63n)",
                "2n ** 200n",
                "0n",
            ):
                rt.eval(f"keep({js})")
        assert seen == [2**53, 2**53 + 1, -(2**63), 2**200, 0]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "plain",
            "héllo wörld",
            "日本語",
            "emoji 😀🎉",
            "line\nbreak\ttab",
            "nul\x00byte",
            'quote"back\\slash',
            "  ",
        ],
    )
    def test_strings_round_trip_unchanged(self, text: str) -> None:
        with _iso() as rt:
            rt.bind_function("echo", lambda s: s)
            rt.bind_function("expect", lambda: text)
            # JSON string literals are valid JavaScript; they carry every code point exactly.
            assert rt.eval(f"echo({json.dumps(text)})") == text
            assert rt.eval("expect()") == text
            # and the guest sees the same text (compared as UTF-16 code units, as JS does)
            assert rt.eval(f"{json.dumps(text)} === expect()") is True

    def test_nested_containers_round_trip(self) -> None:
        value = {"a": [1, 2, {"b": None, "c": [True, False]}], "d": {"e": {"f": "g"}}}
        with _iso() as rt:
            rt.bind_function("get", lambda: value)
            assert rt.eval("get()") == value

    def test_bytes_of_every_value(self) -> None:
        data = bytes(range(256))
        with _iso() as rt:
            rt.bind_function("blob", lambda: data)
            assert rt.eval("Array.from(blob()).join(',')") == ",".join(
                str(b) for b in data
            )


class TestSizeLimitsAcrossTheBoundary:
    def test_a_large_result_within_the_limit_arrives_intact(self) -> None:
        with _iso() as rt:
            out = rt.eval("'x'.repeat(4 * 1024 * 1024)")
            assert len(out) == 4 * MIB

    def test_a_result_over_the_frame_cap_is_an_error_and_the_runtime_survives(
        self,
    ) -> None:
        with _iso() as rt:
            with pytest.raises((TypeError, RuntimeError, JavaScriptError)):
                rt.eval("'x'.repeat(40 * 1024 * 1024)")
            assert rt.eval("1 + 1") == 2

    def test_a_host_function_argument_over_the_cap_is_a_catchable_js_error(
        self,
    ) -> None:
        with _iso() as rt:
            rt.bind_function("sink", lambda s: len(s))
            assert (
                rt.eval(
                    "try { sink('x'.repeat(40 * 1024 * 1024)); 'sent' } catch (e) { 'refused' }"
                )
                == "refused"
            )
            assert rt.eval("sink('ok')") == 2

    def test_a_host_function_result_over_the_cap_is_a_catchable_js_error(self) -> None:
        with _iso() as rt:
            rt.bind_function("huge", lambda: "x" * (40 * MIB))
            assert rt.eval("try { huge(); 'got' } catch (e) { 'refused' }") == "refused"
            assert rt.eval("1") == 1

    def test_deeply_nested_host_result_is_refused_not_a_crash(self) -> None:
        deep: object = 0
        for _ in range(500):
            deep = [deep]
        with _iso() as rt:
            rt.bind_function("deep", lambda: deep)
            assert rt.eval("try { deep(); 'got' } catch (e) { 'refused' }") == "refused"

    def test_many_small_calls_in_a_row(self) -> None:
        with _iso() as rt:
            rt.bind_function("inc", lambda n: n + 1)
            assert (
                rt.eval("let n = 0; for (let i = 0; i < 500; i++) n = inc(n); n") == 500
            )

    def test_a_long_sequence_of_evals(self) -> None:
        with _iso() as rt:
            for i in range(300):
                assert rt.eval(f"{i} + 1") == i + 1


def test_a_kill_by_the_idle_watchdog_keeps_its_reason() -> None:
    # The watchdog thread cannot raise into the caller; the pump used to report a bare "SIGKILL".
    rt = IsolatedRuntime(RuntimeConfig(), request_timeout=30)
    rt._kill_reason = "worker used 1 bytes, over max_memory=0; killed"  # noqa: SLF001
    os.kill(rt._proc.pid, signal.SIGKILL)  # noqa: SLF001
    with pytest.raises(WorkerCrashed, match="over max_memory"):
        rt.eval("1")
