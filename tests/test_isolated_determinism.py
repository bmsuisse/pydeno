"""A frozen clock and a seeded `Math.random`: Monty's `os_policy` for the guest's sources of time
and entropy. Frozen time removes the wall clock as a timing source and makes runs reproducible.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from pydeno import IsolatedRuntime, RuntimeConfig

INSTANT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
INSTANT_MS = int(INSTANT.timestamp() * 1000)


def _frozen(**kwargs: object) -> IsolatedRuntime:
    return IsolatedRuntime(RuntimeConfig(timeout=10.0), clock=INSTANT, **kwargs)  # type: ignore[arg-type]


class TestFrozenClock:
    def test_date_now_is_the_instant(self) -> None:
        with _frozen() as rt:
            assert rt.eval("Date.now()") == INSTANT_MS

    def test_new_date_is_the_instant(self) -> None:
        with _frozen() as rt:
            assert rt.eval("new Date().toISOString()") == "2026-01-02T03:04:05.000Z"
            assert rt.eval("new Date().getTime()") == INSTANT_MS

    def test_calling_date_as_a_function_gives_the_frozen_string(self) -> None:
        with _frozen() as rt:
            assert "2026" in rt.eval("Date()")

    def test_time_does_not_advance_in_a_busy_loop(self) -> None:
        with _frozen() as rt:
            assert (
                rt.eval(
                    "const a = Date.now(); for (let i = 0; i < 2e6; i++) {} Date.now() === a"
                )
                is True
            )

    def test_time_does_not_advance_between_evals(self) -> None:
        with _frozen() as rt:
            first = rt.eval("Date.now()")
            time.sleep(0.3)
            assert rt.eval("Date.now()") == first

    def test_explicit_dates_still_work(self) -> None:
        with _frozen() as rt:
            assert rt.eval("new Date(0).getTime()") == 0
            assert rt.eval("new Date(2020, 0, 1).getFullYear()") == 2020
            assert rt.eval("new Date('2000-01-01T00:00:00Z').getTime()") == 946684800000
            assert rt.eval("Date.UTC(2000, 0, 1)") == 946684800000
            assert rt.eval("Date.parse('2000-01-01T00:00:00Z')") == 946684800000

    def test_date_objects_keep_their_identity(self) -> None:
        with _frozen() as rt:
            assert rt.eval("new Date() instanceof Date") is True
            assert rt.eval("Date.prototype.constructor === Date") is True
            assert rt.eval("new Date().constructor === Date") is True
            assert (
                rt.eval("Object.prototype.toString.call(new Date())") == "[object Date]"
            )

    def test_date_can_be_subclassed(self) -> None:
        with _frozen() as rt:
            assert (
                rt.eval(
                    "class Stamp extends Date { tag() { return 't' + this.getUTCFullYear(); } }"
                    " new Stamp().tag()"
                )
                == "t2026"
            )

    def test_the_real_clock_is_not_reachable_through_the_prototype_chain(self) -> None:
        """If `Object.getPrototypeOf(Date)` were the original Date, its `now` would tick."""
        with _frozen() as rt:
            assert rt.eval("Object.getPrototypeOf(Date) === Function.prototype") is True
            assert rt.eval("Date.prototype.constructor.now()") == INSTANT_MS
            assert rt.eval("new Date().constructor.now()") == INSTANT_MS

    def test_intl_format_without_an_argument_uses_the_frozen_time(self) -> None:
        with _frozen() as rt:
            assert (
                rt.eval(
                    "new Intl.DateTimeFormat('en', {timeZone: 'UTC', dateStyle: 'short'}).format()"
                )
                == "1/2/26"
            )
            parts = rt.eval(
                "new Intl.DateTimeFormat('en', {timeZone: 'UTC', year: 'numeric'})"
                ".formatToParts().map(p => p.value).join('')"
            )
            assert parts == "2026"

    def test_the_guests_bootstrap_sees_the_frozen_clock(self) -> None:
        cfg = RuntimeConfig(timeout=10.0, bootstrap="globalThis.boot = Date.now();")
        with IsolatedRuntime(cfg, clock=INSTANT) as rt:
            assert rt.eval("boot") == INSTANT_MS

    def test_a_guest_cannot_unfreeze_it_by_deleting_date(self) -> None:
        with _frozen() as rt:
            rt.eval("delete globalThis.Date")
            assert rt.eval("typeof Date") == "undefined"  # gone, not the real one

    def test_a_frozen_clock_is_per_runtime(self) -> None:
        with _frozen() as frozen, IsolatedRuntime(RuntimeConfig(timeout=10.0)) as live:
            assert frozen.eval("Date.now()") == INSTANT_MS
            assert abs(live.eval("Date.now()") - time.time() * 1000) < 10_000


class TestClockInputs:
    @pytest.mark.parametrize(
        ("clock", "expected_ms"),
        [
            (INSTANT, INSTANT_MS),
            (datetime(2026, 1, 2, 3, 4, 5), INSTANT_MS),  # naive means UTC
            (INSTANT_MS / 1000, INSTANT_MS),  # epoch seconds, float
            (0, 0),
            (1_700_000_000, 1_700_000_000_000),
        ],
    )
    def test_accepted_forms(self, clock: object, expected_ms: int) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0), clock=clock) as rt:  # type: ignore[arg-type]
            assert rt.eval("Date.now()") == expected_ms

    @pytest.mark.parametrize(
        "bad", [float("nan"), float("inf"), "now", True, 1e18, [1]]
    )
    def test_rejected_forms(self, bad: object) -> None:
        with pytest.raises(ValueError, match="clock"):
            IsolatedRuntime(clock=bad)  # type: ignore[arg-type]

    def test_a_zone_aware_datetime_is_converted(self) -> None:
        from datetime import timedelta

        plus_two = timezone(timedelta(hours=2))
        local = datetime(2026, 1, 2, 5, 4, 5, tzinfo=plus_two)  # the same instant
        with IsolatedRuntime(RuntimeConfig(timeout=10.0), clock=local) as rt:
            assert rt.eval("Date.now()") == INSTANT_MS


class TestRandomSeed:
    def _draw(self, seed: int | None) -> list[float]:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0), random_seed=seed) as rt:
            return rt.eval("Array.from({length: 8}, () => Math.random())")

    def test_the_same_seed_gives_the_same_sequence(self) -> None:
        assert self._draw(1234) == self._draw(1234)

    def test_different_seeds_give_different_sequences(self) -> None:
        assert self._draw(1) != self._draw(2)

    def test_unseeded_runtimes_are_not_reproducible(self) -> None:
        assert self._draw(None) != self._draw(None)

    def test_the_values_are_still_in_range(self) -> None:
        values = self._draw(7)
        assert all(0.0 <= v < 1.0 for v in values)
        assert len(set(values)) == len(values)

    @pytest.mark.parametrize("bad", [-1, 2**31, 1.5, True, "7"])
    def test_rejected_seeds(self, bad: object) -> None:
        with pytest.raises(ValueError, match="random_seed"):
            IsolatedRuntime(random_seed=bad)  # type: ignore[arg-type]

    def test_the_seed_is_reported_among_the_v8_flags(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0), random_seed=99) as rt:
            assert "--random-seed=99" in rt.v8_flags


class TestBothTogether:
    def test_a_fully_reproducible_run(self) -> None:
        def run() -> object:
            with IsolatedRuntime(
                RuntimeConfig(timeout=10.0), clock=INSTANT, random_seed=5
            ) as rt:
                return rt.eval(
                    "[Date.now(), Math.random(), new Date().toISOString(), Math.random()]"
                )

        assert run() == run()
