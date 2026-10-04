"""Converting a guest result must be charged before it is expanded (issue #75, slice A).

Typed arrays other than `Uint8Array` (and `DataView`s, and boxed strings) have no dedicated branch
in the V8 -> JSValue converter, so they go through the generic object branch, which first asks V8
for every own property name. Those names are virtual: an `Int8Array` of 2**24 elements is 16 MB of
storage and 16 million index strings once listed, hundreds of megabytes of heap. The listing is
one native call that termination cannot interrupt, so a 2-second deadline ran for 7 to 12 seconds.
The serialization byte budget was only charged per key afterwards, too late to help.

Arrays already charge their length before walking; these values now do the same.
"""

from __future__ import annotations

import time

import pytest

from pydeno import IsolatedRuntime, Runtime, RuntimeConfig

TIMEOUT = 10.0
# Far below the deadline, far above what refusing up front costs.
QUICK = 3.0

_INDEXED = [
    "new Int8Array(2 ** 24)",
    "new Float64Array(2 ** 24)",
    "new Uint16Array(2 ** 24)",
    "new Uint8ClampedArray(2 ** 24)",
    "new String('x'.repeat(2 ** 24))",
]


@pytest.mark.parametrize("expr", _INDEXED)
def test_a_huge_indexed_result_is_refused_before_it_is_listed(expr: str) -> None:
    with Runtime(RuntimeConfig(timeout=TIMEOUT)) as rt:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval(expr)
        assert time.monotonic() - started < QUICK


def test_a_huge_typed_array_result_is_refused_by_the_isolated_worker() -> None:
    with IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT), request_timeout=TIMEOUT * 2
    ) as rt:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval("new Float64Array(2 ** 24)")
        assert time.monotonic() - started < QUICK
        assert rt.eval("1 + 1") == 2  # the session survives the refusal


def test_small_typed_arrays_and_boxed_strings_convert_as_before() -> None:
    with Runtime() as rt:
        assert rt.eval("new Float64Array([1.5, 2])") == {"0": 1.5, "1": 2}
        assert rt.eval("new Int16Array([-1])") == {"0": -1}
        assert rt.eval("new String('ab')") == {"0": "a", "1": "b"}
        assert rt.eval("new Uint8Array([1, 2])") == b"\x01\x02"


@pytest.mark.parametrize("expr", ["2 ** 63", "2 ** 64", "-(2 ** 64)", "1e300"])
def test_a_number_outside_int64_is_not_silently_clamped(expr: str) -> None:
    """`2 ** 63` used to come back as `2 ** 63 - 1`: the float-to-int cast saturates, and the
    round-trip check compared the saturated value after converting it back to a float, where
    `i64::MAX as f64` rounds up to `2 ** 63` again."""
    with Runtime() as rt:
        out = rt.eval(expr)
        assert out == float(rt.eval(f"String({expr})"))
        assert out != 2**63 - 1


def test_int64_boundaries_still_come_back_as_ints() -> None:
    with Runtime() as rt:
        assert rt.eval("-(2 ** 63)") == -(2**63)
        assert rt.eval("2 ** 53") == 2**53
        assert isinstance(rt.eval("2 ** 62"), int)


@pytest.mark.parametrize("factory", ["inprocess", "isolated"])
def test_a_huge_boxed_string_argument_is_refused_before_it_is_listed(
    factory: str,
) -> None:
    """The bridge copies a host-call argument with `Object.entries`, which lists a boxed string's
    characters in one native call; the node cap was only checked per entry afterwards."""
    if factory == "inprocess":
        rt = Runtime(RuntimeConfig(timeout=TIMEOUT))
    else:
        rt = IsolatedRuntime(
            RuntimeConfig(timeout=TIMEOUT), request_timeout=TIMEOUT * 2
        )
    with rt:
        rt.bind_function("f", lambda *a: len(a))
        started = time.monotonic()
        with pytest.raises(Exception, match="too large"):
            rt.eval("f(new String('x'.repeat(2 ** 24)))")
        assert time.monotonic() - started < QUICK
        assert rt.eval("f(new String('ab'))") == 1  # small ones still cross
