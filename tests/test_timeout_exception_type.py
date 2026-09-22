"""`RuntimeTimeout`: a timeout you can catch by type, not by substring.

Through 0.4.0 `RuntimeError::Timeout` mapped to a bare `PyRuntimeError`, the
same type an internal failure produces, so the only way to tell the two apart
was to match `"timed out"` in the message -- which this repo's own tests did
(`tests/test_microtask_timeout.py`). A message is not an API: rewording it for
clarity would silently break every caller keying off it.

`RuntimeTimeout` subclasses `RuntimeError`, following `RuntimeForceKilled`
subclassing `RuntimeTerminated`, so this is additive: every existing
`except RuntimeError` keeps catching timeouts unchanged. That compatibility is
the point of the type, so it is asserted here rather than assumed.
"""

from __future__ import annotations

import asyncio

import pytest

import pydeno
from pydeno import Runtime, RuntimeConfig, RuntimeTimeout

# Spins forever with no microtasks and no I/O: only the deadline stops it.
_RUNAWAY = "while (true) {}"


def test_runtime_timeout_is_exported() -> None:
    assert pydeno.RuntimeTimeout is RuntimeTimeout
    assert "RuntimeTimeout" in pydeno.__all__
    assert RuntimeTimeout.__module__ == "pydeno"


def test_runtime_timeout_subclasses_runtime_error() -> None:
    """The compatibility guarantee, stated as an assertion: existing callers
    written against 0.4.0 catch `RuntimeError`, and must keep working."""
    assert issubclass(RuntimeTimeout, RuntimeError)


def test_sync_timeout_raises_runtime_timeout() -> None:
    with Runtime(RuntimeConfig(timeout=0.2)) as rt:
        with pytest.raises(RuntimeTimeout) as exc_info:
            rt.eval(_RUNAWAY)
        assert "timed out" in str(exc_info.value)


def test_sync_timeout_still_caught_as_runtime_error() -> None:
    with Runtime(RuntimeConfig(timeout=0.2)) as rt:
        with pytest.raises(RuntimeError):
            rt.eval(_RUNAWAY)


@pytest.mark.asyncio
async def test_async_timeout_raises_runtime_timeout() -> None:
    with Runtime(RuntimeConfig(timeout=0.2)) as rt:
        with pytest.raises(RuntimeTimeout):
            await rt.eval_async(_RUNAWAY)


def test_internal_errors_are_not_runtime_timeout() -> None:
    """The type has to actually discriminate: a non-timeout `RuntimeError`
    from the same runtime must not be catchable as a timeout."""
    with Runtime(RuntimeConfig(max_serialization_depth=2)) as rt:
        with pytest.raises(RuntimeError) as exc_info:
            rt.eval("({a: {b: {c: {d: 1}}}})")
        assert not isinstance(exc_info.value, RuntimeTimeout)


def test_asyncio_timeout_error_is_unrelated() -> None:
    """Deliberately not named `TimeoutError`: Python's builtin derives from
    `OSError`, so the two hierarchies must stay disjoint."""
    assert not issubclass(RuntimeTimeout, TimeoutError)
    assert not issubclass(RuntimeTimeout, asyncio.TimeoutError)
