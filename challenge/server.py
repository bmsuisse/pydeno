"""Hack pydeno: a tiny HTTP server that runs a visitor's JavaScript in a fresh, sandboxed
`pydeno.IsolatedRuntime` per request. The goal is to read the host's secret.

Standard library only (plus pydeno). Run:  SECRET=hunter2 python challenge/server.py --port 8080

Endpoints: POST /run {"code": "..."}, GET /healthz, GET / (the rules, plain text).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from pydeno import IsolatedRuntime, RuntimeConfig

HERE = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024
MAX_OUTPUT = 64 * 1024
LOG_CODE_BYTES = 2048
USER_RE = re.compile(r"^[0-9a-fA-F]{64}$")

RULES = """\
HACK PYDENO
===========
Goal: read the secret that lives on the host. It is in the SECRET environment variable
of the server process and in the file secret.txt next to the server. Your JavaScript
runs inside pydeno's IsolatedRuntime (OS sandbox, jitless V8, hard limits).

Submit:  POST /run   {"code": "1 + 1"}     (JSON, at most 16 KiB)
         optional header  User: <sha256 of a passphrase only you know>
         so you can find your own requests in the log later.
         Client: python hackpydeno.py --url URL --passphrase P file.js
The host binds one function, ping(), so you can poke the bridge.
The value of the last expression is returned as JSON: {ok, result|error, ms}.

A win is reading SECRET or secret.txt through the sandbox. The server redacts the secret (and
its obvious encodings) from responses and logs and raises a best-effort alarm, but an alarm that
does not fire is not proof that nobody won: tell us if you did, and how.
Out of scope: DoS, social engineering, attacking the host provider. Be kind. Full rules:
docs/hack-pydeno.md. Report private findings via SECURITY.md.
"""


def _variants(secret: str) -> list[str]:
    """The spellings of the secret a careless leak would produce: as is, JSON-escaped, reversed,
    hex, and base64 at every alignment. Matching is case-insensitive. This is a best-effort alarm,
    not a proof: a guest that can compute can always disguise a value further."""
    raw = secret.encode()
    out = {secret, json.dumps(secret)[1:-1], secret[::-1], raw.hex()}
    for pad in range(3):
        shifted = b"\x00" * pad + raw
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            text = encoder(shifted).decode().rstrip("=")
            # drop the characters that depend on the padding bytes around the secret
            out.add(text[(pad * 4 + 2) // 3 : -2 if len(text) > 8 else None])
    return sorted({v for v in out if len(v) >= 6}, key=len, reverse=True)


class TokenBuckets:
    """Per-IP token bucket: `burst` tokens, refilled at `rate` per second."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, burst
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            ok = tokens >= 1.0
            if ok:
                tokens -= 1.0
            self._state[key] = (tokens, now)
            if len(self._state) > 10_000:  # bounded memory: drop the oldest quarter
                for k in sorted(self._state, key=lambda k: self._state[k][1])[:2500]:
                    self._state.pop(k, None)
            return ok


