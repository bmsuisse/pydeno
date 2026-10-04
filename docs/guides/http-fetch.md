# Network access: `http_fetch`

pydeno has no permission model. A runtime grants nothing, and what guest code can reach is exactly
what the host bound into it. So there is no `--allow-net=host` flag; the equivalent is a host tool
that does the fetching and decides what is allowed. `pydeno.http_fetch` is that tool, written once
with the server-side request forgery (SSRF) cases handled:

```python
from pydeno import AgentSandbox, http_fetch

fetch = http_fetch(
    ["api.example.com/v1/", "status.example.com"],
    headers={"Authorization": "Bearer ..."},   # the guest can neither set nor read these
    timeout=10.0,
    max_response_bytes=256 * 1024,
)

with AgentSandbox({"fetch_url": fetch}, max_tool_calls=20) as session:
    prompt = session.describe_tools()   # lists the allowed URLs for the model
    result = session.run("""
        const r = await fetch_url("https://api.example.com/v1/items?limit=5")
        if (r.status !== 200) return { error: r.status }
        return JSON.parse(r.body)
    """)
```

The guest passes one URL and gets back plain data:

```js
{ status: 200, headers: { "content-type": "application/json" }, body: "...", truncated: false,
  url: "https://api.example.com/v1/items?limit=5" }
```

- `status` is the final status after redirects. 404 and 500 are results, not errors.
- `headers` holds only the allow-listed response headers (`response_headers=`, by default
  `content-type`, `content-length`, `content-language`, `etag`, `last-modified`, `cache-control`,
  `expires`, `date`, `retry-after`), lowercased. `Set-Cookie` and the rest are dropped.
- `body` is text (decoded with the response charset, else UTF-8, bad bytes replaced) or, with
  `response="bytes"`, raw bytes (a `Uint8Array` in JavaScript).
- `truncated` is set when the body was cut at `max_response_bytes`, or the server closed the
  connection before sending the length it announced.
- `url` is the URL that answered, after redirects.

A refused or failed request throws in the guest, with an error `name` it can branch on:

| `e.name` | When |
|---|---|
| `HttpFetchBlocked` | The policy refused it: URL, scheme, host, resolved address, redirect, size of the URL |
| `HttpFetchTimeout` | The whole call (DNS, connect, TLS, redirects, body) ran past `timeout` |
| `HttpFetchFailed` | Allowed, but it failed: no such host, connection refused, TLS or HTTP error |

All three are `pydeno.ToolError` subclasses (`HttpFetchError` is their base). Their messages are
fixed strings written by pydeno ("the URL is not in the allow-list"), never a resolved address,
a response body or a header value, so they are shown to the guest even when the session redacts
host errors. A model can read why it was refused and correct its next call.

## Binding it

The tool is an ordinary callable, so it goes wherever a tool goes:

| Where | What to pass |
|---|---|
| `AgentSandbox({"fetch_url": fetch})` | `fetch`, or `fetch.aio` |
| `AsyncAgentSandbox({"fetch_url": fetch})` | either; the sync form already runs off the event loop |
| `ToolBridge({"fetch_url": fetch.aio}).attach(runtime)` | `fetch.aio`, so a slow request does not block the runtime thread |

`fetch.aio` is the async form of the same tool: the blocking socket work runs in a worker thread.

Every call is a tool call, so it counts against the session's `max_tool_calls` (or the bridge's
`max_calls`), and in an `AgentSandbox` it is recorded in the journal like any other: its result is
plain data, and a session restored with `AgentSandbox.load()` replays it from the journal without
issuing the request again.

## Options

