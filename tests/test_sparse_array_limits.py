"""The array branch of the V8 -> JSValue serializer must meter, like every other container.

Before this, `value_to_js_value_internal` charged `max_serialization_bytes` for
objects, sets and strings but *not* for arrays. Array holes convert to
`Undefined`, and `Undefined` costs zero bytes, so a sparse array was free at any
length:

    a[10_000_000] = 1   ->  a ten-million-element Python list, zero bytes charged
    a[4294967294] = 1   ->  Vec::with_capacity(4294967295), host killed by the OOM killer

Both are reachable from a single line of guest JavaScript, which makes them a
memory-exhaustion denial of service against the embedding process rather than a
serialization quirk.
"""

import pytest

from pydeno import Runtime, RuntimeConfig

# Comfortably above the per-element cost of a few thousand real elements and
# far below the ~80 MB a ten-million-element array would have to charge.
_SMALL_BYTE_CAP = 1 * 1024 * 1024


def _runtime() -> Runtime:
    return Runtime(RuntimeConfig(max_serialization_bytes=_SMALL_BYTE_CAP))


@pytest.mark.parametrize("index", [1_000_000, 10_000_000])
def test_sparse_array_is_refused_by_the_byte_limit(index: int) -> None:
    """A sparse array costs its `length`, not its populated element count."""
    with _runtime() as rt:
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval(f"(() => {{ const a = []; a[{index}] = 1; return a; }})()")


def test_max_length_sparse_array_does_not_allocate() -> None:
    """The pathological case: a guest-chosen four-billion-element reservation.

    This used to reach `Vec::with_capacity` and take the process out with
    SIGKILL, so the meaningful assertion is that the call returns at all.
    """
    with _runtime() as rt:
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval("(() => { const a = []; a[4294967294] = 1; return a; })()")


def test_sparse_array_as_an_op_argument_is_refused_too() -> None:
    """The inbound (guest -> host tool) direction meters the same way."""
    with _runtime() as rt:
        op_id = rt.register_op("sink", lambda *args: "sunk", mode="sync")
        with pytest.raises(Exception, match="Serialization size"):
            rt.eval(
                f"(() => {{ const a = []; a[10000000] = 1;"
                f" return __host_op_sync__({op_id}, a); }})()"
            )


def test_dense_arrays_within_the_budget_still_work() -> None:
    """The fix must not make ordinary arrays cost more than they are worth."""
    with _runtime() as rt:
        assert rt.eval("Array.from({length: 1000}, (_, i) => i)") == list(range(1000))


def test_array_bytes_are_charged_against_the_shared_budget() -> None:
    """An array large enough to exceed the cap is refused even when dense."""
    with _runtime() as rt:
        with pytest.raises(RuntimeError, match="Serialization size"):
            rt.eval("new Array(1000000).fill(0)")
