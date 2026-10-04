# Command line

`pydeno` evaluates JavaScript and prints the result as JSON:

```bash
$ pydeno '[1, 2, 3].map(x => x * 2)'
[2, 4, 6]
$ python -m pydeno '({ answer: 42 })'
{"answer": 42}
$ uvx pydeno 'new Date(0).toISOString()'
"1970-01-01T00:00:00.000Z"
```

`pydeno` and `python -m pydeno` are the same command (`uvx pydeno` needs a release that
includes it; 0.7.0 does not). Code runs in an
[`IsolatedRuntime`](advanced/isolation.md) with `sandbox="require"`: a separate worker process under
the OS sandbox (Seatbelt on macOS, Landlock and seccomp on Linux), with V8 in `--jitless` mode, a memory
cap and a deadline. The in-process `Runtime` is never used, so a one-liner is the safe way to run code
you did not write.

## Where the code comes from

| Form | Example |
|---|---|
| Argument | `pydeno '1 + 2'` |
| `-c` | `pydeno -c 'const n = 6; n * 7'` |
| A file | `pydeno -f script.js` |
| stdin | `echo '1 + 2' \| pydeno`, or `pydeno -` |

The code is evaluated as a script: the value of its last expression is the result. A promise is
awaited. `console.log`, `info` and `debug` print to stdout, `console.warn`, `error` and `trace` to
stderr, as they happen; the result is printed last. A result of `undefined` prints nothing.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--timeout SECONDS` | 30 | Deadline for the evaluation |
| `--max-memory SIZE` | 1G | Kill the worker above this resident memory (`256M`, `1G`, plain bytes) |
| `--sandbox require\|auto` | `require` | `require` refuses to run without every OS sandbox layer this platform has; `auto` applies what is available |
| `--no-sandbox` | off | Run the worker without the OS sandbox; prints a warning on stderr. Still a separate process with the same limits |
| `--json` | on | Print the result as JSON (non-ASCII text is kept as is) |
| `--raw` | off | Print a string result as plain text; any other result is still JSON |

Results follow [`ExecutionResult`](agent-sessions.md)'s JSON rules: bytes become base64 text, dates
ISO 8601 text, sets lists, and `NaN`/`Infinity` become `null`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The JavaScript threw or did not compile |
| 2 | Usage error (bad option, no code, unreadable file) |
| 3 | Timeout (the deadline or the worker's CPU cap) |
| 4 | The OS sandbox is not available here (`--sandbox require` refused to start) |
| 5 | Any other runtime failure (memory limit, worker crash, ...) |

Errors go to stderr as `pydeno: <kind>: <message>`, where `<kind>` is the
[error kind](../reference/error-kinds.md) from `classify_error`:

```bash
$ pydeno 'null.x'
pydeno: js_error: Evaluation failed: TypeError: Cannot read properties of null (reading 'x')
$ echo $?
1
```

For exit code 4, `python -c 'import pydeno; print(pydeno.sandbox_status())'` shows which layer is
missing ([sandbox status](../reference/preflight-and-status.md)).

## In an LLM tool

The [`llm`](https://llm.datasette.io/) plugin in
[`integrations/llm-pydeno`](https://github.com/bmsuisse/pydeno/tree/main/integrations/llm-pydeno)
gives a model a sandboxed JavaScript session whose state is kept between calls.