| Option | Default | Meaning |
|---|---|---|
| `allow` | required | Exact destinations, see below |
| `schemes` | `("https",)` | Add `"http"` only deliberately |
| `headers` | none | Fixed request headers (an API key). Transport headers (`Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, ...) are refused |
| `timeout` | `10.0` | Seconds for the whole call, a deadline rather than a per-read timeout |
| `max_response_bytes` | 1 MiB | Body bytes kept; the rest is never read |
| `max_url_length` | 2048 | The request-size cap (GET only, no request body) |
| `max_redirects` | 5 | Hops followed, each checked from scratch |
| `redirects` | `"same-origin"` | `"allow-list"` follows to any allowed URL; `"never"` returns the 3xx |
| `response` | `"text"` | Or `"bytes"` |
| `response_headers` | see above | Response header names passed back |
| `resolver` | system | `resolver(host, port) -> [ip, ...]`; its answers are vetted like the system's |
| `ssl_context` | `ssl.create_default_context()` | For a private CA, say |

**Allow entries** are matched exactly:

- `"api.example.com"`: any path on the default port of the scheme.
- `"api.example.com:8443"`: that port only.
- `"api.example.com/v1/"`: paths under `/v1/` (and `/v1` itself), on a segment boundary, so
  `/v1evil` does not match.
- `"https://api.example.com/v1/"`: that scheme only.

There is no wildcard and no suffix matching: `example.com` does not allow `www.example.com`. Hosts
are compared lowercase after IDNA encoding (`bücher.example` is `xn--bcher-kva.example`). An IP
address is allowed only in canonical form (`203.0.113.7`, `[2001:db8::1]`), and a private or
loopback one is refused at request time anyway. Cloud metadata names (`metadata.google.internal`,
`metadata`, `instance-data`, ...) are refused even if listed.

## Threat model

The guest is untrusted code, often written by a model that read untrusted text. It controls the
URL and nothing else. The tool runs in the host process, inside the host's network, which can
usually reach things the internet cannot: cloud metadata services, admin ports on loopback,
databases on the private network. The goal is that the only thing the guest can make the host
fetch is a GET to an allow-listed URL on a public address, with a bounded response.

What every request, and every redirect hop, goes through:

1. **Strict parsing.** The URL must be absolute and ASCII outside the host. Whitespace and
   control characters are refused outright, so a CR/LF cannot inject a header or a second request
   (percent-encoded `%0D%0A` stays encoded on the wire). Refused as well: userinfo
   (`https://user:pass@host/`, `https://allowed@evil/`), IPv6 zone ids, a port that is out of
   range or not the entry's, `.`/`..` path segments (also percent-encoded) and encoded `/` or `\`
   in the path, which servers decode differently and could use to step out of a path prefix.
2. **Numeric hosts in one form only.** Browsers and libraries read `2130706433`, `0177.0.0.1`,
   `0x7f.1` and `127.1` as `127.0.0.1`. A host whose last label is numeric must be a canonical
   dotted-decimal IPv4 address, so no two parsers can disagree about which address it names.
3. **The allow-list**, as above.
4. **Resolve once, check every answer.** The host name is resolved (A and AAAA). If **any**
   answer is loopback, private (RFC 1918), link-local (`169.254.0.0/16`, which holds the
   metadata address `169.254.169.254`, and `fe80::/10`), CGNAT (`100.64.0.0/10`), unspecified,
   multicast, broadcast, reserved, documentation, benchmarking, unique-local (`fc00::/7`, which
   holds `fd00:ec2::254`), or an IPv6 address embedding such an IPv4 address (`::ffff:a.b.c.d`,
   NAT64 `64:ff9b::/96`, 6to4 `2002::/16`, Teredo), the request is refused. The error does not say
   which address it was.
5. **Connect to the address that was checked.** The socket is opened to the vetted IP; the TLS
   server name, the certificate check and the `Host` header use the host name. A DNS server that
   answers with a public address for the check and a private one for the connection (DNS
   rebinding) gets nowhere: there is no second lookup.
6. **No proxy, no guest headers.** `http.client` does not read `HTTP_PROXY`/`HTTPS_PROXY`, so the
   environment cannot route requests elsewhere. The guest cannot pass headers or options (the tool
   takes exactly one argument); the request carries `Host`, `User-Agent`, `Accept-Encoding:
   identity`, `Connection: close` and the host's fixed headers.
7. **Bounded response.** The body is read in chunks up to `max_response_bytes`, whatever
   `Content-Length` says (a server claiming a small length cannot make it read more; a server
   claiming a huge one cannot make it allocate). No transparent decompression, so no zip bombs.
   The status line and headers are bounded by `http.client` (64 KiB per line, 100 headers).
8. **One deadline.** `timeout` covers resolution, connection, TLS, every hop and the body. A
   watchdog closes the socket at the deadline, so a server dripping one byte at a time cannot
   hold the call open.
9. **Redirects** are never followed by the HTTP library. Each `Location` goes back through
   steps 1 to 8: at most `max_redirects` hops, never from `https` to `http`, by default only to
   the same scheme, host and port, and the host's fixed headers are not sent to another origin
   (with `redirects="allow-list"`).

### What this does not cover

- **What the allowed hosts do.** An allow-listed API is reachable with the host's credentials;
  the guest can call any GET endpoint under the allowed prefix as often as the budget allows.
  Scope entries tightly, and use separate tools (with separate headers) for hosts with different
  credentials.
- **An allowed host that is itself an open redirect or a proxy.** With the default
  `redirects="same-origin"` an open redirect on the allowed host cannot leave it, but an endpoint
  that fetches a URL server-side is out of reach of any client-side check.
- **Exfiltration through the URL.** The guest chooses the path and the query string, so it can
  send what it knows to an allowed host. If that matters, allow only hosts you trust with the
  session's data.
- **Request methods other than GET**, request bodies and cookies: not supported.
- **Resolver slowness.** The system resolver cannot be cancelled: a lookup that runs past the
  deadline raises `HttpFetchTimeout` in the caller, while the lookup itself finishes in a
  background thread (at most 8 at a time per process).
- **TLS policy** is Python's default context (system trust store, certificate and host name
  verification, TLS 1.2 minimum). Pass `ssl_context=` to change it.
- **Plain `http`** (when you add it to `schemes`) is readable and modifiable by anyone on the path.

The checks are tested against a local server (`tests/test_http_fetch.py`): redirects to another
host and to loopback, the 5-hop cap, a resolver that changes its answer between calls, userinfo,
decimal, octal and hex IPv4 forms, IPv6 loopback and v4-mapped addresses, the metadata address, a
body larger than the cap with and without a lying `Content-Length`, the deadline, non-https
schemes, CR/LF in the URL, and guest-supplied headers.
