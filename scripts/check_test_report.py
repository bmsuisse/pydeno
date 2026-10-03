#!/usr/bin/env python3
"""Fail CI when the test suite *shrinks* instead of when it goes red.

Why this exists: before this script, every "470 tests pass" claim in this repo
was local-only. The stable-release review then found that on a debug build
`pytest tests/` died of a stack overflow at test 46, taking roughly 20 fuzz
tests and nine whole test files with it -- and a naive CI job would have
reported that as a *non-zero exit* at best and, with the wrong flags, as a
pass. A green CI that quietly ran half the suite is worse than no CI, so the
count is checked, not just the colour.

Four independent things are asserted, because each catches a different way a
suite can silently deflate:

1. `--co -q` collected at least `--min-tests` tests, and collection produced
   **no errors**. An import error in one file removes every test in it; pytest
   reports that as an error but the file's tests simply never appear.
2. The JUnit report exists at all. A process that dies from a signal writes
   nothing, so a missing file is itself the M2 failure mode.
3. Every collected test produced a result: `tests == collected`. This is the
   assertion that catches a mid-run process death, a `-x` early exit, or a
   plugin deselecting silently.
4. `skipped <= --max-skipped`, and `failures == errors == 0`. Skips are
   printed with their reasons either way, so a new one is visible in the log
   before it is ever tolerated.

`xfail` is reported by pytest's JUnit writer as a *skip*, but it is a different
thing: a deliberate, documented expectation that something fails (for example
the in-process crashes pinned in `tests/test_monty_parity_security.py`). Counting
it as a skip would force the skip budget above zero and hide real skips behind
it, so xfails are counted separately against their own budget,
`--max-xfailed`, and listed too. A strict xfail that starts passing is a
failure in pytest itself, which is what tells us to move it out of the list.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# `pytest --co -q` ends with e.g. "477 tests collected in 0.78s", or
# "476/477 tests collected (1 deselected) in 0.8s". Either way the number
# before the slash-or-space is what actually got collected.
_COLLECTED_RE = re.compile(r"^(\d+)(?:/\d+)? tests? collected", re.MULTILINE)


def read_collected(collect_log: Path) -> int:
    matches = _COLLECTED_RE.findall(collect_log.read_text())
    if not matches:
        raise SystemExit(
            f"::error::could not find a collection count in {collect_log}. "
            f"`pytest --co -q` did not report one, which usually means it "
            f"crashed or errored during collection."
        )
    return int(matches[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument(
        "--collect-log",
        required=True,
        type=Path,
        help="captured stdout of a prior `pytest --co -q` run",
    )
    parser.add_argument(
        "--min-tests",
        required=True,
        type=int,
        help="floor on the collected count; a `>=` floor so adding tests never "
        "needs a bump, only removing them does",
    )
    parser.add_argument("--max-skipped", type=int, default=0)
    parser.add_argument(
        "--max-xfailed",
        type=int,
        default=0,
        help="budget for deliberate expected failures (pytest.xfail), kept apart "
        "from skips so one cannot hide the other",
    )
    parser.add_argument("--label", default="pytest")
    args = parser.parse_args()

    collected = read_collected(args.collect_log)
    problems: list[str] = []

    if collected < args.min_tests:
        problems.append(
            f"collected {collected} tests, expected at least "
            f"{args.min_tests}. Tests disappeared -- a collection error, a "
            f"renamed file, or a deleted module. If this is a deliberate "
            f"removal, lower MIN_PYTHON_TESTS in the workflow in the same "
            f"commit."
        )

    if not args.junit.is_file():
        print(
            f"::error::{args.label}: no JUnit report at {args.junit}. pytest "
            f"did not finish -- this is what a host-process crash looks like "
            f"(see tests/test_thread_stack_size.py).",
            file=sys.stderr,
        )
        return 1

    suites = ET.parse(args.junit).getroot()
    total = failures = errors = 0
    skips: list[tuple[str, str]] = []
    xfails: list[tuple[str, str]] = []
    for suite in suites.iter("testsuite"):
        total += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
    for case in suites.iter("testcase"):
        for skip in case.iter("skipped"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            # The suite-level `skipped` attribute lumps xfail in with real skips,
            # so classify per test case.
            bucket = xfails if skip.get("type") == "pytest.xfail" else skips
            bucket.append((name, skip.get("message", "")))
    skipped = len(skips)
    xfailed = len(xfails)

    if skips:
        print(f"{args.label}: {skipped} skipped test(s):")
        for name, reason in skips:
            print(f"  - {name}: {reason}")
    if xfails:
        print(f"{args.label}: {xfailed} expected failure(s) (xfail):")
        for name, reason in xfails:
            print(f"  - {name}: {reason}")

    if total != collected:
        problems.append(
            f"collected {collected} tests but the report has {total} "
            f"results -- {collected - total} test(s) never ran. A "
            f"mid-run process death or an early exit."
        )
    if skipped > args.max_skipped:
        problems.append(
            f"{skipped} skipped test(s), budget is {args.max_skipped}. "
            f"Listed above. A skipped test is an unverified test; either fix "
            f"the condition or raise the budget deliberately."
        )
    if xfailed > args.max_xfailed:
        problems.append(
            f"{xfailed} expected failure(s) (xfail), budget is "
            f"{args.max_xfailed}. Listed above. An xfail documents a known gap; "
            f"adding one is a decision, so raise MAX_XFAILED deliberately in "
            f"the same commit."
        )
    if failures or errors:
        problems.append(f"{failures} failure(s) and {errors} error(s).")

    if problems:
        for problem in problems:
            print(f"::error::{args.label}: {problem}", file=sys.stderr)
        return 1

    print(
        f"{args.label}: {total} tests ran, {total - skipped - xfailed} passed, "
        f"{skipped} skipped, {xfailed} expected failure(s), "
        f"floor {args.min_tests}. OK."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
