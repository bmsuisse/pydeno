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

Input is read up to 16 MiB, the most the worker accepts in one message; larger input, or input that
is not UTF-8, is a usage error (exit code 2). The read is bounded, so `pydeno -f /dev/zero` or an
endless pipe stops at the cap instead of filling memory.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--timeout SECONDS` | 30 | Deadline for the evaluation; above 0 and at most 86400 |
| `--max-memory SIZE` | 1G | Kill the worker above this resident memory: bytes, or a `K`/`M`/`G`/`T` suffix, optionally with `B` or `iB` (binary units); at most `1T` |
| `--sandbox require\|auto` | `require` | `require` refuses to run without every OS sandbox layer this platform has; `auto` applies what is available |
| `--no-sandbox` | off | Run the worker without the OS sandbox; prints a warning on stderr. Still a separate process with the same limits |
| `--json` | on | Print the result as JSON (non-ASCII text is kept as is) |
| `--raw` | off | Print a string result as plain text; any other result is still JSON |

Results follow [`ExecutionResult`](agent-sessions.md)'s JSON rules: bytes become base64 text, dates
ISO 8601 text, sets lists, and `NaN`/`Infinity` become `null`. Integers beyond 2^53 - 1 (a
`BigInt`, or a `Number` that has already lost precision) print as JSON strings, since most JSON
readers cannot hold them exactly: `pydeno '2n ** 64n'` prints `"18446744073709551616"`; a small
`BigInt` such as `2n` prints as `2`.

A result that has no JSON form (a circular structure, a `Map`, `Symbol`, `Error` or function, an
invalid `Date`) is exit code 6; the code itself ran. Convert it in the code first, for example
`Object.fromEntries(map)` or `{name: e.name, message: e.message}`.

### Terminal safety

Output is guest-controlled, so nothing the guest writes reaches the terminal as a control sequence.
In console output, a `--raw` string and error messages, control characters (except tab and
newline), escape sequences, C1 controls and invisible or bidirectional formatting characters
(zero-width characters, direction overrides and isolates, the BOM, tag characters) are replaced with
`?`. JSON output escapes the same characters as `\uXXXX`, so it stays lossless: parsing it gives the
original string.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The JavaScript threw or did not compile |
| 2 | Usage error (bad option, no code, unreadable, oversized or non-UTF-8 input) |
| 3 | Timeout (the deadline or the worker's CPU cap) |
| 4 | The OS sandbox is not available here (`--sandbox require` refused to start) |
| 5 | Any other runtime failure (memory limit, worker crash, ...) |
| 6 | The result cannot be converted to JSON (see above) |

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
