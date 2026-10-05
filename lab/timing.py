"""One timing round: N cold worker starts (spawn, sandbox, isolate, first eval, close) and N
spare-worker starts (the default prewarm path). Prints JSON lists of seconds."""

import json
import sys
import time

from pydeno import IsolatedRuntime

n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
cold, warm, first = [], [], []
for _ in range(n):
    t0 = time.perf_counter()
    rt = IsolatedRuntime(prewarm=False)
    t1 = time.perf_counter()
    rt.eval("1 + 1")
    t2 = time.perf_counter()
    rt.close()
    cold.append(t1 - t0)
    first.append(t2 - t0)
IsolatedRuntime().close()  # fills the spare
time.sleep(0.5)
for _ in range(n):
    t0 = time.perf_counter()
    rt = IsolatedRuntime()
    rt.eval("1 + 1")
    warm.append(time.perf_counter() - t0)
    rt.close()
    time.sleep(0.15)  # let the spare refill, as between requests
print(json.dumps({"cold": cold, "cold_first_eval": first, "spare": warm}))
