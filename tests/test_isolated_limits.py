"""The limits in `RuntimeConfig` must mean the same thing across the process boundary, and the
timeouts must compose sensibly: a soft timeout is the guest's, a hard deadline is the parent's."""

from __future__ import annotations

import asyncio
import time

import pytest

from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    Runtime,
    RuntimeConfig,
    RuntimeTerminated,
    RuntimeTimeout,
    WorkerCrashed,
)

MIB = 1024 * 1024


class TestConfigLimitsCrossTheBoundary:
    def test_the_heap_limit_terminates_the_guest(self) -> None:
        cfg = RuntimeConfig(
            max_heap_size=16 * MIB, initial_heap_size=4 * MIB, timeout=20.0
        )
        with IsolatedRuntime(cfg, request_timeout=30) as rt:
            with pytest.raises((RuntimeTerminated, RuntimeError)) as caught:
                rt.eval("const a = []; for (;;) a.push({x: new Array(1000).fill(1)})")
            assert "heap" in str(caught.value).lower() or isinstance(
                caught.value, RuntimeTerminated
            )

    def test_the_heap_limit_message_matches_the_in_process_runtime(self) -> None:
        js = "const a = []; for (;;) a.push({x: new Array(1000).fill(1)})"
        cfg = dict(max_heap_size=16 * MIB, initial_heap_size=4 * MIB, timeout=20.0)
        in_process = Runtime(RuntimeConfig(**cfg))
        with pytest.raises(RuntimeTerminated) as inside:
            in_process.eval(js)
        with IsolatedRuntime(RuntimeConfig(**cfg), request_timeout=30) as rt:
            with pytest.raises(RuntimeTerminated) as isolated:
                rt.eval(js)
        assert str(inside.value) == str(isolated.value)

    def test_the_buffer_cap_is_a_catchable_range_error(self) -> None:
        cfg = RuntimeConfig(max_buffer_bytes=8 * MIB, timeout=10.0)
        with IsolatedRuntime(cfg) as rt:
            assert (
                rt.eval(
                    "try { new ArrayBuffer(64 * 1024 * 1024); 'allocated' } catch (e) { e.constructor.name }"
                )
                == "RangeError"
            )
            assert rt.eval("new ArrayBuffer(1024).byteLength") == 1024

    def test_serialization_depth_limit_applies_to_results(self) -> None:
        cfg = RuntimeConfig(max_serialization_depth=5, timeout=10.0)
        with IsolatedRuntime(cfg) as rt:
            assert rt.eval("[[[1]]]") == [[[1]]]
            with pytest.raises((RuntimeError, JavaScriptError, TypeError)):
                rt.eval("[[[[[[[[1]]]]]]]]")
            assert rt.eval("1 + 1") == 2

    def test_serialization_byte_limit_applies_to_results(self) -> None:
        cfg = RuntimeConfig(max_serialization_bytes=2048, timeout=10.0)
        with IsolatedRuntime(cfg) as rt:
            assert rt.eval("'x'.repeat(100)") == "x" * 100
            with pytest.raises((RuntimeError, JavaScriptError, TypeError)):
                rt.eval("'x'.repeat(100000)")
            assert rt.eval("2 + 2") == 4

    def test_serialization_limits_apply_to_host_function_arguments(self) -> None:
        cfg = RuntimeConfig(max_serialization_bytes=2048, timeout=10.0)
        with IsolatedRuntime(cfg) as rt:
            rt.bind_function("sink", lambda s: len(s))
            assert rt.eval("sink('x'.repeat(100))") == 100
            assert (
                rt.eval(
                    "try { sink('x'.repeat(100000)); 'sent' } catch (e) { 'refused' }"
                )
                == "refused"
            )

    def test_bootstrap_runs_before_guest_code(self) -> None:
        cfg = RuntimeConfig(bootstrap="globalThis.preset = 123;", timeout=10.0)
        with IsolatedRuntime(cfg) as rt:
            assert rt.eval("preset") == 123

    def test_initial_heap_without_a_maximum_is_an_error_at_start(self) -> None:
        cfg = RuntimeConfig(initial_heap_size=4 * MIB)
        with pytest.raises(WorkerCrashed, match="failed to start"):
            IsolatedRuntime(cfg)


