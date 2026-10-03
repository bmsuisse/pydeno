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


class TestParentOwnedHandlersValidateWhatTheWorkerSends:
    """The resolver, loader and console handlers are the parent's own; the worker may call them
    with anything it likes, so they only accept what their contract says."""

    @pytest.mark.parametrize(
        "args",
        [(), ("a", "b"), (1,), (None,), ("x\0y",), ("x" * 5000,), (["a"],)],
        ids=["none", "two", "int", "null", "nul-byte", "too-long", "list"],
    )
    def test_a_loader_only_takes_one_bounded_string(self, args: tuple) -> None:
        from pydeno._isolated import _checked_specifiers

        seen: list[tuple] = []
        loader = _checked_specifiers(lambda *a: seen.append(a) or "src", 1)
        with pytest.raises(ValueError, match="invalid module specifier"):
            loader(*args)
        assert seen == []

    def test_a_resolver_takes_two_strings_and_a_good_call_passes_through(self) -> None:
        from pydeno._isolated import _checked_specifiers

        resolver = _checked_specifiers(lambda spec, ref: f"{ref}>{spec}", 2)
        assert resolver("a", "b") == "b>a"
        with pytest.raises(ValueError):
            resolver("a")

    @pytest.mark.parametrize(
        "args",
        [("__init__", []), ("log", "text"), ("log",), ("log", [], 1), (1, [])],
        ids=["dunder-level", "string-args", "missing-args", "extra-arg", "int-level"],
    )
    def test_console_only_takes_a_real_level_and_a_list(self, args: tuple) -> None:
        from pydeno._isolated import _checked_console

        seen: list[tuple] = []
        console = _checked_console(lambda *a: seen.append(a))
        with pytest.raises(ValueError, match="invalid console call"):
            console(*args)
        assert seen == []
        console("warn", ["ok"])
        assert seen == [("warn", ["ok"])]
