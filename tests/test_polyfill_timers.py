"""The timer polyfill (`WEB_POLYFILLS`): ordering, budgets that reset, and cost.

Found by review: the budget was a lifetime counter (a reused runtime's later timers silently never
fired), firing and clearing were O(n) per timer, and `structuredClone` assigned `__proto__` as an
ordinary key.
"""

import time

import pytest

from pydeno import WEB_POLYFILLS, IsolatedRuntime, RuntimeConfig


@pytest.fixture(scope="module")
def rt():  # type: ignore[no-untyped-def]
    with IsolatedRuntime(
        RuntimeConfig(bootstrap=WEB_POLYFILLS, timeout=60.0)
    ) as runtime:
        yield runtime


def _run(rt, code: str):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(rt.eval_async(code, timeout=60))


def test_timers_fire_in_due_order_then_insertion_order(rt) -> None:  # type: ignore[no-untyped-def]
    out = _run(
        rt,
        """new Promise((resolve) => {
          const seen = [];
          setTimeout(() => seen.push('c'), 30);
          setTimeout(() => seen.push('a1'), 10);
          setTimeout(() => seen.push('a2'), 10);
          setTimeout(() => seen.push('b'), 20);
          setTimeout(() => resolve(seen.join(',')), 40);
        })""",
    )
    assert out == "a1,a2,b,c"


def test_clearing_a_timer_stops_it_and_is_cheap(rt) -> None:  # type: ignore[no-untyped-def]
    start = time.monotonic()
    out = _run(
        rt,
        """new Promise((resolve) => {
          let hits = 0;
          const ids = [];
          for (let i = 0; i < 50000; i++) ids.push(setTimeout(() => { hits++; }, 100 + i));
          for (const id of ids) clearTimeout(id);
          setTimeout(() => resolve(hits), 1);
        })""",
    )
    assert out == 0
    assert (
        time.monotonic() - start < 8
    )  # was O(n) per clear: ~4.5 s for 50k on the old queue


def test_forty_thousand_timers_are_cheap(rt) -> None:  # type: ignore[no-untyped-def]
    start = time.monotonic()
    out = _run(
        rt,
        """new Promise((resolve) => {
          let n = 0;
          for (let i = 0; i < 40000; i++) setTimeout(() => { if (++n === 40000) resolve(n); }, i % 97);
        })""",
    )
    assert out == 40000
    assert time.monotonic() - start < 8  # was ~1.5 s per fire-scan before the heap


def test_the_budget_resets_when_the_queue_drains(rt) -> None:  # type: ignore[no-untyped-def]
    burst = "new Promise((resolve) => { let n = 0; const id = setInterval(() => { if (++n === 60000) { clearInterval(id); resolve(n); } }, 1); })"
    assert _run(rt, burst) == 60000
    assert (
        _run(rt, burst) == 60000
    )  # a lifetime budget of 100000 would have stopped the second
    # and an ordinary timer afterwards still fires
    assert (
        _run(rt, "new Promise((resolve) => setTimeout(() => resolve('fired'), 1))")
        == "fired"
    )


def test_a_runaway_interval_ends_and_the_runtime_still_works() -> None:
    with IsolatedRuntime(RuntimeConfig(bootstrap=WEB_POLYFILLS, timeout=60.0)) as rt:
        import asyncio

        asyncio.run(rt.eval_async("setInterval(() => {}, 1); 1", timeout=60))
        time.sleep(0.5)
        out = asyncio.run(
            rt.eval_async(
                "new Promise((r) => setTimeout(() => r('ok'), 1))", timeout=20
            )
        )
        assert out == "ok"


def test_too_many_pending_timers_is_a_visible_error(rt) -> None:  # type: ignore[no-untyped-def]
    out = _run(
        rt,
        """(() => {
          try { for (let i = 0; i < 100001; i++) setTimeout(() => {}, 1000000); return 'no error'; }
          catch (e) { return e.constructor.name; }
        })()""",
    )
    assert out == "RangeError"


def test_an_infinite_delay_does_not_poison_virtual_time(rt) -> None:  # type: ignore[no-untyped-def]
    out = _run(
        rt,
        "new Promise((resolve) => { setTimeout(() => {}, Infinity); setTimeout(() => resolve(performance.now()), 5); })",
    )
    assert isinstance(out, (int, float)) and out == out and out != float("inf")


def test_structured_clone_keeps_a_proto_key_as_data(rt) -> None:  # type: ignore[no-untyped-def]
    out = _run(
        rt,
        """(() => {
          const src = JSON.parse('{"__proto__": {"x": 1}, "a": 2}');
          const c = structuredClone(src);
          return [Object.getPrototypeOf(c) === Object.prototype, Object.keys(c).join(','), c.a];
        })()""",
    )
    assert out == [True, "__proto__,a", 2]