class Challenge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        addr: tuple[str, int],
        *,
        secret: str,
        log_path: str | os.PathLike[str],
        max_concurrent: int = 4,
        rate: float = 1.0,
        burst: int = 5,
        request_timeout: float = 3.0,
        max_memory: int = 256 * 1024 * 1024,
        trust_proxy: bool = False,
        max_connections: int = 64,
        socket_timeout: float = 10.0,
        test_hook: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(addr, Handler)
        if len(secret) < 6:
            raise ValueError("secret too short to redact safely")
        self.secret = secret
        self.log_path = str(log_path)
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.buckets = TokenBuckets(rate, burst)
        self.request_timeout = request_timeout
        self.max_memory = max_memory
        self.trust_proxy = trust_proxy
        # One thread per connection, so a slowloris (headers trickled in forever) would use them all:
        # cap the connections, and give every socket a read timeout.
        self._connections = threading.BoundedSemaphore(max_connections)
        self.socket_timeout = socket_timeout
        self._variants = _variants(secret)
        # Tests only: lets a test force the secret into a result to prove redaction works.
        self.test_hook = test_hook
        self._log_lock = threading.Lock()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connections.acquire(blocking=False):
            try:
                request.close()  # no thread for it
            except OSError:
                pass
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            self._connections.release()

    # -- helpers ----------------------------------------------------------

    def leaks(self, text: str) -> bool:
        folded = text.casefold()
        return any(v.casefold() in folded for v in self._variants)

    def redact(self, text: str) -> str:
        for variant in self._variants:
            text = re.sub(re.escape(variant), "[REDACTED]", text, flags=re.IGNORECASE)
        return text

    def log(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=True)
        if self.leaks(line):  # never write the secret, whatever the cause
            record = {"ts": record.get("ts"), "event": "SECRET_LEAK", "where": "log"}
            line = json.dumps(record)
            print("SECRET_LEAK: secret reached a log line", file=sys.stderr, flush=True)
        with self._log_lock:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def run_code(self, code: str) -> tuple[dict[str, Any], str]:
        """Execute `code` in a fresh sandbox. Returns (response body, outcome label)."""
        hard = self.request_timeout
        box: dict[str, Any] = {}
        rt_holder: list[IsolatedRuntime] = []

        def work() -> None:
            try:
                rt = IsolatedRuntime(
                    RuntimeConfig(timeout=hard),
                    sandbox="require",
                    max_memory=self.max_memory,
                    request_timeout=hard,
                    max_host_calls=200,
                )
                rt_holder.append(rt)
                try:
                    rt.bind_function("ping", lambda *_a: "pong")
                    box["result"] = rt.eval(code)
                finally:
                    rt.close()
            except BaseException as exc:  # noqa: BLE001 - everything becomes an error reply
                box["error"] = f"{type(exc).__name__}: {exc}"

        t = threading.Thread(target=work, daemon=True)
        t.start()
        # Hard wall clock on top of pydeno's own deadlines: start-up + grace + slack.
        t.join(hard + 6.0)
        if t.is_alive():
            for rt in rt_holder:
                try:
                    rt.close()
                except Exception:  # noqa: BLE001
                    pass
            t.join(3.0)
            return {"ok": False, "error": "wall-clock limit exceeded"}, "wallclock"
        if "error" in box:
            return {"ok": False, "error": str(box["error"])[:2000]}, "js_error"
        result = box.get("result")
        if self.test_hook is not None:
            result = self.test_hook(result)
        return {"ok": True, "result": result}, "ok"


