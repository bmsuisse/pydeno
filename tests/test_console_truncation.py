"""#141: a console line too large for one frame sets `truncated`; console does not spend `max_host_calls`."""

from __future__ import annotations

import pytest

from pydeno import AgentSandbox, IsolatedRuntime, RuntimeConfig

HUGE = 2**24  # exactly the frame cap: the smallest line that used to vanish


@pytest.mark.parametrize("n", [70_000, 2**20, HUGE, 2**26])
def test_big_line_is_truncated_not_dropped(n: int) -> None:
    with AgentSandbox({}) as s:
        r = s.execute(f"console.log('x'.repeat({n})); return 1")
    assert r.status == "Succeeded"
    assert r.truncated is True
    # A bounded prefix, as for lines between 64 KiB and 16 MiB.
    assert 0 < len(r.stdout) < 100_000
    assert r.stdout.startswith("xxxx")
    assert r.stdout.rstrip().endswith("[truncated]")


def test_line_between_cap_and_frame_is_unchanged() -> None:
    with AgentSandbox({}) as s:
        r = s.execute("console.log('x'.repeat(2**20)); return 1")
    assert r.truncated is True and 0 < len(r.stdout) < 100_000


def test_small_output_is_not_truncated() -> None:
    with AgentSandbox({}) as s:
        r = s.execute("console.log('a'); console.log('b'); return 1")
    assert r.stdout == "a\nb\n"
    assert r.truncated is False


def test_surrounding_lines_survive_up_to_the_cut() -> None:
    with AgentSandbox({}) as s:
        r = s.execute(
            "console.log('a'); console.log('x'.repeat(2**26)); console.log('b'); return 1"
        )
    assert r.truncated is True
    assert r.stdout.startswith("a\nxxxx")
    assert r.stdout.rstrip().endswith("[truncated]")


def test_stderr_stream_is_flagged_too() -> None:
    with AgentSandbox({}) as s:
        r = s.execute(f"console.error('e'.repeat({HUGE})); return 1")
    assert r.truncated is True
    assert r.stderr.startswith("eeee") and r.stderr.rstrip().endswith("[truncated]")
    assert r.stdout == ""


def test_several_large_arguments_are_cut_to_a_bounded_prefix() -> None:
    with AgentSandbox({}) as s:
        r = s.execute(
            f"const big = 'y'.repeat({HUGE // 2}); console.log(big, big, big); return 1"
        )
    assert r.truncated is True
    assert len(r.stdout) < 100_000


def test_session_survives_and_next_run_is_clean() -> None:
    with AgentSandbox({}) as s:
        s.execute(f"console.log('x'.repeat({HUGE})); return 1")
        r = s.execute("console.log('ok'); return 2")
    assert r.stdout == "ok\n"
    assert r.truncated is False


def test_isolated_execute_flags_it_too() -> None:
    with IsolatedRuntime(capture_console=True) as rt:
        r = rt.execute(f"console.log('x'.repeat({HUGE})); 1")
    assert r.truncated is True
    assert r.stdout.rstrip().endswith("[truncated]")


def test_on_console_callback_sees_the_prefix_once() -> None:
    seen: list[tuple[str, list[object]]] = []
    cfg = RuntimeConfig(on_console=lambda level, args: seen.append((level, args)))
    with IsolatedRuntime(cfg) as rt:
        rt.eval(f"console.log('x'.repeat({HUGE})); 1")
    assert len(seen) == 1
    level, args = seen[0]
    assert level == "log"
    assert isinstance(args[0], str) and 0 < len(args[0]) < HUGE


def test_callback_error_does_not_repeat_the_call() -> None:
    calls: list[object] = []

    def boom(level: str, args: list[object]) -> None:
        calls.append(args)
        raise RuntimeError("callback broke")

    with IsolatedRuntime(RuntimeConfig(on_console=boom)) as rt:
        assert rt.eval("console.log('hi'); 1") == 1
    assert len(calls) == 1


def test_console_does_not_count_against_max_host_calls() -> None:
    with IsolatedRuntime(capture_console=True, max_host_calls=3) as rt:
        r = rt.execute("for (let i = 0; i < 50; i++) console.log(i); 7")
    assert r.status == "Succeeded"
    assert r.result == 7
    assert r.stdout.count("\n") == 50


def test_tools_still_count_against_max_host_calls() -> None:
    with IsolatedRuntime(capture_console=True, max_host_calls=3) as rt:
        rt.bind_function("f", lambda: 1)
        with pytest.raises(Exception, match="max_host_calls"):
            rt.eval("for (let i = 0; i < 10; i++) { f(); console.log(i) } 1")


@pytest.mark.asyncio
async def test_async_runtime_console_not_counted() -> None:
    from pydeno import AsyncIsolatedRuntime

    seen: list[object] = []
    cfg = RuntimeConfig(on_console=lambda level, args: seen.append(args))
    async with AsyncIsolatedRuntime(cfg, max_host_calls=3) as rt:
        assert (
            await rt.eval_async("for (let i = 0; i < 10; i++) console.log(i); 1") == 1
        )
    assert len(seen) == 10


def test_plain_runtime_callback_without_a_third_parameter_gets_the_prefix() -> None:
    from pydeno import Runtime

    seen: list[int] = []
    cfg = RuntimeConfig(on_console=lambda level, args: seen.append(len(args[0])))
    Runtime(cfg).eval(f"console.log('x'.repeat({HUGE})); 1")
    assert len(seen) == 1 and 0 < seen[0] < 2**21
