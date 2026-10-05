#!/usr/bin/env python3
"""Assume-breach syscall sweep: what can code *inside the sandboxed worker* still reach?

The threat model is a V8 escape: the attacker has arbitrary native code in the worker, after
`pydeno._sandbox.apply()` has run. They can issue any syscall with any arguments. So do
exactly that, for every syscall number, from a fresh sandboxed process each time, and
record what the kernel says.

The seccomp filter decides at syscall *entry*, before the kernel looks at the arguments, so a
syscall our filter denies answers `EPERM` whatever we pass (or, for the never-legitimate ones,
the process dies of SIGSYS before anything happens). A reachable one answers
`EFAULT`/`EINVAL`/`EBADF`... or simply succeeds. That makes `EPERM` with garbage arguments a
precise "blocked by the filter" signal, and everything else a precise inventory of what the
attacker still has.

SAFETY: this fires every syscall in the kernel's table, including ones that reboot, unmount or
signal. It refuses to run outside a container. Run it only like this (see
`scripts/linux_matrix.sh` for the wider matrix):

    podman run --rm --network none --cap-drop all --pids-limit 512 --memory 2g \\
        --read-only --tmpfs /tmp -e PYDENO_REDTEAM_CONTAINER=1 \\
        -v "$PWD":/src:ro python:3.13-slim python /src/scripts/redteam_syscalls.py
"""

from __future__ import annotations

import errno as errno_mod
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX_PY = HERE.parent / "python" / "pydeno" / "_sandbox.py"
TABLES = HERE.parent / "tests" / "data" / "syscalls.json"

# What the child does. It applies the real sandbox, then fires one syscall with junk
# arguments and reports. No Python-level helper may touch the filesystem after apply().
CHILD = r"""
import ctypes, importlib.util, json, os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # a SIGSYS kill must not leave a core file
spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
nr = int(sys.argv[2])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
sink = ctypes.create_string_buffer(4096)       # a valid, writable pointer for arguments
layers = sb.apply() if sys.argv[3] == "sandbox" else "none"   # "raw" skips the sandbox
p = ctypes.addressof(sink)
args = [int(a) if a != "PTR" else p for a in sys.argv[4].split(",")]
ctypes.set_errno(0)
ret = libc.syscall(nr, *[ctypes.c_long(a) for a in args])
sys.stdout.write(json.dumps({"layers": layers, "ret": ret, "errno": ctypes.get_errno()}) + "\n")
sys.stdout.flush()
os._exit(0)
"""

ARG_PATTERNS = [
    "0,0,0,0,0,0",
    "PTR,PTR,PTR,PTR,PTR,PTR",
    "-100,PTR,0,0,0,0",
    "1,1,1,1,1,1",
]

# Calls the sweep must not fire even in a container: they would end the sweep itself.
SKIP = {
    "exit",
    "exit_group",
    "pause",
    "sigsuspend",
    "rt_sigsuspend",
    "vhangup",
    "reboot",
}


REFUSED = {"EPERM", "signal 31"}


def load_sandbox():
    spec = importlib.util.spec_from_file_location("_sandbox", SANDBOX_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fire(nr: int, pattern: str, mode: str) -> dict:
    try:
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                CHILD,
                str(SANDBOX_PY),
                str(nr),
                mode,
                pattern,
            ],
            capture_output=True,
            text=True,
            timeout=6,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "hang"}
    if done.returncode < 0:
        return {"outcome": f"signal {-done.returncode}"}
    try:
        data = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"outcome": f"exit {done.returncode}", "stderr": done.stderr[-120:]}
    err = data["errno"] if data["ret"] == -1 else 0
    return {
        "outcome": "ok"
        if data["ret"] != -1
        else errno_mod.errorcode.get(err, str(err)),
        "ret": data["ret"],
        "layers": data["layers"],
    }


