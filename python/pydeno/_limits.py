"""Validation of limit values, shared by every layer that accepts one.

A limit is a comparison (`elapsed > deadline`, `rss > max_memory`), and every comparison with NaN
is false, so a NaN limit never fires and nothing says so. Infinity is a unit mistake or a config
typo, not a way to say "no limit" (that is `None`). Python's `json` parses both, so they can come
from a configuration file. Errors are uniform: `TypeError` for a value of the wrong type,
`ValueError` for one of the right type out of range.

Numbers that are numbers keep working: any `numbers.Real` (and `decimal.Decimal`) as seconds, any
integer-like (`operator.index`: `int`, numpy integers) as a count. `bool` is neither.
"""

from __future__ import annotations

# PEP 810 (Python 3.15): these stdlib modules are loaded on first use, not at import. A plain
# list, so it is inert on 3.10-3.14. Never list what the isolation worker imports before it
# applies its sandbox (`_worker`, `_sandbox`, `_wire`, `_wasm`, `_awaitable`): a lazy import
# there would run after the sandbox closed the filesystem.
__lazy_modules__ = ["decimal"]

import decimal
import math
import numbers
import operator
import threading
from datetime import timedelta
from typing import Any

#: The largest integer the wire carries as a plain JSON number (see `_wire`).
MAX_WIRE_INT = 2**53 - 1
#: The longest duration any limit may have. A deadline is eventually a timed wait
#: (`Lock.acquire(timeout=...)`), which refuses more than `threading.TIMEOUT_MAX`; a deadline plus
#: its grace plus another grace must still fit, hence the quarter (still about 70 years).
MAX_SECONDS = threading.TIMEOUT_MAX / 4


def limit_seconds(name: str, value: Any, *, allow_zero: bool = False) -> float | None:
    """None, or a finite number of seconds in (0, MAX_SECONDS] ([0, ...] with `allow_zero`), from a
    real number or a `timedelta`."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(
        value, (numbers.Real, decimal.Decimal, timedelta)
    ):
        raise TypeError(f"{name} must be a number of seconds, a timedelta, or None")
    try:
        seconds = (
            value.total_seconds() if isinstance(value, timedelta) else float(value)
        )
    except (OverflowError, ValueError, decimal.InvalidOperation):
        seconds = math.nan
    low = ">= 0" if allow_zero else "> 0"
    if (
        not math.isfinite(seconds)
        or seconds < 0
        or (seconds == 0 and not allow_zero)
        or seconds > MAX_SECONDS
    ):
        raise ValueError(
            f"{name} must be a finite number of seconds {low} and at most {MAX_SECONDS:.0f}, "
            "or None"
        )
    return 0.0 if seconds == 0 else seconds


def limit_int(
    name: str, value: Any, *, minimum: int, maximum: int = MAX_WIRE_INT
) -> int | None:
    """None, or an integer in [minimum, maximum] (any integer-like; never a bool or a float)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an int or None, not a bool")
    try:
        number = operator.index(value)
    except TypeError:
        raise TypeError(f"{name} must be an int or None") from None
    if number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if number > maximum:
        suffix = " (2**53 - 1)" if maximum == MAX_WIRE_INT else ""
        raise ValueError(f"{name} must be at most {maximum}{suffix}")
    return number
