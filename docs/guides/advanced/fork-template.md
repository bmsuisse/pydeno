# Faster worker start: fork from a template (opt-in, Linux)

A new `IsolatedRuntime` starts a Python worker process: interpreter, the `asyncio` import chain, the OS
sandbox, the self-test, V8. On a Linux x86_64 box that is about 63 ms before the first call returns, and
about 45 ms of it is Python starting and importing. The fork template removes that part.

```python
import pydeno

pydeno.enable_fork_template()          # or set PYDENO_FORK_TEMPLATE=1 in the environment

with pydeno.IsolatedRuntime(sandbox="require") as rt:
    rt.eval("1 + 1")                   # rt.worker_start == "fork-template"
```

It is **off by default** and **Linux only**. `sandbox_status().worker_start` is the *configured* mode
(`"exec"` or `"fork-template"`), and `sandbox_status()` carries a warning while the template is on. It does
not say what happened to any one worker: if the template cannot start or fork, that worker silently falls
back to a fresh interpreter (the fallback is logged through the `pydeno` logger and counted in a warning of
`sandbox_status()`). **`IsolatedRuntime.worker_start` and `AsyncIsolatedRuntime.worker_start` are the
authority for one worker.**

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
its exit code (the template reaps it and relays the code). The self-test's "parent" probes (can the
worker read the parent's environment, signal it, ...) now target the **template**, which is the worker's
parent, not the host process; the OS sandbox the worker applies is the same, but the self-test no longer
probes the host directly in this mode.

## What it costs you in security

Forked workers are copies of one process, so workers from the same template share:

- the **address-space layout** of everything the template had mapped: the Python image and its libraries
  and **V8's code**. V8 is statically linked into the `_pydeno` extension module, which the template
  imports, so V8's text and static data sit at the same addresses in every worker of a template; only
  V8's *heap* and its random seed are made per worker, after the fork (no isolate exists in the template);
- the **stack canary** and the **pointer guard** (glibc sets both once, at process start), Python's
  **hash seed**, and the layout `id()` exposes.

Not shared: Python reseeds the `random` module in a forked child, so "module-level random state is shared"
is *not* a property of this feature.

An information leak that exposes an address or a canary in one session therefore helps an attacker in the
next session that comes from the same template. For a guest that can execute arbitrary code inside V8 and
has escaped nothing, this changes little; it matters once you assume a guest has a memory-corruption
exploit and the question is how much one stolen secret is worth. That is why this is opt-in.

What rotation does and does not do:

- The template is **replaced** after `max_forks` workers (default 64) or `max_age_seconds` (default 300),
  whichever comes first. A replaced template is retired, not killed: it keeps serving the workers it
  already made and exits when the last one has.
- That bounds **how many workers share one layout**. It does not bound how long they live (a worker
  from a retired template runs until its runtime closes) and it does not separate tenants: any number of
  concurrent sessions can be live on one template, and a long-lived one keeps its layout for as long as
  it runs. If sessions are mutually hostile, a layout shared by *two of them at the same time* is the
  case to think about, and rotation does not prevent it.
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
- A custom `python=` is never forked. If the template cannot start or fork (any `Exception`, a
  descriptor limit included; never `KeyboardInterrupt`), the worker falls back to the ordinary start and the
  fallback is logged and counted.
- **The host keeps the authority to stop its workers.** If the template dies (killed, crashed), the host kills
  the workers it started from it, instead of leaving them to notice their new parent, and reports them as
  killed (the real exit status died with the template). At interpreter exit the template shuts down after
  every other exit hook and kills what is left. A fork request the template does not answer within 30 s
  retires that template; a late answer is matched by sequence number, and the worker it made is killed.
- The template keeps a finished worker's zombie until the host has its exit status (it reports first,
  reaps on the host's ack), so the host cannot signal a recycled pid. One window remains: code that
  signals the process group directly (`os.killpg(proc.pid, ...)` after `proc.poll()` returned `None`)
  races an exit and its ack by microseconds, as it does for a plain `Popen`; `pidfd` is not used.
- After `os.fork()` in the host, the child drops the template it inherited (the control socket is the
  host's) and **keeps the mode on**: it starts a template of its own when it first needs one, so a
  preforking server gets fork-started workers in every child (each child with its own template).
- The template's working directory, umask and resource limits are those of the host **when the template was
  started**, not when a worker starts; a later `os.chdir()` or `os.umask()` in the host does not reach
  workers (workers have an empty environment either way).
- A forked worker that fails with a Python exception before it speaks the protocol writes the traceback to
  its stderr, which the host includes in the crash message like for an exec-started worker. If the
  template itself dies unexpectedly, its stderr tail is logged.
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

## What is not done (issue #72)

- **V8 startup snapshot.** Measured on the same host: `Runtime()` takes 6.8 ms plain and 3.8 ms from a
  snapshot made with `SnapshotBuilder`, so a built-in snapshot is worth about 3 ms. Not implemented: the
  worker always runs a bootstrap (global stripping, optional frozen clock) and `RuntimeConfig` forbids a
  snapshot together with a bootstrap, V8 checks a snapshot against its flag set (the worker's flags differ from a
  builder's), and the template cannot build one because it must stay free of V8. It needs a design of its
  own.
- **A native worker without Python** (the 4 to 6 week item in the issue).
