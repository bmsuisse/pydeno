# Source pre-check and sandbox status

## `sandbox_status()`: what can this host apply?

`IsolatedRuntime(sandbox="require")` refuses to start where the OS sandbox is incomplete, but you learn
that on the first request. `pydeno.sandbox_status()` tells a service at start-up, without starting an
isolate or running guest code:

```python
import pydeno

status = pydeno.sandbox_status()
log.info(status.explain())
if not status.complete:          # exactly what sandbox="require" would refuse
    raise SystemExit("this host cannot give untrusted code a complete sandbox")
```

It returns a frozen `SandboxStatus` with one `Layer(applied, detail)` per protection (`seatbelt`,
`landlock`, `seccomp`, `empty_root`, `no_new_privs`, `privileges`, `resource_probes`, `termination`, `self_test`),
the `applied` string the worker would report, `complete`, `warnings` and `to_dict()`.

How it works: it forks a throwaway child that runs the real `harden_process()`, `apply()` and the
worker's startup self-test, so the answer is what a worker would get, while the calling process is
never confined (a sandbox cannot be lifted, so it is only ever applied in a process that then exits).
A second child is hardened like a worker, then the parent reads its memory, CPU time and thread count
and tests SIGKILL permission,
because a limit that cannot be measured never fires. It takes tens of milliseconds, never raises, kills
and reaps anything that overruns a one-second deadline, and starts no isolate.

`complete` is true when every layer `sandbox="require"` demands for this platform applied, the self-test
found nothing the sandbox should have stopped, the resource probes work, termination authority is
available, and (Linux) the process is not
root or can drop root. Missing termination authority refuses startup in every sandbox mode. `empty_root` is a bonus layer: its absence is a warning, not a failure.

On Linux the details say what was checked, not only what was asked for:

- `landlock`: the kernel's Landlock ABI version, and whether the canary (a directory readable a
  moment before the ruleset) was refused afterwards. A kernel that accepts the ruleset and does not
  enforce it shows `applied=False` with the reason, and `complete` is false.
- `seccomp`: that the allow-list filter installed in a throwaway child, and whether a
  never-legitimate call in that child was killed (a worker start only asks the kernel whether the
  kill action is supported, since every kill is audited). One that was not killed is also a
  `self_test` failure (`exec-not-killed`).
- `empty_root`: whether the kernel caps the worker's threads (`RLIMIT_NPROC` inside the worker's own
  user namespace, Linux 5.14+).

## `check_source()`: a readable early rejection, not a security boundary

With `policy=SourcePolicy(...)` it applies the host's own rules instead (forbidden names, `eval`,
`import()`, the `Function` constructor, `WebAssembly`, a size cap), and `static_gate(policy)` puts
those rules in front of a sandbox: see [A gate in front of the sandbox](../guides/gate.md). What
follows is the behaviour without a policy, which is unchanged.

```python
from pydeno._preflight import check_source

result = check_source(code)                   # allow_import=False, allow_dynamic_import=False
if not result.ok:
    print(result.format())                    # "2:3 error [fetch] fetch() does not exist ..."
```

This exists so that an author, or a model, gets a precise line, a column and a sentence ("bind a host
function instead") before any runtime starts, instead of a `ReferenceError` from inside the isolate.
It flags static `import` / `export`, `import(`, `require(`, `fetch(`, `XMLHttpRequest`, `WebSocket`,
`process.`, `Deno.`, `child_process` and `globalThis.<those>` as errors; `__proto__` and
`.constructor.constructor` as warnings; `eval(` and `new Function(` as notes (`report_eval=False` to
drop them). A small tokenizer understands strings, template literals (including `${...}` code),
comments and regular-expression literals, so a word inside one of those is not a finding. It is not a
parser and does not know scopes.

**It is never a security boundary.** Nothing in `Runtime`, `IsolatedRuntime` or `AgentSandbox` consults
it, and the sandbox does not depend on it. A guest can evade every rule (`globalThis['req' + 'uire']`,
a string assembled at run time, code fetched later), and `check_source` does not try to catch that;
`tests/test_preflight.py` asserts that these evasions are *not* detected and, with an
`IsolatedRuntime`, that the names do not exist in the isolate anyway. What stops a guest from reaching
the network, the filesystem or a process is the absence of those capabilities in the isolate and the
OS sandbox around the worker ([Isolated runtime](../guides/advanced/isolation.md)). A clean result
means only that the text has none of the common mistakes.
