<div align="center">

# pydeno

**A sandbox for AI-generated JavaScript, callable from Python**

`pydeno` = **py**thon + **deno**: run the JavaScript a model wrote, from your Python code, without
trusting it. Real V8 (via [deno_core][deno_core]), inside a supervised, OS-sandboxed worker process,
with your own Python functions exposed as the only things it can call.

<br />

[![Publish](https://github.com/bmsuisse/pydeno/actions/workflows/workflow.yaml/badge.svg)][workflows-ci]
[![PyPI](https://img.shields.io/pypi/v/pydeno.svg)][pydeno-pypi]
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-bmsuisse.github.io%2Fpydeno-blue)][pydeno-docs]

<p align="center">
  <a href="https://bmsuisse.github.io/pydeno/"><strong>Documentation</strong></a>
  ·
  <a href="SECURITY.md"><strong>Security</strong></a>
  ·
  <a href="https://github.com/bmsuisse/pydeno/tree/main/examples"><strong>Examples</strong></a>
  ·
  <a href="https://github.com/bmsuisse/pydeno/issues"><strong>Issues</strong></a>
</p>

</div>

## What it is

Agents increasingly write code to answer a question: a data transform, a chart spec, a slide deck, a
"call these tools in a loop" script. That code is untrusted by construction. `pydeno` lets a Python
program run it and get the result back, with a boundary that holds even if the code is hostile:

```python
from pydeno import IsolatedRuntime, RuntimeConfig

with IsolatedRuntime(RuntimeConfig(timeout=2.0), sandbox="require") as rt:
    rt.bind_function("lookup", lambda sku: {"A1": 3.5}[sku])   # the only thing the guest can call
    print(rt.eval("lookup('A1') * 2"))                          # 7.0
    rt.eval("new Array(2 ** 32 - 1).fill(0)")                   # WorkerCrashed; *your* process is fine
```

- **No ambient authority.** The guest has no filesystem, network, process, environment, `Deno`,
  `process` or `require`. Anything it can do beyond computing, you bound on purpose.
- **A crash is contained.** Hostile code that aborts V8, spins forever or eats memory kills a
  disposable worker process, not yours.
- **Real JavaScript.** Same engine as Chrome and Node, so model-written code behaves like JavaScript,
  and real libraries run (pptxgenjs, three.js, Vega-Lite and others; see below).

Requires Python 3.10+ on macOS or Linux. `pydeno` is experimental: expect breaking changes between
versions.

```bash
pip install pydeno  # or: uv pip install pydeno
```

## How it is secure

Defence in depth, each layer assuming the one above it has failed. Details and the threat model are
in [`SECURITY.md`](SECURITY.md) and [the isolation guide](docs/guides/advanced/isolation.md).

| Layer | What it does |
|---|---|
| **Separate process** | The guest runs in a worker process with an empty environment, started in its own session. A V8 abort, hang or out-of-memory kills the worker; the parent reports `WorkerCrashed` or `RuntimeTimeout`. |
| **OS sandbox, applied before the JavaScript engine exists** | macOS: Seatbelt. Linux: Landlock, a private empty root (mount, network, IPC and UTS namespaces), and a seccomp-bpf filter that denies new processes, network, mounts, kernel interfaces, other processes' signals, file-metadata and filesystem changes. Anything newer than the reviewed syscall range answers `ENOSYS`. `sandbox="require"` refuses to start unless **every** layer applied. |
| **No privileges** | A worker started as root drops to `nobody` with an empty capability set. `no_new_privs` is set. |
| **Smaller engine surface** | V8 runs `--jitless` (no JIT, no WebAssembly) by default. `SharedArrayBuffer`, `Atomics`, `WeakRef` and `FinalizationRegistry` are removed (they are what timers and GC probes are built from). |
| **Hard limits, enforced from outside** | Wall-clock deadline (default 60 s), CPU cap, memory ceiling (default 1 GiB, enforced by the worker and the parent), host-call budgets, an in-flight call cap and a stall limit on writes. A guest that keeps calling your functions cannot hold the deadline open indefinitely (host-callback time is capped, and CPU time is counted separately). |
| **The worker is treated as untrusted** | Every frame it sends is parsed by a strict native decoder with size, depth, node and hash-collision budgets. No pickle, no `eval`. A worker that sends nonsense is killed. |
| **Capability tokens** | A bound function is reachable only through an unguessable token, installed after the binding exists. `ToolBridge` adds a call budget and fail-closed name checking. |
| **Deterministic when you ask** | `clock=` freezes `Date`/`Intl`; `random_seed=` seeds `Math.random`. |

**What it does not promise.** A V8 bug is *contained* to the worker, not prevented, and about 200
syscalls the engine needs stay reachable. For multi-tenant use, run one runtime per trust unit and
put the whole thing in a locked-down container or microVM ([deployment guidance](SECURITY.md)).
Libraries that need WebAssembly, a DOM or a canvas do not work. The engine is only as current as the
`deno_core` release it is built on; a weekly check tells maintainers when a newer V8 is available.

## How it was tested

The sandbox is tested the way an attacker would try it: from inside, and against the real kernel.

- **Assume-breach syscall sweeps.** A sandboxed process fires *every* syscall in the kernel's table
  with garbage arguments, from a fresh process each time, and records what is still reachable.
  Dangerous calls are fired for real (inside a container, never on a host).
- **The seccomp program is verified, not trusted.** A BPF interpreter in the tests runs the filter's
  decision logic on any platform, and every syscall number in it is checked against the Linux
  kernel's own tables (regenerated from a pinned kernel tag by `scripts/gen_syscall_tables.py`).
- **A capability checklist.** What untrusted code tries first (read or write a file, reach the
  network, spawn a process, read the environment, Deno / Node / browser globals, the runtime's own
  plumbing), through both `eval` and `eval_async`, plus a check that nothing reached the host.
- **Hostile peers.** Fake workers that stop reading, send malformed or oversized frames, flood hash
  tables, or lie about types; fuzzing with Hypothesis; and a port of [pydantic/monty][monty]'s
  security suite (the cases an in-process runtime cannot survive are pinned as strict expected
  failures).
- **Two codecs, tested against each other.** The wire codec is native (Rust) with a readable Python
  reference; hundreds of tests, including generated frames, require identical results and errors.
- **Real libraries, inside the sandbox.** pptxgenjs, three.js (GLB export), Vega-Lite, dagre run with
  the same results as an unsandboxed runtime, with their bytes pinned in `vendor/libs/`; about 25
  more (d3, ECharts, jsPDF, Tailwind v4, Prettier, ...) were checked by hand.
- **Many kernels.** The Linux suite runs on 12 distro images (Debian, Ubuntu, Fedora, AlmaLinux,
  Amazon Linux, Python 3.10-3.14) on both x86_64 and aarch64, and under simulated kernels that lack
  Landlock, seccomp or both, to prove the sandbox degrades honestly and `sandbox="require"` fails
  closed. See [`.github/workflows/test.yml`](.github/workflows/test.yml).
- **Zero skips.** Platform-specific tests are deselected, never skipped; CI enforces a skip budget of
  zero so an unrun test cannot hide.

## Using it

### Tools the guest can call

```python
from pydeno import IsolatedRuntime, ToolBridge

bridge = ToolBridge({"get_weather": get_weather, "send_email": send_email}, max_calls=50)

with IsolatedRuntime(sandbox="require") as rt:
    bridge.attach(rt)
    rt.eval("tools.get_weather('Zurich')")
```

The call that exceeds `max_calls` never reaches your Python function; the guest gets a catchable
`ToolBudgetError`. A Python exception raised inside a tool reaches JavaScript as a real `Error`
whose `name` is the exception's class, so the model's code can branch on it. Use
`redact_host_errors=True` if exception text must not reach the guest.

### Running real libraries

Many libraries assume browser basics a bare isolate lacks. Opt in to a small, pure-JavaScript set
(virtual-time timers, `TextEncoder`, `btoa`, `Blob`, `EventTarget`, `AbortController`,
`structuredClone`) that never defines `window` or `document`:

```python
from pydeno import IsolatedRuntime, RuntimeConfig, WEB_POLYFILLS

rt = IsolatedRuntime(RuntimeConfig(bootstrap=WEB_POLYFILLS))
```

### Make the easy path the safe one

```python
import pydeno

pydeno.configure_default_runtime(isolated=True, sandbox="require")
pydeno.eval("1 + 1")   # now runs in a sandboxed worker
```

### Speed

An `IsolatedRuntime` keeps one spare worker ready, so creating a runtime and evaluating takes about
15 ms with it (about 100 ms without), measured on macOS; structured results move across the boundary
through a native codec. Jitless V8 makes compute-heavy code roughly 1.5-2x slower than with the JIT
(`jitless=False` trades that back for a larger attack surface).

### When the code is yours: the in-process `Runtime`

`Runtime` runs V8 inside your process: the fastest path (~13 µs for a complete host tool call, see
[`BENCHMARKS.md`](BENCHMARKS.md)), with heap and wall-clock limits and cross-thread termination, but
a hostile builtin (`new Array(2 ** 32 - 1).fill(0)`) can abort your process and some builtins ignore
`timeout=`. Use it for code you wrote or reviewed, and `IsolatedRuntime` for anything a model or a
user supplied.

```python
from pydeno import Runtime, RuntimeConfig

with Runtime(RuntimeConfig(max_heap_size=10 * 1024 * 1024)) as runtime:
    runtime.bind_function("getWeather", lambda city: f"72F and sunny in {city}")
    print(runtime.eval("getWeather('Zurich')"))
```

## Integrations

- [**FastMCP tool bridge**](examples/fastmcp_tool_bridge.py) - expose FastMCP tools to sandboxed JS via `bind_function` and an in-process `fastmcp.Client`
- [**pydantic-ai "code mode" agent**](examples/pydantic_ai_agent.py) - an agent tool where the model submits one JS batch script instead of many separate tool calls, run safely with a timeout
- [**ToolBridge**](examples/tool_bridge.py) - hand a sandbox several Python tools with a total call budget, typed errors the model's JS can branch on, and `console.log` routed back to Python
- [**Vendored npm libraries**](examples/vendored_npm_libraries.py) - run real npm document-generation libraries (`pptxgenjs`, `pdf-lib`) from their browser bundles inside the sandbox; see the [guide](https://bmsuisse.github.io/pydeno/guides/advanced/vendored-npm-libraries/) for what makes a library a good candidate
- [**Arrow IPC dataframes**](examples/arrow_ipc_dataframes.py) - move 100k+ row tables into the sandbox as Arrow IPC `bytes` instead of JSON objects (5 ms vs 253 ms); see the [guide](https://bmsuisse.github.io/pydeno/guides/advanced/arrow-ipc-dataframes/)

## Documentation

- [Security policy and threat model](SECURITY.md)
- [The isolation guide](docs/guides/advanced/isolation.md): layers, failure semantics, limits, libraries
- [Quick Start](https://bmsuisse.github.io/pydeno/quickstart/)
- [Concepts](https://bmsuisse.github.io/pydeno/concepts/runtime/): runtimes, type conversion, resource controls
- [Guides](https://bmsuisse.github.io/pydeno/guides/bindings/): binding functions, module loading, snapshots
- [Use cases](https://bmsuisse.github.io/pydeno/use-cases/ai-agent/): AI agent sandboxes, workflow runners, plugin systems
- [API reference](https://bmsuisse.github.io/pydeno/api/pydeno/)
- [Agent skill](skills/pydeno/SKILL.md): an [Agent Skills](https://agentskills.io) `SKILL.md` that teaches coding agents (Claude Code and others) to use pydeno correctly. Copy `skills/pydeno/` into your agent's skills directory, e.g. `.claude/skills/`
- [Benchmarks](BENCHMARKS.md): measured, reproducible numbers

[v8]: https://v8.dev
[deno_core]: https://crates.io/crates/deno_core
[monty]: https://github.com/pydantic/monty
[pydeno-pypi]: https://pypi.org/project/pydeno/
[pydeno-docs]: https://bmsuisse.github.io/pydeno/
[workflows-ci]: https://github.com/bmsuisse/pydeno/actions/workflows/workflow.yaml
