# Alternatives: why pydeno, and when not

You want to run JavaScript that a model (or a user) wrote, from Python. This page says what the options are,
what each one gives up, and when you should pick something other than pydeno. The numbers on it were
measured, with the script in the repository; the security statements come from reading code and running probes.
Where we have not measured or checked something, it says so.

## The short version

pydeno embeds V8, the JavaScript engine, in a Rust extension and runs untrusted code in a separate,
OS-sandboxed worker process. That buys **full modern JavaScript** (Vega, ECharts, three.js, d3 run
unmodified) with **hard limits** and **several containment layers**. It costs **startup time** and a
**large trusted code base** (V8). If you only need a small amount of logic from a model, a small interpreter
is the better trade.

## Comparison

| | pydeno | Monty | denobox | Deno or Node subprocess (DIY) | Docker / hosted sandbox |
|---|---|---|---|---|---|
| Language | Full JavaScript | A subset of Python | Full JavaScript | Full JavaScript | Anything |
| Runs in your process | Worker process, one per sandbox | Yes (an interpreter) | Subprocess | Subprocess | Container or remote |
| Security model | Process boundary + Landlock/seccomp (Linux) or Seatbelt (macOS) + jitless V8 + limits | Small interpreter, no I/O except through functions you provide | Deno's permission system only | Whatever you build | Container or VM isolation |
| Limits enforced for you | Memory, CPU, wall clock, threads, message size | Yes (see its docs) | None | Whatever you build | Container limits |
| State across calls, pausing at tool calls, signed resumable sessions | Yes | Yes | Variables persist; no sessions | DIY | DIY |
| Async API | Yes | Yes | Yes | DIY | Usually |
| Needs a runtime installed | No (V8 is in the wheel) | No | `deno` (installed as a package) | Yes | Docker or an account |

## Measured

One machine (macOS 26, arm64, Python 3.14), median of 15 runs, 95th percentile in brackets, milliseconds.
pydeno 0.7.0 with the OS sandbox required, Monty 1.0.0 (pool checkout), denobox 0.1a2 on Deno 2.9.7.
Reproduce with `python benches_py/alternatives_bench.py pydeno|pydeno-pool|monty|denobox`.

| | New sandbox + first call | Warm call (`1 + 1`) | Fresh sandbox + 10 small commands |
|---|---:|---:|---:|
| pydeno | 59 (212) | 0.10 (0.15) | 58 (63) |
| pydeno 0.8.0: cold start | 53 (56) | 0.09 (0.13) | 54 (56) |
| pydeno 0.8.0: `SandboxPool` checkout | 0.43 (1.74) | 0.09 (0.14) | 6.9 (8.3) |
| denobox | 12.7 (15.3) | 0.03 (0.07) | 13.6 (14.2) |
| Monty | 0.04 (23.9) | 0.01 (0.02) | 0.18 (0.81) |

What that says, plainly:

- **pydeno is the slowest to start**, by a wide margin against Monty (an interpreter in your process) and
  by about 4.5 times against denobox. The 59 ms is a Python worker process starting, applying the OS sandbox and
  running its self-test. If you create a sandbox per request, that matters; keep one per session (or use
  `SessionPool`) and it does not. If you need a fresh sandbox per request, `SandboxPool` (not yet released)
  keeps started, single-use workers ready: the checkout alone takes 0.04 ms, a burst larger than the pool
  falls back to cold starts. See [A pool of ready workers](guides/advanced/isolation.md#a-pool-of-ready-workers-sandboxpool).
- **Per call, all three are far below anything a model call costs.** The warm numbers are tens to hundreds of
  microseconds.
- This measures the machinery with a trivial expression. It says nothing about how fast each engine runs real
  code; Monty and pydeno's JIT-less V8 are different engines doing different jobs.

## Security: what we actually checked

We ran the same probes against denobox and against pydeno's `IsolatedRuntime` with the sandbox required:

| Guest does | denobox | pydeno |
|---|---|---|
| a plain `console.log('hello')` | The protocol desynchronises: a JSON error, then later replies are off by one | Fine. Guest output goes through a host function, not the protocol channel |
| writes a fake reply to stdout | The host receives the forged value | `Deno is not defined`: the guest has no `Deno`, `process` or `require` |
| overwrites `JSON.stringify` | The next call returns the attacker's value | Unaffected: serialisation is native code the guest cannot reach |
| `for (;;) {}` | Hangs; the API has no timeout | `RuntimeTimeout` at the deadline |
| allocates until memory is gone | Not run (it would have hurt the test machine); no limit exists in the code | Worker killed over `max_memory` within a second |

Denobox's own README says the sandbox is only as secure as Deno, and Deno's permission system is mature and
widely reviewed. These findings are about denobox's design (one shared global scope, shared stdout, no
limits), not about Deno. Treat denobox as a convenient way to run code you mostly trust.

### What this does not show

Passing those probes is not a proof of safety. The honest limits of pydeno's security:

- V8 is millions of lines of C++. We contain it, we do not shrink it. A V8 escape lands in the OS sandbox, which
  is the point of having one, but you should not read "contained" as "cannot happen".
- No one outside the project has reviewed it yet. An independent review is planned before 1.0 (see the
  [roadmap](roadmap.md)). Our own review rounds found real defects every time, including after release.
- The in-process `Runtime` offers **no** protection against hostile code. Use `IsolatedRuntime`.
- The OS layers differ by platform; Windows has no isolated worker at all. `sandbox_status()` tells you what is
  actually in force on a host, and `sandbox="require"` refuses to start without it.

## Why not...

**...Monty (or another small interpreter)?** Often you should. A small interpreter you can read end to end has a
far smaller attack surface, starts in microseconds and needs no process. Monty runs a subset of Python, so choose
it when the model's code is mostly logic and data shaping. Choose pydeno when you need JavaScript: a charting or
3D library, the npm ecosystem, or code a model writes better in JavaScript. The two combine well, and the repository
has examples that run Monty and pydeno together.

**...a Deno or Node subprocess?** It is the obvious DIY route, and denobox is a tidy version of it. Deno's
permission flags are good at stopping file and network access. What you then still have to build is the rest: a
protocol channel the guest cannot write to, timeouts, memory and CPU limits, crash handling, and containment if the
engine itself is escaped. That is most of pydeno.

**...QuickJS (or another small engine), compiled to WebAssembly?** A genuine alternative, and arguably a stronger
isolation story: a small engine inside a WebAssembly runtime has a much smaller trusted base. We have not built or
measured it. The usual costs are speed (no JIT) and the ecosystem: large browser-oriented libraries are less
likely to run unmodified.

**...isolated-vm or a bare V8 isolate?** The same engine risk as pydeno without the process and OS layers around it.

**...Docker or a hosted sandbox?** Strong isolation and any language, at the cost of start time, an extra service
and, for hosted ones, a network dependency. If your threat model needs a VM boundary, use one; pydeno can run inside it.

**...just `eval` it?** Not for code you do not trust.

## What pydeno cannot do

- Start a *cold* sandbox as fast as an in-process interpreter (a pool hides it, at the cost of idle processes).
- Offer a small, auditable trusted code base.
- Be isolated on Windows (the in-process runtime only, which does not contain hostile code).
- Claim an independent security review (not yet).
- Make a model's JavaScript faster: V8 runs jitless by default for safety, which is slower than a JIT. You can
  turn the JIT on per runtime, knowing it widens the attack surface.
