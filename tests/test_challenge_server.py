"""The Hack pydeno challenge server (challenge/server.py): the guest must not reach the secret,
and the server's own protections (limits, redaction, logging) must hold."""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import inspect
import json
import os
import socket
import stat
import struct
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.full_sandbox

_SPEC = importlib.util.spec_from_file_location(
    "challenge_server", Path(__file__).parent.parent / "challenge" / "server.py"
)
assert _SPEC and _SPEC.loader
server_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server_mod)

SECRET = "tHr0waway-s3cret-9f8e7d6c"


class Running:
    def __init__(self, tmp_path: Path, **kw: object) -> None:
        self.log = tmp_path / "requests.jsonl"
        self.secret_file = tmp_path / "secret.txt"
        server_mod.write_secret_file(SECRET, self.secret_file)
        opts: dict = dict(rate=1000.0, burst=1000, request_timeout=3.0)
        opts.update(kw)
        self.srv = server_mod.Challenge(
            ("127.0.0.1", 0), secret=SECRET, log_path=self.log, **opts
        )
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
    ) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def run(self, code: str, user: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if user:
            headers["User"] = user
        status, raw = self.request(
            "POST", "/run", json.dumps({"code": code}).encode(), headers
        )
        return status, json.loads(raw)

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def log_lines(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]


@pytest.fixture(scope="module")
def srv(tmp_path_factory: pytest.TempPathFactory):
    s = Running(tmp_path_factory.mktemp("challenge"))
    yield s
    s.close()


def test_simple_js_works(srv: Running) -> None:
    status, body = srv.run("1 + 2")
    assert status == 200 and body["ok"] and body["result"] == 3
    assert isinstance(body["ms"], int)


def test_ping_is_bound(srv: Running) -> None:
    _, body = srv.run("ping()")
    assert body["result"] == "pong"


def test_healthz_and_rules(srv: Running) -> None:
    assert srv.request("GET", "/healthz") == (200, b"ok\n")
    status, text = srv.request("GET", "/")
    assert status == 200 and b"HACK PYDENO" in text
    assert SECRET.encode() not in text


def test_no_cors_headers(srv: Running) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request("GET", "/healthz")
    resp = conn.getresponse()
    resp.read()
    assert not [
        h for h, _ in resp.getheaders() if h.lower().startswith("access-control")
    ]
    conn.close()


ATTACKS = {
    "process.env": "typeof process === 'undefined' ? 'no process' : JSON.stringify(process.env)",
    "require": "require('fs').readFileSync('secret.txt', 'utf8')",
    "import": "import('fs').then(m => m.readFileSync('secret.txt','utf8'))",
    "fetch": "fetch('file:///etc/passwd').then(r => r.text())",
    "fetch_http": "fetch('http://127.0.0.1:1/').then(r => r.text())",
    "Deno": "Deno.env.get('SECRET') + Deno.readTextFileSync('secret.txt')",
    "Deno.core": "Deno.core.ops.op_read_file('secret.txt')",
    "globalThis_keys": "Object.getOwnPropertyNames(globalThis).join(',')",
    "fs_attempt": "new XMLHttpRequest().open('GET', 'file://' + '/proc/self/environ')",
    "path_error": "try { null.x } catch (e) { e.stack + ' | ' + e.message + ' | ' + String(e) }",
    "syntax_error_path": "try { eval('}') } catch (e) { e.stack }",
    "proto": "Object.prototype.constructor.constructor('return typeof process')()",
    "function_ctor": "(function(){}).constructor('return this.process && process.env.SECRET')()",
    "proto_pollute": "Object.prototype.SECRET = 1; ({}).SECRET + JSON.stringify(Object.getPrototypeOf(globalThis))",
    "deep_stack": "try { (function f(){ f() })() } catch (e) { e.stack.slice(0, 2000) }",
    "console": "console.log(typeof process, typeof Deno); console.error(typeof require); 1",
    "error_prepare": "Error.prepareStackTrace = (e, s) => s.map(String).join(); new Error('x').stack",
    "ping_args": "ping(process, SECRET, globalThis)",
}


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_guest_cannot_reach_secret(srv: Running, name: str) -> None:
    status, body = srv.run(ATTACKS[name])
    assert status == 200
    blob = json.dumps(body)
    assert SECRET not in blob and "s3cret" not in blob
    assert not (srv.log_lines() and "SECRET_LEAK" in srv.log.read_text())


def test_guest_sees_no_host_paths_or_env(srv: Running) -> None:
    _, body = srv.run("try { null.x } catch (e) { e.stack }")
    blob = json.dumps(body)
    assert (
        "/Users/" not in blob
        and "/home/" not in blob
        and str(srv.log.parent) not in blob
    )


def test_ping_abuse_never_returns_secret(srv: Running) -> None:
    code = """
    const out = [];
    for (let i = 0; i < 50; i++) out.push(ping(i, 'SECRET', {a: [1, 2]}, null, undefined));
    for (const k of Object.getOwnPropertyNames(ping)) out.push(k);
    out.push(String(ping), ping.constructor.name);
    try { ping.call(null, new Array(1000).fill('x'.repeat(1000))) } catch (e) { out.push(String(e)) }
    try { ping(1n, new Date(), new Uint8Array(10), () => 1, Symbol('x')) } catch (e) { out.push(String(e)) }
    out.slice(0, 5).concat(out.slice(-4))
    """
    status, body = srv.run(code)
    assert status == 200
    assert SECRET not in json.dumps(body)


def test_redaction_fires_when_secret_forced_into_result(tmp_path: Path) -> None:
    s = Running(tmp_path, test_hook=lambda r: {"leaked": SECRET, "also": "x" + SECRET})
    try:
        status, raw = s.request(
            "POST",
            "/run",
            json.dumps({"code": "1"}).encode(),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        assert SECRET.encode() not in raw and b"[REDACTED]" in raw
        lines = s.log_lines()
        assert any(rec.get("event") == "SECRET_LEAK" for rec in lines)
        assert SECRET not in s.log.read_text()
    finally:
        s.close()


def test_secret_in_submitted_code_is_redacted_in_log(tmp_path: Path) -> None:
    s = Running(tmp_path)
    try:
        s.run(f"'{SECRET}'.length")
        text = s.log.read_text()
        assert SECRET not in text and "[REDACTED]" in text
    finally:
        s.close()


def test_413_body_too_large(srv: Running) -> None:
    big = json.dumps({"code": "1" + " " * (server_mod.MAX_BODY + 10)}).encode()
    status, raw = srv.request("POST", "/run", big, {"Content-Type": "application/json"})
    assert status == 413 and json.loads(raw)["ok"] is False


def test_400_malformed_json(srv: Running) -> None:
    for body in (b"{not json", b"[]", b'{"code": 5}', b"{}", b""):
        status, raw = srv.request(
            "POST", "/run", body, {"Content-Type": "application/json"}
        )
        assert status == 400, body
        assert json.loads(raw)["ok"] is False


def test_429_rate_limit(tmp_path: Path) -> None:
    s = Running(tmp_path, rate=0.001, burst=3)
    try:
        codes = [s.run("1")[0] for _ in range(6)]
        assert codes[:3] == [200, 200, 200]
        assert 429 in codes[3:]
    finally:
        s.close()


def test_concurrency_cap_and_hard_timeout_then_recovery(tmp_path: Path) -> None:
    s = Running(tmp_path, max_concurrent=1, request_timeout=2.0)
    try:
        slow: dict = {}

        def runaway() -> None:
            t0 = time.monotonic()
            slow["res"] = s.run("while (true) {}")
            slow["secs"] = time.monotonic() - t0

        t = threading.Thread(target=runaway)
        t.start()
        time.sleep(0.8)
        status, body = s.run("1")
        assert status == 503 and body["ok"] is False
        t.join(30)
        assert not t.is_alive()
        status, body = slow["res"]
        assert status == 200 and body["ok"] is False
        assert slow["secs"] < 12
        # The server is still healthy and the next request works.
        status, body = s.run("40 + 2")
        assert status == 200 and body["result"] == 42
    finally:
        s.close()


def test_log_one_line_per_request_with_user_and_no_plaintext(tmp_path: Path) -> None:
    s = Running(tmp_path)
    try:
        passphrase = "correct horse battery staple"
        user = hashlib.sha256(passphrase.encode()).hexdigest()
        code = "'hello'.length"
        s.run(code, user=user)
        s.run("throw new Error('boom')", user=user)
        s.request("POST", "/run", b"{bad", {"User": user})
        lines = s.log_lines()
        assert len(lines) == 3
        assert all(rec["user"] == user for rec in lines)
        first = lines[0]
        assert first["code_len"] == len(code)
        assert first["code_sha256"] == hashlib.sha256(code.encode()).hexdigest()
        assert first["code_head"] == code
        assert first["outcome"] == "ok" and "ms" in first and "ts" in first
        text = s.log.read_text()
        assert passphrase not in text and SECRET not in text
    finally:
        s.close()


# ---- round-3 review findings -----------------------------------------------------------------------------


def _raw(port: int, data: bytes, *, reset: bool = False) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(data)
    if reset:  # close with RST, as a client that hangs up mid-request does
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    return sock


def _wait_for(predicate, seconds: float = 8.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_stalled_connections_are_closed_not_held_forever(tmp_path: Path) -> None:
    """A slowloris (headers trickled in, never finished) used to hold one thread per connection."""
    s = Running(tmp_path, socket_timeout=1.0, max_connections=8)
    try:
        socks = [_raw(s.port, b"POST /run HTTP/1.1\r\nHost: x\r\n") for _ in range(30)]
        # while they stall, the server never has more than max_connections threads on them
        assert threading.active_count() < 8 + 20
        time.sleep(2.0)  # past socket_timeout: the stalled ones are closed
        status, _ = s.request("GET", "/healthz")
        assert status == 200
        for sock in socks:
            sock.close()
    finally:
        s.close()


def test_a_client_that_hangs_up_is_still_logged(tmp_path: Path) -> None:
    """The code ran, so the record must exist whether or not anyone read the answer."""
    s = Running(tmp_path)
    try:
        code = "1 + 1"
        body = json.dumps({"code": code}).encode()
        request = (
            b"POST /run HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        _raw(s.port, request, reset=True).close()
        digest = hashlib.sha256(code.encode()).hexdigest()
        assert _wait_for(
            lambda: any(r.get("code_sha256") == digest for r in s.log_lines())
        )
    finally:
        s.close()


def test_the_log_line_exists_before_the_client_sees_the_response(
    tmp_path: Path,
) -> None:
    s = Running(tmp_path)
    try:
        for _ in range(10):
            s.run("2 + 2")
            assert s.log_lines()  # no sleep: logged before the answer was sent
    finally:
        s.close()


def _two_xff_lines(port: int, first: str, last: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        body = json.dumps({"code": "1"}).encode()
        conn.putrequest("POST", "/run")
        conn.putheader("X-Forwarded-For", first)
        conn.putheader("X-Forwarded-For", last)  # a second header line
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        return conn.getresponse().status
    finally:
        conn.close()


def test_a_second_x_forwarded_for_line_cannot_dodge_the_rate_limit(
    tmp_path: Path,
) -> None:
    s = Running(tmp_path, trust_proxy=True, rate=0.001, burst=2)
    try:
        # the client varies the FIRST line; the trusted proxy's hop is always the last one
        statuses = [_two_xff_lines(s.port, f"9.9.9.{i}", "10.0.0.1") for i in range(6)]
        assert 429 in statuses, statuses
    finally:
        s.close()


def test_a_garbage_x_forwarded_for_value_does_not_become_a_table_key(
    tmp_path: Path,
) -> None:
    s = Running(tmp_path, trust_proxy=True, rate=1000.0, burst=1000)
    try:
        for i in range(5):
            _two_xff_lines(s.port, "1.1.1.1", "garbage-" + "x" * 50_000 + str(i))
        assert len(s.srv.buckets._state) == 1  # noqa: SLF001 - all fell back to the socket address
    finally:
        s.close()


@pytest.mark.parametrize(
    "disguise",
    [
        lambda x: x.upper(),
        lambda x: x[::-1],
        lambda x: x.encode().hex(),
        lambda x: base64.b64encode(x.encode()).decode(),
        lambda x: base64.b64encode(b"ab" + x.encode()).decode(),
        lambda x: base64.urlsafe_b64encode(b"a" + x.encode()).decode().rstrip("="),
    ],
    ids=["upper", "reversed", "hex", "base64", "base64-shifted", "base64url-shifted"],
)
def test_common_disguises_of_the_secret_are_redacted_and_alarm(
    tmp_path: Path, disguise
) -> None:  # type: ignore[no-untyped-def]
    s = Running(tmp_path, test_hook=lambda _r: disguise(SECRET))
    try:
        _, body = s.run("1")
        text = json.dumps(body)
        assert disguise(SECRET) not in text
        assert "[REDACTED]" in text
        assert any(r.get("event") == "SECRET_LEAK" for r in s.log_lines())
    finally:
        s.close()


def test_nan_and_infinity_do_not_produce_invalid_json(srv: Running) -> None:
    status, raw = srv.request(
        "POST",
        "/run",
        json.dumps({"code": "0 / 0"}).encode(),
        {"Content-Type": "application/json"},
    )

    def refuse(constant: str) -> None:
        raise AssertionError(f"invalid JSON constant {constant}")

    json.loads(raw, parse_constant=refuse)  # strict
    assert status == 200


def test_files_are_private(tmp_path: Path) -> None:
    s = Running(tmp_path)
    try:
        s.run("1")
        assert stat.S_IMODE(os.stat(s.secret_file).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(s.log).st_mode) == 0o600
    finally:
        s.close()


def test_malformed_requests_are_logged_too(tmp_path: Path) -> None:
    s = Running(tmp_path)
    try:
        _raw(s.port, b"GARBAGE\r\n\r\n").close()
        assert _wait_for(
            lambda: any(r.get("event") == "http_error" for r in s.log_lines())
        )
    finally:
        s.close()


def test_the_test_hook_cannot_be_set_from_the_command_line() -> None:
    assert "test_hook" not in inspect.getsource(server_mod.main)


def test_the_user_header_must_match_exactly() -> None:
    assert server_mod.USER_RE.fullmatch("a" * 64) is not None
    assert (
        server_mod.USER_RE.fullmatch("a" * 64 + "\n") is None
    )  # `$` alone would accept this
