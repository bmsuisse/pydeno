#!/usr/bin/env bash
# Record which system calls a real IsolatedRuntime worker makes, before and after its seccomp
# filter goes up, by running the isolation test suites under `strace -f` inside a Linux container.
#
#   scripts/trace_worker_syscalls.sh OUT_JSON IMAGE [IMAGE...]
#
#   OUT_JSON  where to write the merged result, e.g. tests/data/worker_syscalls_x86_64.json
#   IMAGE     a Debian/Ubuntu-family image with Python >= 3.10 (python:3.12-slim, ubuntu:24.04, ...);
#             one container per image, the results are merged (several images = several CPythons)
#
# Environment:
#   WHEELS             directory of manylinux wheels to install (pip picks the match), or
#   PYDENO_SPEC        a pip requirement to install from PyPI instead (e.g. "pydeno==0.6.1");
#                      either way ./python/pydeno/*.py is copied over the installed package, so
#                      what is traced is the sandbox in this checkout
#   PYTEST_TARGETS     what to run (default: the isolation and example suites)
#   CONTAINER_RUNTIME  podman or docker (default: whichever is on PATH, podman first)
#   EXTRA_RUN_ARGS     extra flags for `run`
#
# Why this exists: the seccomp filter (`pydeno/_sandbox.py`) is written by intent. This is the
# measured side: every syscall a worker made after the filter was in force (with the errors the
# filter or kernel answered), which is what an allow-list must keep open and what a kill rule must
# never match. A worker is followed from its `execve` of `pydeno._worker`; every thread and every
# child it starts inherits its phase ("before" or "after" the filter), so the record is exact even
# though threads interleave in the trace. The architecture is the host's: containers share its
# kernel, so run this on a native x86_64 machine for x86_64 and a native aarch64 one for aarch64.
# Emulated amd64 (qemu, Rosetta) does not run the filter the way a real x86_64 kernel does.
set -euo pipefail

OUT_JSON=${1:?usage: trace_worker_syscalls.sh OUT_JSON IMAGE [IMAGE...]}
shift
[ "$#" -ge 1 ] || { echo "usage: trace_worker_syscalls.sh OUT_JSON IMAGE [IMAGE...]" >&2; exit 2; }
if [ -z "${WHEELS:-}" ] && [ -z "${PYDENO_SPEC:-}" ]; then
  echo "set WHEELS=<dir of manylinux wheels> or PYDENO_SPEC=<pip requirement>" >&2
  exit 2
fi