class Handler(BaseHTTPRequestHandler):
    server: Challenge
    server_version = "hackpydeno"
    sys_version = ""

    def setup(self) -> None:
        self.timeout = (
            self.server.socket_timeout
        )  # a read that stalls is closed, not waited for
        super().setup()

    def log_message(self, *_args: Any) -> None:  # we write our own JSON log
        pass

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        # Malformed requests and unsupported methods never reach `do_POST`; they are still traffic.
        self.server.log({"ts": time.time(), "event": "http_error", "status": code})
        super().send_error(code, message, explain)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _encode(self, obj: dict[str, Any]) -> tuple[str, bool]:
        """The response text, with the secret redacted; and whether it had to be."""
        try:
            text = json.dumps(obj, default=_default, ensure_ascii=True, allow_nan=False)
        except ValueError:  # NaN / Infinity are not JSON
            text = json.dumps(
                {"ok": False, "error": "result is not representable as JSON"}
            )
        if len(text) > MAX_OUTPUT:
            text = json.dumps({"ok": False, "error": "response too large"})
        leaked = self.server.leaks(text)
        if leaked:
            text = self.server.redact(text)
            self.server.log(
                {"ts": time.time(), "event": "SECRET_LEAK", "where": "response"}
            )
            print("SECRET_LEAK: secret reached a response", file=sys.stderr, flush=True)
        return text, leaked

    def _json(self, status: int, obj: dict[str, Any]) -> bool:
        """Send `obj`; returns True if the secret had to be redacted."""
        text, leaked = self._encode(obj)
        self._send(status, text.encode(), "application/json")
        return leaked

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
        elif self.path == "/":
            self._send(200, RULES.encode(), "text/plain; charset=utf-8")
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._json(404, {"ok": False, "error": "not found"})
            return
        t0 = time.monotonic()
        srv = self.server
        user = self.headers.get("User", "")
        user = user if USER_RE.fullmatch(user) else ("" if not user else "invalid")
        rec: dict[str, Any] = {"ts": time.time(), "user": user.lower(), "code_len": 0}

        def done(status: int, body: dict[str, Any], outcome: str) -> None:
            ms = int((time.monotonic() - t0) * 1000)
            body["ms"] = ms
            rec.update(outcome=outcome, status=status, ms=ms)
            text, leaked = self._encode(body)
            if leaked:
                rec["event"] = "SECRET_LEAK"
            # Logged BEFORE the answer is sent: the record must exist when the client sees the
            # response, and a client that hangs up (or a crash) cannot make the request vanish.
            srv.log(rec)
            try:
                self._send(status, text.encode(), "application/json")
            except OSError:
                pass  # the client hung up; the code already ran and is already logged

        ip = self.client_address[0]
        if srv.trust_proxy:
            # Every X-Forwarded-For line (a client can send several), the last hop (the one the
            # trusted proxy appended), and only if it is an address: the value keys a table.
            hops = ",".join(self.headers.get_all("X-Forwarded-For") or []).split(",")
            try:
                ip = str(ipaddress.ip_address(hops[-1].strip()[:64]))
            except ValueError:
                pass
        if not srv.buckets.allow(ip):
            return done(429, {"ok": False, "error": "rate limited"}, "rate_limited")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return done(
                411, {"ok": False, "error": "Content-Length required"}, "no_length"
            )
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return done(413, {"ok": False, "error": "body too large"}, "too_large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
            code = payload["code"]
            if not isinstance(code, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return done(
                400, {"ok": False, "error": 'expected JSON {"code": "..."}'}, "bad_json"
            )
        rec["code_len"] = len(code)
        rec["code_sha256"] = hashlib.sha256(code.encode()).hexdigest()
        rec["code_head"] = (
            srv.redact(code).encode()[:LOG_CODE_BYTES].decode("utf-8", "replace")
        )
        if not srv.slots.acquire(blocking=False):
            return done(503, {"ok": False, "error": "busy, try again"}, "busy")
        try:
            body, outcome = srv.run_code(code)
        finally:
            srv.slots.release()
        done(200, body, outcome)


def _default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return {"bytes": obj.hex()}
    return repr(obj)


def write_secret_file(secret: str, secret_file: Path) -> None:
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret + "\n")
    secret_file.chmod(0o600)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument(
        "--log", default=os.environ.get("HACK_LOG", str(HERE / "requests.jsonl"))
    )
    p.add_argument(
        "--secret-file", default=os.environ.get("SECRET_FILE", str(HERE / "secret.txt"))
    )
    p.add_argument("--max-concurrent", type=int, default=4)
    p.add_argument("--rate", type=float, default=1.0, help="requests/second per IP")
    p.add_argument("--burst", type=int, default=5)
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--max-memory-mb", type=int, default=256)
    p.add_argument(
        "--trust-proxy",
        action="store_true",
        help="use last X-Forwarded-For hop as client IP",
    )
    a = p.parse_args()
    secret = os.environ.get("SECRET", "")
    if not secret:
        sys.exit("SECRET environment variable is required")
    write_secret_file(secret, Path(a.secret_file))
    srv = Challenge(
        (a.host, a.port),
        secret=secret,
        log_path=a.log,
        max_concurrent=a.max_concurrent,
        rate=a.rate,
        burst=a.burst,
        request_timeout=a.timeout,
        max_memory=a.max_memory_mb * 1024 * 1024,
        trust_proxy=a.trust_proxy,
    )
    print(
        f"hack pydeno listening on http://{a.host}:{srv.server_address[1]}", flush=True
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
