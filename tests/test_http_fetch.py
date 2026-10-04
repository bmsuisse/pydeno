"""`pydeno.http_fetch`: the allow-listed, SSRF-safe GET tool (issue #39).

Every test talks to a local `http.server` on 127.0.0.1. The public API refuses loopback, so the
tools here are built with `_allow_loopback_for_tests`, which is not reachable from `http_fetch()`
(see `test_public_default_refuses_loopback`), and with an injected resolver that maps the test
hostnames to 127.0.0.1. Private, link-local and metadata addresses stay refused under the switch,
which is what lets the SSRF cases be tested end to end without leaving the machine.

Portable: no POSIX-only module. ToolBridge and AgentSandbox import `pydeno._isolated`, which is
POSIX-only, so binding coverage is in `test_http_fetch_agent.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import pydeno
from pydeno import ToolError
from pydeno.tools.http_fetch import (
    HttpFetch,
    HttpFetchBlocked,
    HttpFetchError,
    HttpFetchFailed,
    HttpFetchTimeout,
    _address_refused,
    _allow_loopback_for_tests,
    http_fetch,
)

# Test-only, self-signed for `fetch.test`, valid until 2126; trusted by nothing but these tests.
# openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 36500
#   -subj /CN=fetch.test -addext subjectAltName=DNS:fetch.test
#   -addext basicConstraints=critical,CA:TRUE   (key and certificate concatenated)
CERT = Path(__file__).parent / "data" / "http_fetch_tls.pem"


# ---------------------------------------------------------------------------
# a local server
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: Server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802, C901, PLR0912
        self.server.requests.append((self.path, dict(self.headers.items())))
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if path in ("/ok", "/v1/ok", "/v1"):
            self._send(
                200,
                b"hello",
                {"Content-Type": "text/plain; charset=utf-8", "X-Secret": "s3cret"},
            )
        elif path == "/latin1":
            self._send(
                200,
                "café".encode("latin-1"),
                {"Content-Type": "text/plain; charset=latin-1"},
            )
        elif path == "/bytes":
            self._send(
                200, bytes(range(256)), {"Content-Type": "application/octet-stream"}
            )
        elif path == "/headers":
            self._send(200, b"", {"Set-Cookie": "a=1", "Content-Type": "text/plain"})
        elif path.startswith("/redirect/"):
            n = int(path.rsplit("/", 1)[1])
            if n == 0:
                self._send(200, b"done")
            else:
                self._send(302, b"", {"Location": f"/redirect/{n - 1}"})
        elif path == "/to":
            self._send(int(q.get("status", "302")), b"", {"Location": q["loc"]})
        elif path == "/status/404":
            self._send(404, b"nope")
        elif path == "/big":
            # No Content-Length: the body ends when the connection closes.
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(64):
                    self.wfile.write(b"x" * 65536)
            except OSError:
                pass
        elif path == "/liar":
            self.send_response(200)
            self.send_header("Content-Length", q["cl"])
            self.end_headers()
            self.wfile.write(b"y" * int(q["n"]))
        elif path == "/slow":
            time.sleep(float(q.get("s", "3")))
            self._send(200, b"late")
        elif path == "/drip":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(1000):
                    self.wfile.write(b"z")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
        else:
            self._send(404, b"")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[tuple[str, dict[str, str]]] = []
        if context is not None:
            self.socket = context.wrap_socket(self.socket, server_side=True)
        self.thread = threading.Thread(
            target=self.serve_forever, args=(0.02,), daemon=True
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.stop()


class Resolver:
    """Maps test hostnames to answers; records every call."""

    def __init__(self, answers: dict[str, list[str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        if host not in self.answers:
            raise socket.gaierror("no such host")
        return list(self.answers[host])


LOCAL = {"fetch.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]}


def make(
    allow: list[str], resolver: Resolver | None = None, **kwargs: Any
) -> HttpFetch:
    kwargs.setdefault("schemes", ("http", "https"))
    kwargs.setdefault("timeout", 5.0)
    return _allow_loopback_for_tests(
        HttpFetch(allow, resolver=resolver or Resolver(LOCAL), **kwargs)
    )


def blocked(tool: HttpFetch, url: str) -> str:
    with pytest.raises(HttpFetchBlocked) as info:
        tool(url)
    return str(info.value)


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------


class TestResult:
    def test_plain_data_with_allow_listed_headers(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        out = tool(f"http://fetch.test:{server.port}/ok")
        assert out["headers"].pop("date")  # allow-listed; `http.server` always sends it
        assert out == {
            "status": 200,
            "headers": {
                "content-type": "text/plain; charset=utf-8",
                "content-length": "5",
            },
            "body": "hello",
            "truncated": False,
            "url": f"http://fetch.test:{server.port}/ok",
        }

    def test_bytes_response(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], response="bytes")
        assert tool(f"http://fetch.test:{server.port}/bytes")["body"] == bytes(
            range(256)
        )

    def test_charset_is_honoured(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        assert tool(f"http://fetch.test:{server.port}/latin1")["body"] == "café"

    def test_error_status_is_a_result(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        out = tool(f"http://fetch.test:{server.port}/status/404")
        assert (out["status"], out["body"]) == (404, "nope")

    def test_response_headers_are_filtered(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        assert (
            "set-cookie"
            not in tool(f"http://fetch.test:{server.port}/headers")["headers"]
        )
        wide = make([f"fetch.test:{server.port}"], response_headers=["set-cookie"])
        assert wide(f"http://fetch.test:{server.port}/headers")["headers"] == {
            "set-cookie": "a=1"
        }

    def test_request_headers_are_fixed(self, server: Server) -> None:
        tool = make(
            [f"fetch.test:{server.port}"], headers={"Authorization": "Bearer k"}
        )
        tool(f"http://fetch.test:{server.port}/ok")
        (_, headers) = server.requests[0]
        assert {k.lower(): v for k, v in headers.items()} == {
            "host": f"fetch.test:{server.port}",
            "user-agent": "pydeno-http-fetch",
            "accept-encoding": "identity",
            "connection": "close",
            "authorization": "Bearer k",
        }


# ---------------------------------------------------------------------------
# public defaults
# ---------------------------------------------------------------------------


class TestPublicDefaults:
    def test_loopback_switch_is_not_public(self) -> None:
        for fn in (http_fetch, HttpFetch):
            names = set(inspect.signature(fn).parameters)
            assert not any("loopback" in n or n.startswith("_") for n in names)
        assert http_fetch(["fetch.test"])._allow_loopback is False

    def test_public_default_refuses_loopback(self, server: Server) -> None:
        resolver = Resolver(LOCAL)
        tool = http_fetch(
            [f"fetch.test:{server.port}", f"127.0.0.1:{server.port}"],
            schemes=["http"],
            resolver=resolver,
        )
        assert "not allowed" in blocked(tool, f"http://fetch.test:{server.port}/ok")
        assert "not allowed" in blocked(tool, f"http://127.0.0.1:{server.port}/ok")
        assert server.requests == []

    @pytest.mark.parametrize(
        "url",
        [
            "http://fetch.test/ok",
            "ftp://fetch.test/ok",
            "file:///etc/passwd",
            "gopher://fetch.test/",
            "javascript:alert(1)",
            "data:text/plain,hi",
        ],
    )
    def test_https_only_by_default(self, url: str) -> None:
        resolver = Resolver(LOCAL)
        tool = http_fetch(["fetch.test"], resolver=resolver)
        blocked(tool, url)
        assert resolver.calls == []

    def test_widening_schemes_must_be_explicit(self) -> None:
        with pytest.raises(ValueError, match="scheme"):
            http_fetch(["http://fetch.test/"])
        with pytest.raises(ValueError):
            http_fetch(["fetch.test"], schemes=["ftp"])

    def test_errors_are_tool_errors_and_public(self) -> None:
        for cls in (HttpFetchBlocked, HttpFetchTimeout, HttpFetchFailed):
            assert issubclass(cls, HttpFetchError)
            assert issubclass(cls, ToolError)
            assert cls("x")._pydeno_public is True

    def test_model_sees_the_allow_list(self) -> None:
        tool = http_fetch(["api.example.com/v1/", "Files.Example.com:8443"])
        assert tool.allowed == (
            "https://api.example.com/v1/",
            "https://files.example.com:8443/",
        )
        assert "https://api.example.com/v1/" in (tool.__doc__ or "")
        assert tool.aio.__doc__ == tool.__doc__

    def test_lazy_export(self) -> None:
        assert pydeno.http_fetch is http_fetch
        assert "http_fetch" in pydeno.__all__


# ---------------------------------------------------------------------------
# allow-list
# ---------------------------------------------------------------------------

# Paths that a backend stripping `;params` (Tomcat, Jetty, Spring), decoding twice, or stopping at
# NUL would read as something outside `/v1/`, while the raw string starts with `/v1/`.
PREFIX_BYPASSES = [
    "/v1/..;/admin",
    "/v1/..;x=1/admin",
    "/v1/%2e%2e;/admin",
    "/v1/%2E%2E;/admin",
    "/v1/..%3b/admin",
    "/v1/..%3B/admin",
    "/v1/%2e%2e%3B/admin",
    "/v1/.;/ok",
    "/v1;/../admin",
    "/v1/ok;jsessionid=1",
    "/v1/%252e%252e/admin",
    "/v1/%252E%252E/admin",
    "/v1/%25%32%65%25%32%65/admin",
    "/v1/%252f..%252fadmin",
    "/v1/..%00/admin",
    "/v1/%2e%2e%00/admin",
    "/v1/.../admin",
]


class TestAllowList:
    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.test:{p}/ok",  # another host
            "http://fetch.test.evil:{p}/ok",  # suffix trick
            "http://sub.fetch.test:{p}/ok",  # no subdomain match
            "http://fetch.test:1/ok",  # another port
            "http://fetch.test/ok",  # default port is not {p}
            "http://fetch.test:{p}/v1evil",  # prefix on a segment boundary only
            "http://fetch.test:{p}/v2/ok",
        ],
    )
    def test_refused(self, server: Server, url: str) -> None:
        resolver = Resolver({**LOCAL, "evil.test": ["127.0.0.1"]})
        tool = make([f"fetch.test:{server.port}/v1/"], resolver)
        blocked(tool, url.format(p=server.port))
        assert resolver.calls == [] and server.requests == []

    def test_path_prefix_allows(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}/v1/"])
        assert tool(f"http://fetch.test:{server.port}/v1/ok")["status"] == 200
        assert tool(f"http://fetch.test:{server.port}/v1")["status"] == 200

    def test_host_is_case_insensitive(self, server: Server) -> None:
        tool = make([f"FETCH.test:{server.port}"])
        assert tool(f"http://Fetch.TEST:{server.port}/ok")["status"] == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/../admin",
            "/v1/%2e%2e/admin",
            "/v1/./ok",
            "/v1%2fok",
            "/v1/%5c..",
            "/v1/%zz",
        ],
    )
    def test_dot_segments_and_encoded_separators(
        self, server: Server, path: str
    ) -> None:
        tool = make([f"fetch.test:{server.port}/v1/"])
        blocked(tool, f"http://fetch.test:{server.port}{path}")
        assert server.requests == []

    @pytest.mark.parametrize("path", PREFIX_BYPASSES)
    def test_path_parameter_and_double_decoding_bypasses(
        self, server: Server, path: str
    ) -> None:
        tool = make([f"fetch.test:{server.port}/v1/"])
        blocked(tool, f"http://fetch.test:{server.port}{path}")
        assert server.requests == []

    @pytest.mark.parametrize("path", PREFIX_BYPASSES)
    def test_bypasses_refused_in_a_redirect(self, server: Server, path: str) -> None:
        from urllib.parse import quote

        tool = make([f"fetch.test:{server.port}/"])
        loc = quote(f"http://fetch.test:{server.port}{path}", safe="")
        blocked(tool, f"http://fetch.test:{server.port}/to?loc={loc}")
        blocked(tool, f"http://fetch.test:{server.port}/to?loc={quote(path, safe='')}")
        assert (
            len(server.requests) == 2
        )  # only the two redirecting responses were fetched

    def test_semicolon_in_the_query_is_fine(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}/v1/"])
        assert tool(f"http://fetch.test:{server.port}/v1/ok?a=1;b=2")["status"] == 200

    @pytest.mark.parametrize(
        "entry",
        [
            "*.example.com",
            "user@example.com",
            "example.com.",
            "",
            "https://example.com/?q=1",
        ],
    )
    def test_bad_entries(self, entry: str) -> None:
        with pytest.raises((ValueError, TypeError)):
            http_fetch([entry])

    def test_allow_must_be_a_list(self) -> None:
        with pytest.raises(TypeError):
            http_fetch("example.com")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            http_fetch([])


# ---------------------------------------------------------------------------
# URL tricks
# ---------------------------------------------------------------------------


class TestUrlTricks:
    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@fetch.test:{p}/ok",
            "http://user@fetch.test:{p}/ok",
            "http://fetch.test:{p}@evil.test/ok",
            "http://@fetch.test:{p}/ok",
        ],
    )
    def test_userinfo_refused(self, server: Server, url: str) -> None:
        tool = make([f"fetch.test:{server.port}"])
        assert "credentials" in blocked(tool, url.format(p=server.port))
        assert server.requests == []

    @pytest.mark.parametrize(
        "host",
        [
            "2130706433",
            "0177.0.0.1",
            "0x7f.0.0.1",
            "0x7f000001",
            "0x7f.1",
            "127.1",
            "127.0.1",
            "017700000001",
            "127.0.0.01",
            "1.2.3.4.5",
            "evil.0x10",
        ],
    )
    def test_non_canonical_ipv4_refused(self, server: Server, host: str) -> None:
        resolver = Resolver(LOCAL)
        tool = make([f"fetch.test:{server.port}"], resolver)
        assert "canonical" in blocked(tool, f"http://{host}:{server.port}/ok")
        assert resolver.calls == [] and server.requests == []
        with pytest.raises(ValueError, match="canonical"):
            http_fetch([host])

    @pytest.mark.parametrize(
        "url",
        [
            "http://fetch.test:{p}/ok\r\nX-Injected: 1",
            "http://fetch.test:{p}/ok\nX-Injected: 1",
            "http://fetch.test:{p}/ok HTTP/1.1\r\nX-Injected: 1\r\n\r\n",
            "http://fetch.test\r\nX-Injected: 1:{p}/ok",
            "http://fetch.test:{p}/o\tk",
            "http://fetch.test:{p}/ok?a=b\r\nc",
            "http://fetch.test:{p}/\x00",
            "http://fetch.test:{p}/café",
            "http://fetch.test:{p}\\@evil.test/",
        ],
    )
    def test_header_injection_refused(self, server: Server, url: str) -> None:
        tool = make([f"fetch.test:{server.port}"])
        blocked(tool, url.format(p=server.port))
        assert server.requests == []

    def test_percent_encoded_crlf_stays_encoded(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        tool(f"http://fetch.test:{server.port}/ok%0D%0AX-Injected:%201")
        (path, headers) = server.requests[0]
        assert path == "/ok%0D%0AX-Injected:%201"
        assert "X-Injected" not in headers

    def test_guest_cannot_pass_headers_or_options(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        with pytest.raises(TypeError):
            tool(f"http://fetch.test:{server.port}/ok", {"headers": {"X-A": "1"}})  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            tool(url=f"http://fetch.test:{server.port}/ok", method="POST")  # type: ignore[call-arg]
        blocked(tool, {"url": "x"})  # type: ignore[arg-type]
        assert server.requests == []

    def test_url_length_cap(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], max_url_length=100)
        assert "too long" in blocked(
            tool, f"http://fetch.test:{server.port}/" + "a" * 100
        )

    def test_zone_id_and_bad_ipv6(self) -> None:
        tool = make(["[fe80::1]"])
        assert "zone" in blocked(tool, "http://[fe80::1%25en0]/")
        blocked(tool, "http://[fe80::1/")
        blocked(tool, "http://[not:an:ip]/")

    def test_bad_ports(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        for port in ("", "0", "65536", "+80", "8o", "123456"):
            blocked(tool, f"http://fetch.test:{port}/ok")

    def test_metadata_names_refused(self) -> None:
        for name in ("metadata.google.internal", "metadata", "instance-data"):
            with pytest.raises(ValueError, match="metadata"):
                http_fetch([name])


# ---------------------------------------------------------------------------
# addresses
# ---------------------------------------------------------------------------

REFUSED = [
    "127.0.0.1",
    "127.255.0.9",
    "0.0.0.0",  # noqa: S104
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.1",
    "100.64.0.1",  # CGNAT
    "100.100.100.200",  # a cloud's metadata address
    "169.254.169.254",  # link-local metadata
    "169.254.170.2",
    "192.0.2.1",
    "198.18.0.1",
    "224.0.0.1",
    "239.255.255.250",
    "255.255.255.255",
    "::",
    "::1",
    "::127.0.0.1",  # IPv4-compatible
    "::ffff:127.0.0.1",  # v4-mapped loopback
    "::ffff:169.254.169.254",
    "::ffff:10.0.0.1",
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::1",
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "2002:a9fe:a9fe::1",
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo, client 127.0.0.1
    "fe80::1",
    "fe80::1%en0",
    "fc00::1",
    "fd00:ec2::254",  # a cloud's metadata address
    "ff02::1",
    "2001:db8::1",
    "fec0::1",
    "not-an-ip",
    "0177.0.0.1",
    "2130706433",
]
ALLOWED = [
    "93.184.215.14",
    "8.8.8.8",
    "2606:4700:4700::1111",
    "::ffff:8.8.8.8",
    "64:ff9b::808:808",
]


class TestAddressPolicy:
    @pytest.mark.parametrize("address", REFUSED)
    def test_refused(self, address: str) -> None:
        assert _address_refused(address)

    @pytest.mark.parametrize(
        "address", [a for a in REFUSED if a not in ("127.0.0.1", "127.255.0.9", "::1")]
    )
    def test_refused_even_with_the_test_switch(self, address: str) -> None:
        assert _address_refused(address, loopback=True)

    @pytest.mark.parametrize("address", ALLOWED)
    def test_allowed(self, address: str) -> None:
        assert not _address_refused(address)

    def test_non_string_answers_refused(self) -> None:
        assert _address_refused(2130706433)
        assert _address_refused(None)


class TestResolution:
    @pytest.mark.parametrize(
        "answers",
        [
            ["169.254.169.254"],
            ["10.0.0.1"],
            ["127.0.0.1", "10.0.0.1"],  # any bad answer refuses the whole host
            ["fd00:ec2::254"],
            ["::ffff:169.254.169.254"],
            ["0177.0.0.1"],
        ],
    )
    def test_bad_answers_refused(self, server: Server, answers: list[str]) -> None:
        tool = make([f"fetch.test:{server.port}"], Resolver({"fetch.test": answers}))
        message = blocked(tool, f"http://fetch.test:{server.port}/ok")
        assert "resolves to an address" in message
        for a in answers:
            assert a not in message  # the guest never learns the internal address
        assert server.requests == []

    def test_ipv6_loopback_and_mapped_literals(self, server: Server) -> None:
        public = http_fetch(["[::1]", "[::ffff:127.0.0.1]"], schemes=["http"])
        blocked(public, "http://[::1]/")
        blocked(public, "http://[::ffff:127.0.0.1]/")
        switched = make(["[::ffff:169.254.169.254]", "169.254.169.254"])
        blocked(switched, "http://[::ffff:169.254.169.254]/")
        blocked(switched, "http://169.254.169.254/latest/meta-data/")

    def test_dns_rebinding_connects_to_the_vetted_address(
        self, server: Server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Rebinding(Resolver):
            def __call__(self, host: str, port: int) -> list[str]:
                super().__call__(host, port)
                return ["127.0.0.1"] if len(self.calls) == 1 else ["169.254.169.254"]

        lookups: list[object] = []
        real = socket.getaddrinfo

        def spy(host: Any, *args: Any, **kwargs: Any) -> Any:
            lookups.append(host)
            return real(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", spy)
        resolver = Rebinding(LOCAL)
        tool = make([f"fetch.test:{server.port}"], resolver)
        assert tool(f"http://fetch.test:{server.port}/ok")["status"] == 200
        assert len(resolver.calls) == 1
        # The only system lookup is the numeric one `create_connection` makes for the vetted IP;
        # nothing resolves `fetch.test` a second time.
        assert lookups == ["127.0.0.1"]
        assert server.requests[0][1]["Host"] == f"fetch.test:{server.port}"
        # And the next call sees the new answer, and is refused.
        blocked(tool, f"http://fetch.test:{server.port}/ok")

    def test_unresolvable(self, server: Server) -> None:
        tool = make([f"nowhere.test:{server.port}"])
        with pytest.raises(HttpFetchFailed, match="could not be resolved"):
            tool(f"http://nowhere.test:{server.port}/ok")

    def test_empty_answer(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], Resolver({"fetch.test": []}))
        with pytest.raises(HttpFetchFailed):
            tool(f"http://fetch.test:{server.port}/ok")


# ---------------------------------------------------------------------------
# redirects
# ---------------------------------------------------------------------------


def to(server: Server, loc: str, status: int = 302) -> str:
    from urllib.parse import quote

    return (
        f"http://fetch.test:{server.port}/to?status={status}&loc={quote(loc, safe='')}"
    )


class TestRedirects:
    def test_same_origin_followed(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        out = tool(f"http://fetch.test:{server.port}/redirect/2")
        assert (out["status"], out["body"]) == (200, "done")
        assert out["url"] == f"http://fetch.test:{server.port}/redirect/0"

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_every_redirect_status_is_checked(
        self, server: Server, status: int
    ) -> None:
        tool = make([f"fetch.test:{server.port}", f"other.test:{server.port}"])
        blocked(tool, to(server, f"http://other.test:{server.port}/ok", status))

    def test_different_host_refused_by_default(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}", f"other.test:{server.port}"])
        assert "different host" in blocked(
            tool, to(server, f"http://other.test:{server.port}/ok")
        )
        assert len(server.requests) == 1

    def test_different_allowed_host_with_allow_list_mode(self, server: Server) -> None:
        tool = make(
            [f"fetch.test:{server.port}", f"other.test:{server.port}"],
            redirects="allow-list",
            headers={"Authorization": "Bearer k"},
        )
        out = tool(to(server, f"http://other.test:{server.port}/ok"))
        assert out["status"] == 200
        first, second = (h for _, h in server.requests)
        assert first["Authorization"] == "Bearer k"
        assert (
            "Authorization" not in second
        )  # host credentials stay with the first origin
        assert second["Host"] == f"other.test:{server.port}"

    def test_not_allow_listed_host_refused(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], redirects="allow-list")
        assert "allow-list" in blocked(tool, to(server, "http://evil.test/"))

    def test_redirect_to_loopback_refused(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], redirects="allow-list")
        for loc in (
            f"http://127.0.0.1:{server.port}/ok",
            f"http://[::1]:{server.port}/ok",
            f"http://2130706433:{server.port}/ok",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost/",
        ):
            blocked(tool, to(server, loc))
        assert len(server.requests) == 5

    def test_redirect_to_allowed_host_with_bad_address_refused(
        self, server: Server
    ) -> None:
        resolver = Resolver({**LOCAL, "inside.test": ["10.0.0.7"]})
        tool = make(
            [f"fetch.test:{server.port}", "inside.test"],
            resolver,
            redirects="allow-list",
        )
        assert "resolves to an address" in blocked(
            tool, to(server, "http://inside.test/")
        )

    def test_five_hop_cap(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        assert tool(f"http://fetch.test:{server.port}/redirect/5")["body"] == "done"
        server.requests.clear()
        assert "too many redirects" in blocked(
            tool, f"http://fetch.test:{server.port}/redirect/6"
        )
        assert len(server.requests) == 6  # the sixth redirect is never followed

    def test_max_redirects_zero(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], max_redirects=0)
        blocked(tool, f"http://fetch.test:{server.port}/redirect/1")

    def test_never_returns_the_redirect(self, server: Server) -> None:
        tool = make(
            [f"fetch.test:{server.port}"],
            redirects="never",
            response_headers=["location"],
        )
        out = tool(f"http://fetch.test:{server.port}/redirect/1")
        assert (out["status"], out["headers"]["location"]) == (302, "/redirect/0")

    @pytest.mark.parametrize(
        "loc",
        ["//evil.test/", "\\\\evil.test/", "http://user@fetch.test/"],
    )
    def test_hostile_location(self, server: Server, loc: str) -> None:
        tool = make([f"fetch.test:{server.port}"], redirects="allow-list")
        with pytest.raises(HttpFetchError):
            tool(to(server, loc))
        assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# size and time
# ---------------------------------------------------------------------------


class TestLimits:
    def test_body_capped_without_content_length(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], max_response_bytes=1000)
        out = tool(f"http://fetch.test:{server.port}/big")
        assert (len(out["body"]), out["truncated"]) == (1000, True)

    def test_content_length_claiming_huge(self, server: Server) -> None:
        tool = make(
            [f"fetch.test:{server.port}"], max_response_bytes=100, response="bytes"
        )
        out = tool(f"http://fetch.test:{server.port}/liar?cl=1000000000&n=2000")
        assert (out["body"], out["truncated"]) == (b"y" * 100, True)

    def test_content_length_longer_than_body(self, server: Server) -> None:
        tool = make(
            [f"fetch.test:{server.port}"], max_response_bytes=10_000, response="bytes"
        )
        out = tool(f"http://fetch.test:{server.port}/liar?cl=5000&n=2000")
        assert (out["body"], out["truncated"]) == (b"y" * 2000, True)

    def test_content_length_shorter_than_body(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], response="bytes")
        out = tool(f"http://fetch.test:{server.port}/liar?cl=10&n=5000")
        assert (out["body"], out["truncated"]) == (b"y" * 10, False)

    def test_timeout_waiting_for_response(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], timeout=0.5)
        start = time.monotonic()
        with pytest.raises(HttpFetchTimeout):
            tool(f"http://fetch.test:{server.port}/slow?s=3")
        assert time.monotonic() - start < 2.0

    def test_timeout_is_a_deadline_not_per_read(self, server: Server) -> None:
        # One byte every 50 ms never trips a per-read socket timeout; the deadline still holds.
        tool = make([f"fetch.test:{server.port}"], timeout=0.5)
        start = time.monotonic()
        with pytest.raises(HttpFetchTimeout):
            tool(f"http://fetch.test:{server.port}/drip")
        assert time.monotonic() - start < 2.0

    def test_timeout_in_resolver(self, server: Server) -> None:
        class Slow(Resolver):
            def __call__(self, host: str, port: int) -> list[str]:
                time.sleep(2)
                return ["127.0.0.1"]

        tool = make([f"fetch.test:{server.port}"], Slow(), timeout=0.3)
        start = time.monotonic()
        with pytest.raises(HttpFetchTimeout):
            tool(f"http://fetch.test:{server.port}/ok")
        assert time.monotonic() - start < 1.5

    def test_connection_refused(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        tool = make([f"fetch.test:{port}"])
        with pytest.raises(HttpFetchFailed, match="connection failed"):
            tool(f"http://fetch.test:{port}/ok")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"timeout": 0},
            {"max_response_bytes": -1},
            {"max_redirects": -1},
            {"redirects": "always"},
            {"response": "json"},
            {"headers": {"Host": "evil"}},
            {"headers": {"Transfer-Encoding": "chunked"}},
            {"headers": {"X-A": "1\r\nX-B: 2"}},
            {"headers": {"Bad Name": "1"}},
        ],
    )
    def test_bad_options(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises((ValueError, TypeError)):
            http_fetch(["fetch.test"], **kwargs)


# ---------------------------------------------------------------------------
# TLS: SNI and certificate checks use the hostname while the socket goes to the vetted IP
# ---------------------------------------------------------------------------


@pytest.fixture
def tls_server() -> Iterator[Server]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT)
    s = Server(context)
    yield s
    s.stop()


def client_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(CERT))
    # The test certificate is its own CA; strict mode (3.13+) wants extensions it does not need.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


class TestTls:
    def test_https_to_vetted_ip_with_hostname(self, tls_server: Server) -> None:
        tool = make([f"fetch.test:{tls_server.port}"], ssl_context=client_context())
        out = tool(f"https://fetch.test:{tls_server.port}/ok")
        assert (out["status"], out["body"]) == (200, "hello")

    def test_certificate_must_match_the_hostname(self, tls_server: Server) -> None:
        tool = make([f"other.test:{tls_server.port}"], ssl_context=client_context())
        with pytest.raises(HttpFetchFailed, match="certificate"):
            tool(f"https://other.test:{tls_server.port}/ok")

    def test_untrusted_certificate(self, tls_server: Server) -> None:
        tool = make([f"fetch.test:{tls_server.port}"])  # system trust store
        with pytest.raises(HttpFetchFailed, match="certificate"):
            tool(f"https://fetch.test:{tls_server.port}/ok")

    def test_no_downgrade_on_redirect(self, tls_server: Server, server: Server) -> None:
        tool = make(
            [f"fetch.test:{tls_server.port}", f"fetch.test:{server.port}"],
            ssl_context=client_context(),
            redirects="allow-list",
        )
        from urllib.parse import quote

        loc = quote(f"http://fetch.test:{server.port}/ok", safe="")
        assert "https to http" in blocked(
            tool, f"https://fetch.test:{tls_server.port}/to?loc={loc}"
        )
        assert server.requests == []


# ---------------------------------------------------------------------------
# async form and exports (ToolBridge / AgentSandbox: test_http_fetch_agent.py)
# ---------------------------------------------------------------------------


class TestAsTool:
    async def test_aio_does_not_block_the_loop(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.ensure_future(tick())
        out = await tool.aio(f"http://fetch.test:{server.port}/slow?s=0.5")
        ticker.cancel()
        assert out["body"] == "late"
        assert ticks > 10

    def test_module_exports(self) -> None:
        import pydeno.tools

        hf = importlib.import_module("pydeno.tools.http_fetch")
        assert set(pydeno.tools.__all__) == set(hf.__all__)
        for name in hf.__all__:
            assert getattr(pydeno, name) is getattr(hf, name)