RUNTIME=${CONTAINER_RUNTIME:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
TARGETS=${PYTEST_TARGETS:-"$(cd "$ROOT" && echo tests/test_isolated_*.py tests/test_example_*.py)"}
WHEEL_MOUNT=()
if [ -n "${WHEELS:-}" ]; then
  WHEEL_MOUNT=(-v "$(cd "$WHEELS" && pwd):/wheels:ro")
fi

# ------------------------------------------------------------------ the trace parser (in-container)
cat > "$WORK/parse.py" <<'PARSE_EOF'
"""Read `strace -f -q` output on stdin; write what worker processes did, as JSON, to argv[1].

Each tid's events are kept as segments of syscall counts separated by the rare events that
matter (a successful seccomp filter install, a clone, a worker execve), so a multi-GB trace
needs only memory proportional to the number of threads."""
import collections
import json
import os
import re
import sys

LINE = re.compile(r"^(\d+)\s+(.*)$")
START = re.compile(r"^([a-z_0-9]+)\((.*)$")
RESUMED = re.compile(r"^<\.\.\. ([a-z_0-9]+) resumed>(.*)$")
RESULT = re.compile(
    r"\)\s+=\s+(-?\d+|0x[0-9a-f]+|\?)(?:\s+(E[A-Z0-9_]+))?(?:\s+\([^)]*\))?(?:\s+<[^>]*>)?\s*$"
)
CLONES = {"clone", "clone3", "fork", "vfork"}

# per tid: list of events; an event is ("calls", Counter) or a marker tuple
events = collections.defaultdict(list)
pending = {}  # tid -> (name, args) of an <unfinished ...> call
sigsys_tids = set()


def calls(tid):
    ev = events[tid]
    if not ev or ev[-1][0] != "calls":
        ev.append(("calls", collections.Counter()))
    return ev[-1][1]


def finish(tid, name, args, tail):
    m = RESULT.search(tail)
    ret, err = (m.group(1), m.group(2)) if m else ("?", None)
    calls(tid)[(name, err)] += 1
    if name == "seccomp" and ret == "0" and "SECCOMP_SET_MODE_FILTER" in args:
        events[tid].append(("filter",))
    elif name in CLONES and ret.isdigit() and int(ret) > 0:
        events[tid].append(("clone", int(ret), "CLONE_THREAD" in args))
    elif name == "execve" and ret == "0":
        events[tid].append(("exec", "pydeno._worker" in args))


for raw in sys.stdin:
    m = LINE.match(raw.rstrip("\n"))
    if not m:
        continue
    tid, rest = int(m.group(1)), m.group(2)
    if rest.startswith("+++ killed by SIGSYS"):
        sigsys_tids.add(tid)
        continue
    if rest.startswith(("+++", "---")):
        continue
    r = RESUMED.match(rest)
    if r:
        name, args = pending.pop(tid, (r.group(1), ""))
        finish(tid, name, args + r.group(2), r.group(2))
        continue
    s = START.match(rest)
    if not s:
        continue
    name, tail = s.group(1), s.group(2)
    if tail.endswith("<unfinished ...>"):
        pending[tid] = (name, tail)
        continue
    finish(tid, name, tail, tail)

# Resolve: who created whom, and in which phase. A tid starts in its creator's state at the
# moment of the clone, then changes state at its own markers.
creator = {}
for tid, evs in events.items():
    for ev in evs:
        if ev[0] == "clone":
            creator.setdefault(ev[1], (tid, ev))

after = collections.defaultdict(collections.Counter)
before = collections.Counter()
workers = set()
memo = {}


def initial_state(tid):
    # (is_worker, filtered)
    if tid in memo:
        return memo[tid]
    memo[tid] = (False, False)  # cycle guard
    state = (False, False)
    if tid in creator:
        parent, marker = creator[tid]
        worker, filtered = initial_state(parent)
        for ev in events[parent]:
            if ev is marker:
                break
            if ev[0] == "filter":
                filtered = True
            elif ev[0] == "exec":
                worker, filtered = ev[1], False
        state = (worker, filtered)
    memo[tid] = state
    return state


killed = 0
for tid, evs in events.items():
    worker, filtered = initial_state(tid)
    for ev in evs:
        if ev[0] == "filter":
            filtered = True
        elif ev[0] == "exec":
            worker, filtered = ev[1], False
            if worker:
                workers.add(tid)
        elif ev[0] == "calls" and worker:
            for (name, err), n in ev[1].items():
                if filtered:
                    after[name][err or "ok"] += n
                else:
                    before[name] += n
    if worker and tid in sigsys_tids:
        killed += 1

with open(sys.argv[1] + ".part", "w") as fh:  # renamed when complete: the driver polls for it
    json.dump(
        {
            "workers": len(workers),
            "killed_by_sigsys": killed,  # threads of worker processes that died by SIGSYS
            "after_filter": {k: dict(v) for k, v in sorted(after.items())},
            "before_filter": dict(sorted(before.items())),
        },
        fh,
        indent=1,
    )
os.replace(sys.argv[1] + ".part", sys.argv[1])
print(
    f"traced {len(workers)} workers, {len(after)} syscalls after the filter, "
    f"{killed} SIGSYS deaths",
    file=sys.stderr,
)
PARSE_EOF

# ------------------------------------------------------------------ the in-container driver
cat > "$WORK/inner.sh" <<'INNER_EOF'
#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq strace >/dev/null
command -v python3 >/dev/null 2>&1 || apt-get install -y -qq python3 >/dev/null
python3 -c 'import venv' 2>/dev/null && python3 -m venv /tmp/v 2>/dev/null \
  || { apt-get install -y -qq python3-venv >/dev/null; python3 -m venv /tmp/v; }
if [ -d /wheels ]; then
  /tmp/v/bin/pip install -q --no-index --find-links /wheels pydeno >/dev/null
else
  /tmp/v/bin/pip install -q "$PYDENO_SPEC" >/dev/null
fi
/tmp/v/bin/pip install -q "pytest>=8.4.0" "pytest-asyncio>=1.2.0" "hypothesis>=6.100.0" >/dev/null
/tmp/v/bin/pip install -q "pydantic-monty>=1.0" >/dev/null 2>&1 || echo "(no pydantic-monty here; its examples are deselected)"
SITE=$(/tmp/v/bin/python -c "import pydeno,os;print(os.path.dirname(pydeno.__file__))")
cp /src/python/pydeno/*.py "$SITE"/
mkdir -p /work && cp -r /src/tests /src/vendor /src/examples /src/pyproject.toml /work/ && cd /work
/tmp/v/bin/python - <<'PY' > /out/meta.json
import json, os, platform
from importlib.metadata import version
print(json.dumps({
    "python": platform.python_version(),
    "pydeno_wheel": version("pydeno"),
    "kernel": os.uname().release,
    "machine": os.uname().machine,
    "os": open("/etc/os-release").read().split("PRETTY_NAME=")[1].split("\n")[0].strip('"'),
}))
PY
cat /out/meta.json
set +e
# -f: follow every thread and child; -q: no attach/detach noise, but keep "+++ killed by" lines;
# signal=none: deliveries are not syscalls. The trace streams into the parser, never to disk.
# shellcheck disable=SC2086
strace -f -q -e trace=all -e signal=none -s 64 -o "|/tmp/v/bin/python /out/parse.py /out/trace.json" \
  /tmp/v/bin/python -m pytest $PYTEST_TARGETS -q --no-header -p no:randomly -p no:cacheprovider \
  > /tmp/pytest.out 2>&1
grep -E "[0-9]+ (passed|failed|error)" /tmp/pytest.out | tail -1 > /out/pytest.tail || tail -1 /tmp/pytest.out > /out/pytest.tail
cat /out/pytest.tail
# strace exits when pytest does; the parser may still be writing
for _ in $(seq 1 120); do [ -s /out/trace.json ] && break; sleep 1; done
[ -s /out/trace.json ]
INNER_EOF

# ------------------------------------------------------------------ one container per image
n=0
for IMAGE in "$@"; do
  n=$((n + 1))
  RUN_OUT="$WORK/run$n"
  mkdir -p "$RUN_OUT"
  cp "$WORK/parse.py" "$WORK/inner.sh" "$RUN_OUT/"
  echo ">>> tracing on $IMAGE ($RUNTIME, host $(uname -m))"
  # SYS_PTRACE for strace; seccomp=unconfined so the container runtime's own profile does not
  # answer for the worker's filter (and does not hide ptrace from strace).
  # shellcheck disable=SC2086
  "$RUNTIME" run --rm ${EXTRA_RUN_ARGS:-} \
    --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
    -v "$ROOT:/src:ro" ${WHEEL_MOUNT[@]+"${WHEEL_MOUNT[@]}"} -v "$RUN_OUT:/out" \
    -e PYTEST_TARGETS="$TARGETS" -e PYDENO_SPEC="${PYDENO_SPEC:-}" \
    "$IMAGE" sh /out/inner.sh
  echo "$IMAGE" > "$RUN_OUT/image"
done

# ------------------------------------------------------------------ merge (host)
python3 - "$ROOT" "$OUT_JSON" "$WORK" "$n" <<'MERGE_EOF'
import collections, json, pathlib, re, subprocess, sys

root, out_path, work, n = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3]), int(sys.argv[4])
lock = (root / "Cargo.lock").read_text()


def locked(name):
    m = re.search(rf'name = "{re.escape(name)}"\nversion = "([^"]+)"', lock)
    return m.group(1) if m else "?"


try:
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    commit = "?"

after = collections.defaultdict(collections.Counter)
before = collections.Counter()
runs, machines = [], set()
for i in range(1, n + 1):
    d = work / f"run{i}"
    meta = json.loads((d / "meta.json").read_text())
    trace = json.loads((d / "trace.json").read_text())
    machines.add(meta["machine"])
    for name, errs in trace["after_filter"].items():
        after[name].update(errs)
    before.update(trace["before_filter"])
    runs.append({
        "image": (d / "image").read_text().strip(),
        **meta,
        "workers_traced": trace["workers"],
        "workers_killed_by_sigsys": trace["killed_by_sigsys"],
        "pytest": (d / "pytest.tail").read_text().strip().splitlines()[-1:],
    })
if len(machines) != 1:
    sys.exit(f"runs disagree on the architecture: {machines}")
arch = machines.pop()
tables = json.loads((root / "tests" / "data" / "syscalls.json").read_text())
present = set(tables[arch].values())
# vDSO-backed: answered in user space, so they never show in a trace, but some hypervisors and
# clock sources make the vDSO fall back to the real syscall. They have to stay open regardless.
vdso = sorted(n for n in ("clock_gettime", "clock_getres", "gettimeofday", "getcpu", "time")
              if n in present)
probed = sorted(n for n, errs in after.items() if {"EPERM", "ENOSYS"} & set(errs))
doc = {
    "about": (
        "Syscalls made by IsolatedRuntime workers during the isolation and example test suites, "
        "recorded with strace -f by scripts/trace_worker_syscalls.sh. after_filter: calls made "
        "once the seccomp filter was in force, with how each was answered ('ok' or an errno); "
        "probed: calls that were refused there (legitimate code that tolerates a refusal, so "
        "they must stay errno, never kill); before_filter: start-up calls the filter never sees; "
        "vdso_fallback: added by hand, never in a trace. Regenerate after a V8, deno_core or "
        "worker start-up change."
    ),
    "arch": arch,
    "versions": {"v8": locked("v8"), "deno_core": locked("deno_core"), "source_commit": commit},
    "runs": runs,
    "after_filter": {k: dict(sorted(v.items())) for k, v in sorted(after.items())},
    "probed": probed,
    "vdso_fallback": vdso,
    "before_filter": sorted(before),
}
pathlib.Path(out_path).write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
print(f"wrote {out_path}: {arch}, {len(after)} syscalls after the filter, probed={probed}")
MERGE_EOF
