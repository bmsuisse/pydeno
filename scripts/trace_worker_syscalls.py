#!/usr/bin/env python3
"""Which syscalls does a real `IsolatedRuntime` worker make once its sandbox is up?

This is how the seccomp allow-list in `pydeno/_sandbox.py` (`_ALLOWED`) was derived, and how it
is re-checked when V8, CPython, glibc or the kernel change. It runs inside a Linux container
with `strace` (never on a developer's host: the workload starts many workers):

    scripts/trace_worker_syscalls.py wrap VENV OUT      # make VENV's python trace its workers
    VENV/bin/python -m pytest tests/test_isolated_*.py  # or `VENV/bin/python THIS workload`
    scripts/trace_worker_syscalls.py report OUT         # what the workers did after apply()

`wrap` replaces `VENV/bin/python` with a small wrapper. Ordinary invocations run the real
interpreter (under the same `argv[0]`, so `sys.executable` keeps pointing at the wrapper); a
worker invocation runs under `strace -f`, one log per worker. `report` reads the logs, keeps
only the worker's own threads (not the throwaway child that tests the filter), and only what
happened after the worker's `seccomp(SECCOMP_SET_MODE_FILTER, ...)` succeeded, and prints, per
syscall: how often, and what the kernel answered. A syscall that answers `EPERM` in a healthy
run and is not on a deny list is a candidate for the allow-list (or a probe of `attest()`).

Tracing under emulation says nothing: a `linux/amd64` container on an arm64 host runs the
x86_64 interpreter through a translator that issues the *host's* syscalls. Run it on a native
x86_64 machine and a native aarch64 machine (CI runners are both).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from pathlib import Path

WRAPPER = """#!/bin/bash
# Installed by scripts/trace_worker_syscalls.py: trace pydeno workers, run everything else as is.
case "$*" in
  *pydeno._worker*)
    exec strace -f -qq -s 64 -o "{out}/worker.$$.log" -- "{real}" "$@" ;;
  *)
    exec -a "$0" "{real}" "$@" ;;
esac
"""

LINE = re.compile(r"^(\d+)\s+(.*)$")
CALL = re.compile(r"^([a-z_0-9]+)\((.*)$")
RESUMED = re.compile(r"^<\.\.\. ([a-z_0-9]+) resumed>(.*)$")
RESULT = re.compile(r"\)\s+=\s+(-?\d+|\?|0x[0-9a-f]+)(?:\s+([A-Z][A-Z0-9_]+))?")


def wrap(venv: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    python = venv / "bin" / "python"
    real = venv / "bin" / "python-real"
    if not real.exists():
        target = os.path.realpath(python)
        real.symlink_to(target)
    python.unlink()
    python.write_text(WRAPPER.format(out=out, real=real))
    python.chmod(python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _parse(path: Path) -> dict[str, object]:
    """One worker's log -> {"after": {name: Counter(result)}, "args": {...}, "applied": bool}."""
    main: int | None = None
    threads: set[int] = set()
    applied = False
    after: dict[str, Counter[str]] = defaultdict(Counter)
    args: dict[str, Counter[str]] = defaultdict(Counter)
    pending: dict[int, tuple[str, str]] = {}

    def finish(pid: int, name: str, text: str) -> None:
        nonlocal applied
        res = RESULT.search(text)
        if res is None:
            outcome = "?"
        elif res.group(1).startswith("-") or res.group(1) == "?":
            outcome = res.group(2) or res.group(1)
        else:
            outcome = "ok"
        if pid == main and name == "seccomp" and "SECCOMP_SET_MODE_FILTER" in text:
            if outcome == "ok":
                applied = True
                return
        if name in ("clone", "clone3") and outcome == "ok" and pid in threads:
            if "CLONE_THREAD" in text and res is not None:
                threads.add(int(res.group(1)))
        if applied and pid in threads:
            after[name][outcome] += 1
            if name in (
                "prctl",
                "fcntl",
                "ioctl",
                "mmap",
                "mprotect",
                "madvise",
                "socketpair",
            ):
                head = text.split(",")
                key = {"prctl": 0, "socketpair": 0}.get(
                    name, 1 if name != "mmap" else 2
                )
                if name in ("mmap", "mprotect"):
                    key = 2
                if len(head) > key:
                    args[name][head[key].strip()[:48]] += 1

    with path.open(errors="replace") as fh:
        for raw in fh:
            m = LINE.match(raw.rstrip("\n"))
            if not m:
                continue
            pid, rest = int(m.group(1)), m.group(2)
            if main is None:
                main = pid
                threads.add(pid)
            if rest.startswith(("+++", "---")):
                continue
            r = RESUMED.match(rest)
            if r:
                name, text = (
                    r.group(1),
                    pending.pop(pid, (r.group(1), ""))[1] + r.group(2),
                )
                finish(pid, name, text)
                continue
            c = CALL.match(rest)
            if not c:
                continue
            name, text = c.group(1), c.group(2)
            if text.endswith("<unfinished ...>"):
                pending[pid] = (name, text[: -len("<unfinished ...>")])
                continue
            finish(pid, name, text)
    return {"after": after, "args": args, "applied": applied}