class TestTimeouts:
    def test_a_soft_timeout_is_prompt_and_leaves_the_worker_alive(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=0.4)) as rt:
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout):
                rt.eval("for (;;) {}")
            assert time.monotonic() - start < 3
            assert not rt.is_closed()
            assert rt.eval("1") == 1

    def test_the_soft_timeout_applies_to_every_eval(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=0.3)) as rt:
            for _ in range(3):
                with pytest.raises(RuntimeTimeout):
                    rt.eval("for (;;) {}")
            assert rt.eval("1 + 1") == 2

    def test_eval_async_timeout_overrides_the_config_timeout(self) -> None:
        async def go() -> None:
            with IsolatedRuntime(RuntimeConfig(timeout=30.0), timeout_grace=2.0) as rt:
                start = time.monotonic()
                with pytest.raises(RuntimeTimeout):
                    await rt.eval_async("new Promise(() => {})", timeout=0.5)
                assert time.monotonic() - start < 6

        asyncio.run(go())

    def test_a_timedelta_timeout_is_accepted(self) -> None:
        from datetime import timedelta

        async def go() -> None:
            with IsolatedRuntime(RuntimeConfig(timeout=30.0), timeout_grace=2.0) as rt:
                with pytest.raises(RuntimeTimeout):
                    await rt.eval_async(
                        "new Promise(() => {})", timeout=timedelta(seconds=0.4)
                    )

        asyncio.run(go())

    def test_a_shorter_hard_deadline_beats_a_longer_soft_timeout(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(timeout=30.0), request_timeout=1.0)
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="hard deadline"):
            # A spin, not the sparse-array sort: that sort allocates gigabytes, so on a fast
            # machine the memory ceiling can win the race against a 1 s deadline.
            rt.eval("while (true) {}")
        assert time.monotonic() - start < 8

    def test_grace_extends_the_hard_deadline_past_the_soft_timeout(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(timeout=0.5), timeout_grace=1.5)
        assert rt._hard_timeout(0.5) == 2.0  # noqa: SLF001
        rt.close()

    def test_no_timeout_at_all_still_gets_the_default_hard_deadline(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig())
        assert rt._hard_timeout(None) == 60.0  # noqa: SLF001
        rt.close()

    def test_a_resolved_promise_returns_long_before_the_deadline(self) -> None:
        async def go() -> object:
            with IsolatedRuntime(RuntimeConfig(timeout=30.0)) as rt:
                return await rt.eval_async("Promise.resolve('fast')")

        start = time.monotonic()
        assert asyncio.run(go()) == "fast"
        assert time.monotonic() - start < 10


