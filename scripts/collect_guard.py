#!/usr/bin/env python3
"""Run pytest, but if it has not finished within N seconds, dump every thread's traceback and exit.

Used for the CI "Collect tests" steps. Collecting ~2000 tests takes about a second, so a collection
that runs for minutes is a hang, and a hang that just sits there is the worst way to find out: the
job stays "in progress" for hours with nothing in the log. This turns it into a failure that says
where it was stuck.

    python scripts/collect_guard.py 180 tests/ --co -q ...
"""

import faulthandler
import runpy
import sys

if __name__ == "__main__":
    faulthandler.dump_traceback_later(int(sys.argv[1]), exit=True)
    sys.argv = ["pytest", *sys.argv[2:]]
    runpy.run_module("pytest", run_name="__main__")
