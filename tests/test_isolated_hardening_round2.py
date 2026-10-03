"""Guest-triggerable failures found in the second review round. Each one used to end the session
(or silently weaken it); each now must leave the runtime usable or refuse loudly."""

import pytest

from pydeno import IsolatedRuntime


@pytest.mark.parametrize(
    "source",
    ["10n**5000n", "[10n**5000n]", "-(10n**100000n)"],
    ids=["bigint-5k-digits", "bigint-in-array", "negative-100k-digits"],
)
def test_a_huge_bigint_result_is_an_error_not_a_dead_worker(source: str) -> None:
    with IsolatedRuntime() as rt:
        with pytest.raises(Exception) as excinfo:
            rt.eval(source)
        assert "died" not in str(excinfo.value)
        assert rt.eval("1 + 1") == 2


def test_temporal_now_follows_the_frozen_clock() -> None:
    # `Temporal.Now` used to return the real wall clock under `clock=`, with nanoseconds.
    with IsolatedRuntime(clock=1_000_000) as rt:
        assert (
            rt.eval("Temporal.Now.instant().epochMilliseconds === Date.now()") is True
        )
        # Two reads can never differ, so it is no timer either.
        assert (
            rt.eval(
                "Temporal.Now.instant().epochNanoseconds === Temporal.Now.instant().epochNanoseconds"
            )
            is True
        )
        assert rt.eval("Temporal.Now.plainDateISO().year") == 1970
        assert rt.eval("Temporal.Now.timeZoneId()") == "UTC"


def test_a_snapshot_is_refused_not_silently_dropped() -> None:
    from pydeno import RuntimeConfig

    with pytest.raises(ValueError, match="snapshot"):
        IsolatedRuntime(RuntimeConfig(snapshot=b"not a real snapshot"))


def test_a_buffer_bomb_is_a_catchable_error_and_the_session_survives() -> None:
    # Without a default `max_buffer_bytes` this was "worker went over max_memory": the RSS poll
    # killed the whole process. The default cap makes V8 throw a RangeError the guest can catch.
    with IsolatedRuntime(max_memory=512 * 1024 * 1024) as rt:
        assert (
            rt.eval(
                "try { new Uint8Array(2 ** 30).fill(1); 'allocated' } catch (e) { e.constructor.name }"
            )
            == "RangeError"
        )
        assert rt.eval("1 + 1") == 2
