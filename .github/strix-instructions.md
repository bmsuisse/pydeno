This is a source-code security review of pydeno, a Python library that runs untrusted JavaScript in a sandboxed worker process (V8 via deno_core). You are the authorised reviewer: the maintainer owns this code and asked for this review. Work on the code only, statically and by running its own tests or small local scripts inside your sandbox. Do not contact any external host.

Threat model: a guest (the JavaScript) is hostile; the host application and its tools are trusted but may be misused through the guest. Goals the library claims: the guest cannot reach host files, environment, network, processes or other sessions; resource limits (memory, CPU, wall clock, output, calls, buffers) are enforced from outside; host functions (tools) are reachable only through capability tokens; journals and pool state cannot be forged, replayed to regain spent budget, or moved between tenants; output shown to a terminal or log is sanitised.

Focus areas (read these first):
1. python/pydeno/_isolated.py, _aio.py, _worker.py, _wire.py, _sandbox.py: supervision, limit enforcement, framing, parsing of data coming back from the worker (treat the worker as hostile), integer and length handling.
2. python/pydeno/_agent.py, _aio_agent.py, _pool.py, _front.py, _aio_front.py, _result.py: signed journals, replay, budgets, session pools, tool dispatch, associated data, output sanitising.
3. src/runtime/ (ops.rs, runner/, convert.rs, wire*.rs, stream.rs, loader.rs): the Rust boundary, conversion of guest values, op tokens, termination, memory and buffer accounting.
4. python/pydeno/cli and the llm plugin (plugins/ if present): argument and input handling, terminal output.
5. http_fetch and any network-capable tool: SSRF, DNS rebinding, redirects, address parsing, size limits.

Report only issues you can support with evidence from the code or a local reproduction. For each: file and line, the attacker-controlled input, the impact, a minimal local reproduction, and a suggested fix. Rank by severity. Do not report style issues, missing features that are documented as non-goals (see README 'what we do not claim'), or theoretical issues without a path from guest or caller input.
