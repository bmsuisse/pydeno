# Hack pydeno

Think you can break out of a JavaScript sandbox? Prove it. Inspired by Pydantic's
[Hack Monty](https://pydantic.dev/articles/hack-monty), this is a friendly, no-prize,
for-the-glory challenge against pydeno's `IsolatedRuntime`.

## The target

A server runs the JavaScript you send it inside a fresh
[`IsolatedRuntime`](guides/advanced/isolation.md) (`sandbox="require"`, jitless V8, small memory
ceiling, a few seconds of wall clock, a CPU cap). A new worker process is started for every
request, so nothing carries over between requests.

On the host there is a secret, in two places:

- the `SECRET` environment variable of the server process, and
- the file `secret.txt` next to the server.

The host exposes exactly one function to your code: `ping()`, which returns `"pong"`. It is there
so you can probe the host-function bridge.

## How to submit

```bash
curl -s -X POST "$URL/run" -H 'Content-Type: application/json' \
     -H "User: $(printf 'my secret passphrase' | shasum -a 256 | cut -d' ' -f1)" \
     -d '{"code": "ping()"}'
```

Or use the client from the repository, which hashes the passphrase for you:

```bash
python challenge/hackpydeno.py --url "$URL" --passphrase "my secret passphrase" exploit.js
```

- Body: JSON `{"code": "..."}`, at most 16 KiB. The value of the last expression comes back as
  `{"ok": true, "result": ..., "ms": 12}` or `{"ok": false, "error": "...", "ms": 12}`.
- Optional `User` header: the SHA-256 of a passphrase only you know. We log it, so later you can
  show a request was yours without revealing who you are.
- Limits: per-IP rate limit (429), a global concurrency cap (503), and a body size cap (413).
- `GET /` returns the rules as plain text; `GET /healthz` is the health check.

You can run your own copy any time (`SECRET=x python challenge/server.py`), and you should: it is
the quickest way to iterate. The source is in `challenge/`.

## What counts as a win

**Reading `SECRET` or the contents of `secret.txt` through the sandbox**, from JavaScript you
submitted to the server. The server watches every response and log line for the secret (and
for its obvious disguises: other case, reversed, hex, base64), redacts it, and raises an alarm
named `SECRET_LEAK`. That alarm is **best effort**: a guest that can compute can disguise a value
further than the server looks. So if you got the secret out some other way, **tell us**: send
the SHA-256 of the secret you read, and how.

## Out of scope

- Denial of service: flooding, or simply burning the box. Hitting the limits is not a finding
  (a way to make the *parent* crash or hang with a small request is, see below).
- Social engineering, phishing, or anything aimed at people.
- Attacking the hosting provider, its network, or anything that is not this endpoint.
- Upstream V8 or OS zero-days that you cannot demonstrate on this server. We still want to hear
  about them, as a report, just not as a win.

## How wins are verified

1. The server logs one JSON line per request: timestamp, `User` header, code length, SHA-256 of
   the code, the first 2 KB of the code, the outcome and the duration. Never the secret.
2. A `SECRET_LEAK` event pinpoints the request, or you send us the SHA-256 of the secret you read
   and the code that read it (the log keeps its hash and head; send us the full file). We replay
   it against a fresh copy and confirm it reproduces. The request is logged before the answer is
   sent, so a request cannot be run without leaving its hash, even if you hang up early.
3. Show us your passphrase and we match its SHA-256 to the `User` header.
4. We write up the mechanism, fix it upstream, and add you to the hall of fame.

## Reporting privately

A real escape is a vulnerability in pydeno. Do not post the exploit publicly first. Report it
through GitHub's private advisory form as described in
[SECURITY.md](https://github.com/bmsuisse/pydeno/blob/main/SECURITY.md). Anything found while
playing is in scope for that policy, including crashes and hangs of the host process.

## Hall of fame

| Who | What | When |
|---|---|---|
| *Nobody yet. Could be you.* | | |

## What the sandbox does, and does not, defend

Honest version. Layers, all from [the isolation guide](guides/advanced/isolation.md):

- The guest runs in a separate worker process, supervised and killed from outside on timeout,
  CPU or memory overrun.
- An OS sandbox is applied before V8 starts: Seatbelt on macOS, Landlock plus seccomp on Linux,
  all privileges dropped. No filesystem, network, or new processes.
- V8 runs `--jitless`, so no JIT compiler and no WebAssembly.
- The worker's environment is empty: the `SECRET` variable is not in the process that runs your
  code, and the secret file is outside what it can open.
- The parent treats every worker frame as untrusted input.

What it does **not** promise:

- It does not make V8 bug-free. A V8 memory-corruption bug can still take over the worker; the OS
  layers are what is meant to contain that, and they are the real target here.
- The challenge host also has to be configured well (egress blocked, container hardened). Those
  are the host's layers, not pydeno's.
- Existence of paths can be observable on some platforms (documented in the isolation guide).
- Plain `Runtime` is not a sandbox at all, and is not the target.
- A host function you bind is your authority. Here that is `ping()`, which does nothing.

If you break something, congratulations, and thank you. That is the point.