class TestBindingScale:
    def test_many_host_functions(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=30.0)) as rt:
            for i in range(300):
                rt.bind_function(f"f{i}", lambda i=i: i)
            assert rt.eval("f0() + f150() + f299()") == 0 + 150 + 299

    def test_many_members_in_one_bound_object(self) -> None:
        members = {f"m{i}": (lambda i=i: i) for i in range(200)}
        with IsolatedRuntime(RuntimeConfig(timeout=30.0)) as rt:
            tokens = rt.bind_object("api", members)
            assert len(tokens) == 200
            assert rt.eval("api.m0() + api.m199()") == 199

    def test_revoking_every_binding_leaves_a_working_runtime(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=30.0)) as rt:
            tokens = [rt.bind_function(f"g{i}", lambda i=i: i) for i in range(50)]
            assert all(rt.revoke_op(t) for t in tokens)
            assert not rt._handlers  # noqa: SLF001 - the parent forgot them too
            with pytest.raises(JavaScriptError):
                rt.eval("g0()")
            assert rt.eval("1 + 1") == 2

    def test_revoking_twice_is_false_not_an_error(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            token = rt.bind_function("once", lambda: 1)
            assert rt.revoke_op(token) is True
            assert rt.revoke_op(token) is False

    def test_a_forged_token_revokes_nothing(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            rt.bind_function("keep", lambda: "kept")
            for guess in (0, 1, 2, 12345, 2**53 - 1):
                assert rt.revoke_op(guess) is False
            assert rt.eval("keep()") == "kept"

    def test_rebinding_a_name_replaces_the_function(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            rt.bind_function("v", lambda: 1)
            assert rt.eval("v()") == 1
            rt.bind_function("v", lambda: 2)
            assert rt.eval("v()") == 2


class TestLimitsTooLargeForTheWire:
    """A limit the worker cannot receive as an integer is refused up front.

    Integers past 2**53 - 1 cross the wire as a tagged object, so `max_memory=2**62` (which derives
    `max_buffer_bytes = 2**60`) used to fail inside the worker with "argument 'max_buffer_bytes':
    'dict' object cannot be interpreted as an integer", reported as a `WorkerCrashed`."""

    @pytest.mark.parametrize("max_memory", [2**53, 2**62, 10**30], ids=str)
    def test_huge_max_memory_is_a_value_error(self, max_memory: int) -> None:
        with pytest.raises(ValueError, match="max_memory must be at most"):
            IsolatedRuntime(max_memory=max_memory, sandbox="off")

    def test_huge_config_limit_is_a_value_error(self) -> None:
        cfg = RuntimeConfig(max_buffer_bytes=2**60)
        with pytest.raises(ValueError, match="max_buffer_bytes must be at most"):
            IsolatedRuntime(cfg, sandbox="off")

    def test_the_largest_accepted_max_memory_starts(self) -> None:
        with IsolatedRuntime(max_memory=2**53 - 1, sandbox="off") as rt:
            assert rt.eval("1 + 1") == 2

    @pytest.mark.parametrize("max_memory", [2**53, 2**62], ids=str)
    def test_async_runtime_refuses_too(self, max_memory: int) -> None:
        from pydeno import AsyncIsolatedRuntime

        with pytest.raises(ValueError, match="max_memory must be at most"):
            AsyncIsolatedRuntime(max_memory=max_memory, sandbox="off")


class TestTheCpuCapWithACachedBaseline:
    """A command's CPU baseline is the latest cached reading, not a fresh one per command (two
    readings per command were a third of the warm-call cost). CPU time only grows, so an older
    baseline can only charge a command more, never less; these pin that it still catches a
    burner and that the baseline never runs ahead of the worker's real CPU time."""

    def test_a_cpu_burning_command_is_still_caught(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from pydeno import _isolated

        # cap = hard deadline x factor: 2 s x 0.1 = 0.2 s of CPU, far inside the 2 s deadline,
        # so only the CPU cap can be what stops it.
        monkeypatch.setattr(_isolated, "_CPU_CAP_FACTOR", 0.1)
        with IsolatedRuntime(request_timeout=2.0) as rt:
            for _ in range(200):  # warm: the baseline is now the cached reading
                assert rt.eval("1 + 1") == 2
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout, match="CPU in one command"):
                rt.eval("for (;;) {}")
            assert time.monotonic() - start < 2.0, "the wall-clock deadline fired first"
            assert rt.is_closed()

    def test_the_baseline_is_never_newer_than_the_worker_cpu_time(self) -> None:
        from pydeno import _sandbox

        with IsolatedRuntime() as rt:
            for _ in range(50):
                rt.eval(
                    "(() => { let s = 0; for (let i = 0; i < 1e4; i++) s += i; return s })()"
                )
                cached = rt._last_cpu  # noqa: SLF001
                assert cached is not None
                now = _sandbox.cpu_seconds(rt._proc.pid)  # noqa: SLF001
                assert now is not None and cached <= now

    def test_a_stale_baseline_is_read_afresh(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from pydeno import _isolated, _sandbox

        calls: list[int] = []
        real = _sandbox.cpu_seconds

        def spy(pid: int) -> float | None:
            calls.append(pid)
            return real(pid)

        with IsolatedRuntime() as rt:
            rt.eval("1")
            monkeypatch.setattr(_sandbox, "cpu_seconds", spy)
            rt.eval("1")
            assert calls == [], "a fresh baseline was read on the warm path"
            rt._last_cpu_at -= _isolated._CPU_BASELINE_MAX_AGE + 1  # noqa: SLF001
            rt.eval("1")
            assert calls == [rt._proc.pid]  # noqa: SLF001