def report(out: Path, as_json: Path | None) -> int:
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    args: dict[str, Counter[str]] = defaultdict(Counter)
    workers = sandboxed = 0
    for log in sorted(out.glob("worker.*.log")):
        workers += 1
        parsed = _parse(log)
        if not parsed["applied"]:
            continue
        sandboxed += 1
        for name, outcomes in parsed["after"].items():  # type: ignore[union-attr]
            totals[name].update(outcomes)
        for name, seen in parsed["args"].items():  # type: ignore[union-attr]
            args[name].update(seen)
    print(f"{workers} worker logs, {sandboxed} with a seccomp filter applied")
    for name in sorted(totals):
        outcomes = ", ".join(f"{k}:{v}" for k, v in totals[name].most_common())
        print(f"  {name:24} {outcomes}")
    for name in sorted(args):
        print(f"  args of {name}: {dict(args[name].most_common(12))}")
    if as_json is not None:
        as_json.write_text(
            json.dumps(
                {
                    "machine": os.uname().machine,
                    "workers": sandboxed,
                    "syscalls": {k: dict(v) for k, v in sorted(totals.items())},
                    "args": {k: dict(v) for k, v in sorted(args.items())},
                },
                indent=1,
            )
        )
    return 0 if sandboxed else 1


def workload() -> None:
    """Every kind of command a worker serves, on both engine modes, in one process."""
    import asyncio
    import time

    from pydeno import IsolatedRuntime, RuntimeConfig

    wasm = bytes.fromhex(
        "0061736d0100000001070160027f7f017f030201000707010361646400000a09010700200020016a0b"
    )
    failures: list[str] = []

    def step(what: str, fn: object) -> None:
        try:
            fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - a trace wants every step, not the first error
            failures.append(f"{what}: {type(exc).__name__}: {exc}")

    async def later(x: int) -> int:
        await asyncio.sleep(0.01)
        return x * 2

    async def loader(specifier: str) -> str:
        return "export const w = 5;"

    for jitless in (True, False):
        # Generous deadlines: strace slows every syscall down.
        with IsolatedRuntime(
            RuntimeConfig(timeout=30.0), jitless=jitless, capture_console=True
        ) as rt:
            rt.bind_function("add", lambda a, b: a + b)
            rt.bind_function("later", later)
            rt.bind_object("cfg", {"name": "x", "f": lambda: 7})
            rt.add_static_module("m", "export const v = 41 + 1;")
            for code in (
                "console.log('hi', {a: 1}); console.error('e'); add(1, 2) + cfg.f()",
                "JSON.stringify(Array.from({length: 1e5}, (_, i) => i)).length",
                "new Uint8Array(1 << 22).fill(1).length",
                "let s = ''; for (let i = 0; i < 2e5; i++) s += i; s.length",
                "new Intl.DateTimeFormat('de-CH').format(new Date(0))",
                "[1,2,3].map(x => x ** 2).toSorted((a, b) => b - a)",
                "new TextEncoder().encode('x'.repeat(1e5)).length",
                "crypto.getRandomValues(new Uint8Array(16)).length",
                "structuredClone({a: [1, 2, new Map([[1, 2]])]})",
                "Math.random() + performance.now() + Date.now()",
                "try { null.x } catch (e) { e.stack.length }",
                "new RegExp('(a+)+b').test('a'.repeat(20))",
                "BigInt(2) ** BigInt(4000) > 0n",
            ):
                step(code[:30], lambda code=code: rt.eval(code))
            step("execute", lambda: rt.execute("throw new Error('boom')"))
            for code in (
                "later(21).then(x => x + 0)",
                "new Promise(r => queueMicrotask(() => r(1)))",
                "(async () => { let t = 0; for (let i = 0; i < 20; i++)"
                " t += await later(i); return t; })()",
                "Promise.all(Array.from({length: 20}, (_, i) => later(i)))",
            ):
                step(code[:30], lambda code=code: asyncio.run(rt.eval_async(code)))
            step("module", lambda: rt.eval_module("m"))
            step("loader", lambda: rt.set_module_loader(loader))
            step("dyn", lambda: asyncio.run(rt.eval_module_async("custom:x")))
            if not jitless:

                def wasm_roundtrip() -> None:
                    module = rt.load_wasm(wasm)
                    module.call("add", 2, 3)
                    module.unload()

                step("wasm", wasm_roundtrip)
            for _ in range(3):  # several GCs, so the GC's helper threads run
                step(
                    "gc",
                    lambda: rt.eval(
                        "for (let i = 0; i < 50; i++) new Array(1e5).fill({});"
                    ),
                )
            time.sleep(0.5)  # idle: background threads
    # a deadline, a CPU cap, a memory kill and the other sandbox options
    with IsolatedRuntime(RuntimeConfig(timeout=1.0)) as rt:
        step("deadline", lambda: rt.eval("while (true) {}"))
        step("after deadline", lambda: rt.eval("1 + 1"))
    with IsolatedRuntime(max_memory=200 << 20) as rt:
        step(
            "memory",
            lambda: rt.eval(
                "const k = []; while (true) k.push(new Array(1e6).fill(1.5));"
            ),
        )
    for kwargs in (
        {"empty_root": False},
        {"strict_eval": True},
        {"clock": 0, "random_seed": 1},
    ):
        with IsolatedRuntime(**kwargs) as rt:  # type: ignore[arg-type]
            step(str(kwargs), lambda: rt.eval("1"))
    print("workload done;", len(failures), "steps failed")
    for line in failures:
        print("  ", line[:200])


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "wrap":
        wrap(Path(sys.argv[2]), Path(sys.argv[3]))
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "report":
        return report(
            Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else None
        )
    if len(sys.argv) == 2 and sys.argv[1] == "workload":
        workload()
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