def sweep(arch: str, numbers: Iterable[int], mode: str = "sandbox") -> dict[int, dict]:
    names = json.loads(TABLES.read_text())[arch]
    results: dict[int, dict] = {}

    def one(nr: int) -> tuple[int, dict]:
        name = names.get(str(nr), f"unknown_{nr}")
        if name in SKIP:
            return nr, {"name": name, "blocked": None, "outcomes": ["skipped"]}
        outcomes = [fire(nr, pat, mode)["outcome"] for pat in ARG_PATTERNS]
        raw = [fire(nr, pat, "raw")["outcome"] for pat in ARG_PATTERNS]
        # Refused by the filter: EPERM, or for a never-legitimate call the process killed by
        # SIGSYS (31 on x86_64 and aarch64), to every argument pattern.
        sandbox_refused = all(o in REFUSED for o in outcomes)
        return nr, {
            "name": name,
            # Blocked by *our filter*: refused to every argument pattern with the sandbox, and
            # not EPERM without it. If the kernel says EPERM anyway (no capability) the
            # protection is the worker's lack of privilege, not the filter: "cap_dependent".
            "blocked": sandbox_refused and any(o != "EPERM" for o in raw),
            "cap_dependent": sandbox_refused and all(o == "EPERM" for o in raw),
            "killed": all(o == "signal 31" for o in outcomes),
            "outcomes": outcomes,
            "raw": raw,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        for nr, info in pool.map(one, numbers):
            results[nr] = info
    return results


def main() -> int:
    if os.environ.get("PYDENO_REDTEAM_CONTAINER") != "1":
        print(
            "refusing to run: this fires every syscall in the kernel table. Run it inside a "
            "container and set PYDENO_REDTEAM_CONTAINER=1 (see the docstring).",
            file=sys.stderr,
        )
        return 2
    arch = platform.machine()
    arch = "aarch64" if arch == "arm64" else arch
    names = json.loads(TABLES.read_text())[arch]
    numbers = sorted(int(n) for n in names)
    results = sweep(arch, numbers)
    out = Path(tempfile.gettempdir()) / "redteam.json"
    out.write_text(json.dumps({str(k): v for k, v in results.items()}, indent=1))

    skipped = [r for r in results.values() if r["blocked"] is None]
    sb = load_sandbox()
    idx = 0 if arch == "x86_64" else 1
    # The filter is an allow-list: it refuses every name it does not allow outright.
    allowed = {n for n, p in sb._ALLOWED.items() if p[idx] is not None}  # noqa: SLF001
    in_filter = set(names.values()) - allowed
    killed = sorted(r["name"] for r in results.values() if r.get("killed"))
    # EPERM both with and without the sandbox: whoever is denying it, if it is a name our
    # filter lists then the filter is doing the job too; only the rest depend on capabilities.
    cap_dep = [
        r
        for r in results.values()
        if r.get("cap_dependent") and r["name"] not in in_filter
    ]
    reachable = [
        r
        for r in results.values()
        if r["blocked"] is False and not r.get("cap_dependent")
    ]
    filter_blocked = [
        r
        for r in results.values()
        if r["blocked"] or (r.get("cap_dependent") and r["name"] in in_filter)
    ]
    print(
        f"{arch}: {len(results)} syscalls; {len(filter_blocked)} denied by the filter, "
        f"{len(cap_dep)} denied only for lack of capability, {len(reachable)} reachable, "
        f"{len(skipped)} skipped; {len(killed)} of the denied ones kill the process"
    )
    print("\nKILLED (never legitimate):", " ".join(killed))
    print("\nDENIED ONLY FOR LACK OF CAPABILITY (the filter does not stop these):")
    for r in sorted(cap_dep, key=lambda r: r["name"]):
        print(f"  {r['name']}")
    print("\nREACHABLE (the sandboxed process gets something other than a refusal):")
    for r in sorted(reachable, key=lambda r: r["name"]):
        print(f"  {r['name']:28} {' '.join(r['outcomes'])}")
    print(f"\nfull report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
