# Faster worker start: fork from a template (opt-in, Linux)

A new `IsolatedRuntime` starts a Python worker process: interpreter, the `asyncio` import chain, the OS
sandbox, the self-test, V8. On a Linux x86_64 box that is about 57 ms before the first call returns, and
about 45 ms of it is Python starting and importing. The fork template removes that part.

```python
import pydeno

pydeno.enable_fork_template()          # or set PYDENO_FORK_TEMPLATE=1 in the environment

with pydeno.IsolatedRuntime(sandbox="require") as rt:
    rt.eval("1 + 1")                   # rt.worker_start == "fork-template"
```

It is **off by default** and **Linux only**. `sandbox_status().worker_start` says which mode is in force
(`"exec"` or `"fork-template"`), and `sandbox_status()` carries a warning while the template is on.

## What it does

One *template* process is started per host process (and again after every rotation, below). It
imports everything a worker needs and then waits, single-threaded, with no V8 and no isolate. Each worker is
a `fork()` of it: about 0.7 ms. The forked child then runs the ordinary worker: it makes itself a session
leader, receives its own stdin, stdout and stderr, and goes through the same start-up as an exec-started worker. The OS sandbox
(Landlock, seccomp, the empty root where available), the start-up self-test, `sandbox="require"`, the memory
and thread limits and the orphan watchdog all run **in the child, after the fork, in the same code**. V8, its
flags and its random seed are created in the child, never in the template.

Everything that holds for a normal worker still holds: a worker serves one runtime and is killed when it
closes, the host talks to it over the same framed pipe, and a worker that dies is reported as a crash with
its exit code (the template reaps it and relays the code).

## What it costs you in security

Forked workers are copies of one process, so workers from the same template share:

- the **address-space layout** (no per-worker ASLR for the Python image and its libraries) and the **stack
  canary**;
- Python's **hash seed** and the **module-level random state** at the moment of the fork.

An information leak that exposes an address or a canary in one session therefore helps an attacker in the
next session that comes from the same template. For a guest that can execute arbitrary code inside V8 and
has escaped nothing, this changes little; it matters once you assume a guest has a memory-corruption
exploit and the question is how much one stolen secret is worth. That is why this is opt-in.

Mitigations in the code:

- The template is **replaced** after `max_forks` workers (default 64) or `max_age_seconds` (default 300),
  whichever comes first. A replaced template is retired, not killed: it keeps serving the workers it
  already made and exits when the last one has.
- V8 and its seed exist only in the child, so the isolate's own randomisation is per worker.
- If you cannot accept the trade, do not turn it on: nothing else changes.

```python
pydeno.enable_fork_template(max_forks=16, max_age_seconds=60)   # rotate more often
pydeno.disable_fork_template()                                   # back to one interpreter per worker
```

## Behaviour to know about

- The first worker after `enable_fork_template()` waits for the template's imports (about the same as
  one exec-started worker); the template is launched at enable time so that overlaps with your own start-up.
- Process tree: host, template, workers. The host does not wait for workers itself; the template does
  and reports exit codes. Tools that count the host's child processes will see one extra child (the template).
- If the host dies, the template exits at once, which changes every worker's parent, which the worker's own
  watchdog treats as "the parent is gone" (the same mechanism as before).
- A custom `python=` is never forked. If the template cannot start or fork, the worker falls back to the
  ordinary start.
- After `os.fork()` in the host, the child forgets the template and starts nothing from it.
- `SandboxPool`, `AsyncIsolatedRuntime`, `SessionPool` and the front door all pick the mode up, since they all
  start workers through the same function.

## Measured and not measured

Numbers are from this repository's `scripts/autoresearch/metric_speed.py cold` and a paired A/B script, on
one Linux x86_64 host, release build, Python 3.14 free-threaded. They are comparable only to each other. See the
changelog entry for the figures. Not measured: aarch64, other kernels, memory use of N live workers
(forked workers share pages copy-on-write, which should lower it; not measured here). The `empty_root`
layer could not be exercised on the test host (unprivileged user namespaces are blocked there by AppArmor),
so the claim "unshare works in the child because the template is single-threaded" rests on
`tests/test_fork_template.py::test_template_is_single_threaded_and_holds_no_isolate` and on reasoning,
not on a run of that layer. This feature needs native x86_64 and aarch64 verification and independent review
before it is called supported.
