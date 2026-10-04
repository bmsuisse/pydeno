"""`http_fetch`: an allow-listed, SSRF-safe HTTP GET tool for guest code.

pydeno has no permission model, so there is no ``--allow-net``: a guest reaches the network only
through a host function the host bound. This module is that host function, written once with the
server-side request forgery cases handled, so a host does not have to get them right by hand.

The pipeline every request (and every redirect hop) goes through:

1. **Strict parse.** ASCII only outside the host, no whitespace or control characters (so no CRLF
   header injection), an absolute URL, a scheme from ``schemes`` (``https`` only by default), no
   userinfo (``user:pass@``), no IPv6 zone id, a port in range, no dot segments and no encoded
   ``/`` or ``\\`` in the path.
2. **Allow-list.** Exact, lowercase, IDNA host match (no suffix or wildcard match), the entry's
   port (or the scheme's default), and a path prefix that ends on a segment boundary. A numeric
   host is accepted only in canonical dotted-decimal or bracketed IPv6 form, so ``0177.0.0.1``,
   ``0x7f.1``, ``127.1`` and ``2130706433`` never reach a resolver. Cloud metadata names are
   refused outright.
3. **Resolve once, vet every answer.** All A/AAAA answers are checked; if **any** is loopback,
   private, link-local, CGNAT, unspecified, multicast, reserved, unique-local, or embeds such an
   IPv4 address (``::ffff:``, NAT64, 6to4, Teredo), the request is refused.
4. **Connect to the vetted address.** The socket goes to the IP that was checked, with the TLS
   SNI, certificate check and ``Host`` header using the hostname, so a DNS answer that changes
   between check and use (rebinding) cannot redirect the connection.
5. **Bounded response.** GET only, no guest headers, ``Accept-Encoding: identity`` (no transparent
   decompression), the body read as bytes up to ``max_response_bytes`` whatever
   ``Content-Length`` claims, one deadline for the whole call including redirects.
6. **Redirects** are never followed automatically: each hop is re-run through steps 1-5, at most
   ``max_redirects`` of them, never from ``https`` to ``http``, and by default only to the same
   origin.

Errors are subclasses of :class:`pydeno.ToolError` with fixed messages written here; they never
contain a resolved address, a response body or the host's fixed headers, so they are shown to the
guest even when the session redacts host errors.

See ``docs/guides/http-fetch.md``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urljoin

from .._tools import ToolError

__all__ = [
    "AsyncHttpFetch",
    "HttpFetch",
    "HttpFetchBlocked",
    "HttpFetchError",
    "HttpFetchFailed",
    "HttpFetchTimeout",
    "http_fetch",
]

Resolver = Callable[[str, int], Iterable[str]]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class HttpFetchError(ToolError):
    """Base class of every `http_fetch` failure the guest can see.

    The message is always one of this module's fixed strings: no resolved address, no response
    data, no host header value. It is therefore marked public, and an `AgentSandbox` that redacts
    host errors still shows it (the guest learns *why* it was refused, which is what a model
    needs to correct its next call).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self._pydeno_public = True


class HttpFetchBlocked(HttpFetchError):
    """The request was refused by policy: the URL, its host, its resolved address or a redirect."""


class HttpFetchTimeout(HttpFetchError):
    """The whole call (resolution, connection, redirects and body) exceeded its ``timeout``."""


class HttpFetchFailed(HttpFetchError):
    """The request was allowed but failed: no such host, connection refused, TLS or HTTP error."""


# ---------------------------------------------------------------------------
# address policy
# ---------------------------------------------------------------------------

# Belt and braces over `ipaddress`'s own `is_global`, whose tables changed between Python versions.
_REFUSED_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT; also some clouds' metadata (100.100.100.200)
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local; cloud metadata 169.254.169.254, 169.254.170.2
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_REFUSED_V6 = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::/96",  # unspecified, loopback, deprecated IPv4-compatible
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",
        "2001::/23",  # IETF protocol assignments, Teredo
        "2001:db8::/32",
        "fc00::/7",  # unique local; cloud metadata fd00:ec2::254
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

# Refused by name whatever the allow-list says: a host that allow-lists one of these by mistake
# has handed the guest its cloud credentials.
_METADATA_NAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


def _ip_refused(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, loopback: bool
) -> bool:
    """`loopback` (tests only) admits plain 127.0.0.0/8 and ::1, never an embedded form."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return True
        if ip.ipv4_mapped is not None:
            return _ip_refused(ip.ipv4_mapped, False)
        if ip in _NAT64:
            return _ip_refused(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF), False)
        embedded: list[ipaddress.IPv4Address] = []
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if any(_ip_refused(e, False) for e in embedded):
            return True
    if loopback and ip.is_loopback:
        return False
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
    ):
        return True
    nets = _REFUSED_V4 if ip.version == 4 else _REFUSED_V6
    return any(ip in net for net in nets)


def _address_refused(address: object, loopback: bool = False) -> bool:
    """True if a resolved address must not be connected to. Anything unparsable is refused."""
    if not isinstance(address, str):
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return _ip_refused(ip, loopback)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443}
# Whitespace, controls (CR, LF, NUL, ...) and characters RFC 3986 never allows unencoded.
_BAD_CHARS = re.compile(r"[\x00-\x20\x7f\\\"<>`{|}^]")
_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://([^/?#]*)([^#]*)(?:#.*)?", re.DOTALL)
_PATH_CHARS = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/%]*")
_QUERY_CHARS = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/%?]*")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(2[Ff]|5[Cc])")
_PATH_PARAMS = re.compile(r";|%3[Bb]")
_AMBIGUOUS_ESCAPE = re.compile(r"%(25|00)")
_DOTS = re.compile(r"\.+")
_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+")


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str  # lowercase ASCII hostname, canonical IPv4, or canonical IPv6 without brackets
    port: int
    explicit_port: bool
    path: str
    query: str
    is_ip: bool

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (
            host if self.port == _DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"
        )

    @property
    def request_target(self) -> str:
        return f"{self.path}?{self.query}" if self.query else self.path

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.netloc}{self.request_target}"


def _parse(url: object, schemes: frozenset[str], max_length: int) -> _Target:
    """Parse `url` strictly or raise `HttpFetchBlocked` with a fixed reason."""
    if not isinstance(url, str):
        raise HttpFetchBlocked("the URL must be a string")
    if len(url) > max_length:
        raise HttpFetchBlocked("the URL is too long")
    if _BAD_CHARS.search(url):
        raise HttpFetchBlocked(
            "the URL contains whitespace or characters that are not allowed"
        )
    found = _URL.fullmatch(url)
    if found is None:
        raise HttpFetchBlocked("not an absolute URL")
    scheme = found.group(1).lower()
    if scheme not in schemes or scheme not in _DEFAULT_PORTS:
        raise HttpFetchBlocked("the URL scheme is not allowed")
    host, port, explicit, is_ip = _authority(found.group(2))
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    rest = found.group(3)
    if not rest.isascii():
        raise HttpFetchBlocked("the URL path must be ASCII (percent-encode it)")
    path, _, query = rest.partition("?")
    path = path or "/"
    if not path.startswith("/") or not _PATH_CHARS.fullmatch(path):
        raise HttpFetchBlocked("the URL path contains characters that are not allowed")
    if not _QUERY_CHARS.fullmatch(query):
        raise HttpFetchBlocked("the URL query contains characters that are not allowed")
    if _BAD_ESCAPE.search(path) or _BAD_ESCAPE.search(query):
        raise HttpFetchBlocked("the URL contains a malformed percent escape")
    _check_path(path)
    return _Target(scheme, host, port, explicit, path, query, is_ip)


def _check_path(path: str) -> None:
    """Refuse a path that two parties could read as two different paths."""
    if _ENCODED_SEPARATOR.search(path):
        raise HttpFetchBlocked("the URL path contains an encoded '/' or '\\'")
    # Backends disagree about the path: some strip `;params` from each segment (`/v1/..;/admin`
    # is `/v1/../admin` to them), some decode twice (`%252e` is `.`), some stop at NUL. The
    # prefix check only holds if every party reads the path the same way, so these are refused.
    if _PATH_PARAMS.search(path):
        raise HttpFetchBlocked(
            "the URL path contains ';' (path parameters are not allowed)"
        )
    if _AMBIGUOUS_ESCAPE.search(path):
        raise HttpFetchBlocked("the URL path contains '%25' or '%00'")
    if any(_DOTS.fullmatch(unquote(seg)) for seg in path.split("/")):
        raise HttpFetchBlocked("the URL path contains '.' or '..' segments")


def _authority(authority: str) -> tuple[str, int | None, bool, bool]:
    if "@" in authority:
        raise HttpFetchBlocked("credentials in the URL are not allowed")
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise HttpFetchBlocked("malformed IPv6 address in the URL")
        literal, after = authority[1:end], authority[end + 1 :]
        if "%" in literal:
            raise HttpFetchBlocked("IPv6 zone identifiers are not allowed")
        try:
            host = str(ipaddress.IPv6Address(literal))
        except ValueError:
            raise HttpFetchBlocked("malformed IPv6 address in the URL") from None
        is_ip = True
    else:
        raw, sep, port_text = authority.partition(":")
        after = sep + port_text
        host = _hostname(raw)
        is_ip = False
        last = host.rsplit(".", 1)[-1]
        if _NUMERIC_LABEL.fullmatch(last):
            # WHATWG parses such a host as an IPv4 address in one of several bases; we accept only
            # the canonical dotted-decimal form, so every party reads the same address.
            try:
                canonical = str(ipaddress.IPv4Address(host))
            except ValueError:
                canonical = None
            if canonical != host:
                raise HttpFetchBlocked(
                    "a numeric host must be a canonical dotted-decimal IPv4 address"
                )
            is_ip = True
    if not after:
        return host, None, False, is_ip
    if not re.fullmatch(r":[0-9]{1,5}", after) or not 0 < int(after[1:]) < 65536:
        raise HttpFetchBlocked("the URL port is not valid")
    return host, int(after[1:]), True, is_ip


def _hostname(raw: str) -> str:
    if not raw:
        raise HttpFetchBlocked("the URL has no host")
    if "%" in raw:
        raise HttpFetchBlocked("percent-encoded hosts are not allowed")
    if not raw.isascii():
        try:
            raw = raw.encode("idna").decode("ascii")
        except UnicodeError:
            raise HttpFetchBlocked("the URL host is not a valid hostname") from None
    host = raw.lower()
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        raise HttpFetchBlocked("the URL host is not a valid hostname")
    if host in _METADATA_NAMES:
        raise HttpFetchBlocked("cloud metadata hosts are never allowed")
    return host


# ---------------------------------------------------------------------------
# allow-list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    scheme: str | None
    host: str
    port: int | None
    prefix: str

    def matches(self, t: _Target) -> bool:
        if self.scheme is not None and self.scheme != t.scheme:
            return False
        if self.host != t.host:
            return False
        if t.port != (self.port or _DEFAULT_PORTS[t.scheme]):
            return False
        if self.prefix == "/":
            return True
        base = self.prefix.rstrip("/")
        return t.path == base or t.path.startswith(base + "/")

    def describe(self, schemes: frozenset[str]) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port else ""
        scheme = self.scheme or (next(iter(schemes)) if len(schemes) == 1 else None)
        return (
            f"{scheme}://{host}{port}{self.prefix}"
            if scheme
            else f"{host}{port}{self.prefix}"
        )


def _entry(spec: object) -> _Entry:
    if not isinstance(spec, str) or not spec:
        raise TypeError("each allow entry must be a non-empty string")
    explicit_scheme = "://" in spec
    url = spec if explicit_scheme else f"https://{spec}"
    try:
        t = _parse(url, frozenset(_DEFAULT_PORTS), 2048)
    except HttpFetchBlocked as exc:
        raise ValueError(f"invalid allow entry {spec!r}: {exc}") from None
    if t.query:
        raise ValueError(f"invalid allow entry {spec!r}: no query string allowed")
    return _Entry(
        t.scheme if explicit_scheme else None,
        t.host,
        t.port if t.explicit_port else None,
        t.path,
    )


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


class _PinnedConnection(http.client.HTTPConnection):
    """An HTTP(S) connection to one vetted IP, speaking to it as `host`."""

    def __init__(
        self,
        host: str,
        port: int,
        ip: str,
        timeout: float,
        context: ssl.SSLContext | None,
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip
        self._context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        if self._context is not None:
            try:
                sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
        self.sock = sock


_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DEFAULT_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-language",
    "etag",
    "last-modified",
    "cache-control",
    "expires",
    "date",
    "retry-after",
)
# Headers the host may not fix: they belong to the transport, and getting them wrong would let a
# single request smuggle a second one.
_RESERVED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "accept-encoding",
        "te",
        "upgrade",
        "trailer",
        "keep-alive",
        "proxy-connection",
    }
)
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*")
_MAX_HEADER_VALUE = 1024

# Threads for name resolution, shared by every `HttpFetch` in the process. The system resolver
# cannot be cancelled: a lookup that outlives its caller's deadline keeps its thread until the OS
# gives up (often 10-30 s), and while all of them are busy, new lookups queue behind them (each
# caller still gets `HttpFetchTimeout` at its own deadline). Set before the first request.
DNS_THREADS = 8

_dns_pool: concurrent.futures.ThreadPoolExecutor | None = None
_dns_pool_lock = threading.Lock()


def _dns_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _dns_pool  # noqa: PLW0603 - one lazily created pool for the process
    with _dns_pool_lock:
        if _dns_pool is None:
            _dns_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=DNS_THREADS, thread_name_prefix="pydeno-http-fetch-dns"
            )
        return _dns_pool


def _system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass
class _Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    truncated: bool
    charset: str | None


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            raise HttpFetchTimeout("the request timed out")
        return left

    def passed(self) -> bool:
        return time.monotonic() >= self.end


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------


class HttpFetch:
    """The sync tool `http_fetch` builds. Call it with one URL; see :func:`http_fetch`.

    Guest code passes only the URL. Every other knob is fixed by the host here, so nothing the
    guest (or the model behind it) writes can widen the policy. Use :attr:`aio` for an async
    version of the same tool.
    """

    def __init__(
        self,
        allow: Iterable[str],
        *,
        schemes: Iterable[str] = ("https",),
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 1024 * 1024,
        max_url_length: int = 2048,
        max_redirects: int = 5,
        redirects: Literal["same-origin", "allow-list", "never"] = "same-origin",
        response: Literal["text", "bytes"] = "text",
        response_headers: Iterable[str] = _DEFAULT_RESPONSE_HEADERS,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
        user_agent: str = "pydeno-http-fetch",
    ) -> None:
        if isinstance(allow, str):
            raise TypeError("allow must be a list of entries, not a single string")
        self._entries = tuple(_entry(spec) for spec in allow)
        if not self._entries:
            raise ValueError("allow must name at least one host")
        if isinstance(schemes, str):
            raise TypeError("schemes must be a list, e.g. ['https']")
        scheme_set = frozenset(s.lower() for s in schemes)
        if not scheme_set or not scheme_set <= set(_DEFAULT_PORTS):
            raise ValueError("schemes must be a non-empty subset of {'https', 'http'}")
        for e in self._entries:
            if e.scheme is not None and e.scheme not in scheme_set:
                raise ValueError(
                    f"allow entry for {e.host!r} uses scheme {e.scheme!r}, "
                    f"which is not in schemes={sorted(scheme_set)!r}"
                )
        self._schemes = scheme_set
        self._headers = _check_headers(headers or {}, user_agent)
        for name, value in (
            ("timeout", timeout),
            ("max_response_bytes", max_response_bytes),
            ("max_url_length", max_url_length),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive number")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int):
            raise TypeError("max_redirects must be an int")
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if redirects not in ("same-origin", "allow-list", "never"):
            raise ValueError("redirects must be 'same-origin', 'allow-list' or 'never'")
        if response not in ("text", "bytes"):
            raise ValueError("response must be 'text' or 'bytes'")
        if resolver is not None and not callable(resolver):
            raise TypeError("resolver must be callable as resolver(host, port)")
        self._timeout = float(timeout)
        self._max_response_bytes = int(max_response_bytes)
        self._max_url_length = int(max_url_length)
        self._max_redirects = max_redirects
        self._redirects = redirects
        self._response = response
        self._response_headers = frozenset(h.lower() for h in response_headers)
        self._resolver: Resolver = resolver or _system_resolver
        self._ssl_context = ssl_context
        self._ssl_lock = threading.Lock()
        # Never set by the public API: only `_allow_loopback_for_tests` turns it on, so the
        # test-suite can talk to a server on 127.0.0.1. Private, link-local and metadata
        # addresses stay refused even then.
        self._allow_loopback = False
        # What `describe_tools()` / `typescript_stubs()` show the model.
        self.__doc__ = _model_doc(self)

    # The tool itself -----------------------------------------------------

    def __call__(self, url: str) -> dict[str, Any]:
        deadline = _Deadline(self._timeout)
        target = self._target(url)
        origin = target.origin
        hops = 0
        while True:
            fixed = (
                self._headers if target.origin == origin else self._transport_headers()
            )
            follow = self._redirects != "never"
            resp, location = self._request(target, deadline, fixed, follow)
            if location is None:
                return self._result(target, resp)
            if hops >= self._max_redirects:
                raise HttpFetchBlocked("too many redirects")
            hops += 1
            target = self._redirect(target, origin, location)

    @property
    def aio(self) -> AsyncHttpFetch:
        """The same tool as an async callable: the blocking I/O runs in a worker thread."""
        return AsyncHttpFetch(self)

    @property
    def allowed(self) -> tuple[str, ...]:
        """The allow-list, normalized, as the model is shown it."""
        return tuple(e.describe(self._schemes) for e in self._entries)

    def __repr__(self) -> str:
        return f"HttpFetch(allow={list(self.allowed)!r})"

    # Pipeline ------------------------------------------------------------

    def _target(self, url: object) -> _Target:
        t = _parse(url, self._schemes, self._max_url_length)
        if not any(e.matches(t) for e in self._entries):
            raise HttpFetchBlocked("the URL is not in the allow-list")
        return t

    def _redirect(
        self, current: _Target, origin: tuple[str, str, int], location: str
    ) -> _Target:
        # `urljoin` silently drops tabs and newlines and strips whitespace; a Location that holds
        # any goes to the parser unjoined, which refuses it.
        if not _BAD_CHARS.search(location) and location.isascii():
            # A relative reference is joined (and its dot segments resolved) by `urljoin`, so
            # check its own path first: `/v1;/../admin` must not become an innocent `/admin`.
            if not _URL.fullmatch(location) and not location.startswith("//"):
                _check_path(re.split(r"[?#]", location, maxsplit=1)[0])
            location = urljoin(current.url, location)
        new = self._target(location)
        if current.scheme == "https" and new.scheme != "https":
            raise HttpFetchBlocked("a redirect from https to http is not allowed")
        if self._redirects == "same-origin" and new.origin != origin:
            raise HttpFetchBlocked("a redirect to a different host is not allowed")
        return new

    def _vet(self, t: _Target, deadline: _Deadline) -> list[str]:
        """Resolve `t.host` once and return the addresses, all of which passed the policy."""
        if t.is_ip:
            answers: list[object] = [t.host]
        else:
            future = _dns_executor().submit(_resolve, self._resolver, t.host, t.port)
            try:
                answers = future.result(timeout=deadline.remaining())
            except concurrent.futures.TimeoutError:
                raise HttpFetchTimeout("the request timed out") from None
            except HttpFetchError:
                raise
            except Exception:  # noqa: BLE001 - a resolver failure is not the guest's business
                raise HttpFetchFailed("the host name could not be resolved") from None
        if not answers:
            raise HttpFetchFailed("the host name could not be resolved")
        if any(_address_refused(a, self._allow_loopback) for a in answers):
            raise HttpFetchBlocked(
                "the host resolves to an address that is not allowed"
            )
        seen: list[str] = []
        for a in answers:
            text = str(ipaddress.ip_address(str(a)))
            if text not in seen:
                seen.append(text)
        return seen

    def _request(
        self,
        t: _Target,
        deadline: _Deadline,
        headers: list[tuple[str, str]],
        follow: bool,
    ) -> tuple[_Response, str | None]:
        addresses = self._vet(t, deadline)
        context = self._context() if t.scheme == "https" else None
        conn: _PinnedConnection | None = None
        last: BaseException | None = None
        for ip in addresses:
            conn = _PinnedConnection(t.host, t.port, ip, deadline.remaining(), context)
            try:
                conn.connect()
                break
            except (OSError, ssl.SSLError) as exc:
                conn.close()
                conn = None
                last = exc
        if conn is None:
            raise _transport_error(last, deadline)
        # A hard stop for the whole exchange: socket timeouts alone restart on every byte, so a
        # server dripping one byte at a time could hold the call far past `timeout`.
        assert conn.sock is not None
        watchdog = threading.Timer(deadline.end - time.monotonic(), _cut, (conn.sock,))
        watchdog.daemon = True
        watchdog.start()
        try:
            conn.putrequest(
                "GET", t.request_target, skip_host=True, skip_accept_encoding=True
            )
            conn.putheader("Host", t.netloc)
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders()
            resp = conn.getresponse()
            location = resp.getheader("Location") if follow else None
            if resp.status in _REDIRECTS and location is not None:
                return _Response(resp.status, [], b"", False, None), location
            body, truncated = self._read(resp, deadline)
            charset = resp.msg.get_content_charset()
            return (
                _Response(
                    resp.status, list(resp.msg.items()), body, truncated, charset
                ),
                None,
            )
        except HttpFetchError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            raise _transport_error(exc, deadline) from None
        finally:
            watchdog.cancel()
            conn.close()

    def _read(
        self, resp: http.client.HTTPResponse, deadline: _Deadline
    ) -> tuple[bytes, bool]:
        """The body as bytes, at most `max_response_bytes`; `Content-Length` is not trusted."""
        cap = self._max_response_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            deadline.remaining()
            try:
                data = resp.read1(min(65536, cap + 1 - total))
            except http.client.IncompleteRead as exc:
                # The server promised more than it sent. Keep what arrived; flag it.
                if deadline.passed():
                    raise HttpFetchTimeout("the request timed out") from None
                if exc.partial:
                    chunks.append(exc.partial[: cap - total])
                return b"".join(chunks), True
            if not data:
                if deadline.passed():  # the watchdog cut the socket: not a clean end
                    raise HttpFetchTimeout("the request timed out")
                # EOF before the promised Content-Length: keep what arrived, flag it.
                return b"".join(chunks), bool(getattr(resp, "length", None))
            total += len(data)
            if total > cap:
                chunks.append(data[: len(data) - (total - cap)])
                return b"".join(chunks), True
            chunks.append(data)

    def _result(self, t: _Target, resp: _Response) -> dict[str, Any]:
        headers: dict[str, str] = {}
        for name, value in resp.headers:
            key = name.lower()
            if key not in self._response_headers:
                continue
            clean = "".join(ch for ch in value if " " <= ch <= "~")[:_MAX_HEADER_VALUE]
            headers[key] = f"{headers[key]}, {clean}" if key in headers else clean
        body: str | bytes = resp.body
        if self._response == "text":
            body = _decode(resp.body, resp.charset)
        return {
            "status": resp.status,
            "headers": headers,
            "body": body,
            "truncated": resp.truncated,
            "url": t.url,
        }

    def _transport_headers(self) -> list[tuple[str, str]]:
        """The headers every request carries; the host's own headers go only to the first origin."""
        return [
            (n, v)
            for n, v in self._headers
            if n.lower() in ("user-agent", "accept-encoding", "connection")
        ]

    def _context(self) -> ssl.SSLContext:
        with self._ssl_lock:
            if self._ssl_context is None:
                self._ssl_context = ssl.create_default_context()
            return self._ssl_context


class AsyncHttpFetch:
    """The async form of an :class:`HttpFetch` (``tool.aio``): awaitable, I/O off the event loop.

    Bind this one into a :class:`pydeno.Runtime` through :class:`pydeno.ToolBridge` so a slow
    request does not block the runtime thread; `AgentSandbox` and `AsyncAgentSandbox` take either
    form (`AsyncAgentSandbox` already runs a sync tool off its loop).
    """

    def __init__(self, tool: HttpFetch) -> None:
        self._tool = tool
        self.__doc__ = tool.__doc__

    async def __call__(self, url: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._tool, url)

    def __repr__(self) -> str:
        return f"AsyncHttpFetch(allow={list(self._tool.allowed)!r})"


def http_fetch(
    allow: Iterable[str],
    *,
    schemes: Iterable[str] = ("https",),
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    max_response_bytes: int = 1024 * 1024,
    max_url_length: int = 2048,
    max_redirects: int = 5,
    redirects: Literal["same-origin", "allow-list", "never"] = "same-origin",
    response: Literal["text", "bytes"] = "text",
    response_headers: Iterable[str] = _DEFAULT_RESPONSE_HEADERS,
    resolver: Resolver | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> HttpFetch:
    """Build an allow-listed, SSRF-safe ``GET`` tool for guest code.

    The guest calls it with one URL and gets back plain data:
    ``{"status": int, "headers": {...}, "body": str | bytes, "truncated": bool, "url": str}``.
    HTTP error statuses (404, 500) are results, not errors. A refused or failed request raises a
    :class:`HttpFetchError` subclass, which the guest sees as an ``Error`` whose ``name`` is
    ``HttpFetchBlocked``, ``HttpFetchTimeout`` or ``HttpFetchFailed``.

    Args:
        allow: Allowed destinations, matched exactly: ``"api.example.com"`` (any path, default
            port), ``"api.example.com:8443"``, ``"api.example.com/v1/"`` (path prefix, on a
            segment boundary), or with a scheme, ``"https://api.example.com/v1/"``. No wildcards,
            no suffix matching: ``example.com`` does not allow ``www.example.com``.
        schemes: URL schemes the guest may use. ``("https",)`` by default; add ``"http"`` only
            deliberately.
        headers: Fixed request headers the host attaches (an API key, say). The guest can neither
            set nor read them. They are sent to every allow-listed host the guest asks for, and
            dropped on a redirect to another origin; give hosts with different credentials
            separate tools.
        timeout: Seconds for the whole call: DNS, connect, TLS, every redirect and the body.
        max_response_bytes: Body bytes kept; the rest is not read and ``truncated`` is set.
        max_url_length: Longest URL accepted (the request-size cap: there is no request body).
        max_redirects: Redirect hops followed, each re-checked from scratch.
        redirects: ``"same-origin"`` (default) follows only to the same scheme, host and port;
            ``"allow-list"`` to any allowed URL; ``"never"`` returns the 3xx response.
        response: ``"text"`` decodes the body (the response charset, else UTF-8, invalid bytes
            replaced); ``"bytes"`` returns it raw (a ``Uint8Array`` in JavaScript).
        response_headers: Response header names (case-insensitive) passed back to the guest.
        resolver: ``resolver(host, port) -> iterable of IP strings``, replacing the system
            resolver. Its answers are vetted exactly like the system's.
        ssl_context: TLS context for ``https`` (a private CA, say). Defaults to
            ``ssl.create_default_context()``. Keep ``check_hostname=True`` and
            ``verify_mode=CERT_REQUIRED``: the connection is still pinned to the vetted IP
            without them, but nothing then proves the server at that IP is the allow-listed
            host, so the DNS-rebinding defence no longer covers *which* server answers.

    Returns:
        An :class:`HttpFetch`: a sync callable; ``.aio`` is the async form.

    Example:
        >>> from pydeno import AgentSandbox, http_fetch
        >>> fetch = http_fetch(["api.example.com/v1/"], headers={"Authorization": "Bearer ..."})
        >>> with AgentSandbox({"fetch_url": fetch}, max_tool_calls=20) as session:  # doctest: +SKIP
        ...     session.run("(await fetch_url('https://api.example.com/v1/items')).status")
    """
    return HttpFetch(
        allow,
        schemes=schemes,
        headers=headers,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        max_url_length=max_url_length,
        max_redirects=max_redirects,
        redirects=redirects,
        response=response,
        response_headers=response_headers,
        resolver=resolver,
        ssl_context=ssl_context,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve(resolver: Resolver, host: str, port: int) -> list[object]:
    return list(resolver(host, port))


def _cut(sock: socket.socket) -> None:
    """Abort a blocked read from another thread: a plain shutdown(2), even under TLS."""
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


def _transport_error(exc: BaseException | None, deadline: _Deadline) -> HttpFetchError:
    if isinstance(exc, TimeoutError) or deadline.passed():
        return HttpFetchTimeout("the request timed out")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return HttpFetchFailed("the server's TLS certificate could not be verified")
    if isinstance(exc, ssl.SSLError):
        return HttpFetchFailed("the TLS handshake failed")
    if isinstance(exc, (http.client.HTTPException, ValueError)):
        return HttpFetchFailed("the server sent an invalid HTTP response")
    return HttpFetchFailed("the connection failed")


def _check_headers(
    headers: Mapping[str, str], user_agent: str
) -> list[tuple[str, str]]:
    if not isinstance(headers, Mapping):
        raise TypeError("headers must be a mapping of name -> value")
    fixed = [
        ("User-Agent", user_agent),
        ("Accept-Encoding", "identity"),
        ("Connection", "close"),
    ]
    for name, value in headers.items():
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise ValueError(f"invalid header name {name!r}")
        if name.lower() in _RESERVED_REQUEST_HEADERS:
            raise ValueError(f"header {name!r} is set by http_fetch itself")
        if not isinstance(value, str) or not _HEADER_VALUE.fullmatch(value):
            raise ValueError(
                f"invalid value for header {name!r}: no control characters"
            )
        if name.lower() == "user-agent":
            fixed[0] = (name, value)
        else:
            fixed.append((name, value))
    return fixed


def _decode(body: bytes, charset: str | None) -> str:
    if charset:
        try:
            return body.decode(charset, "replace")
        except (LookupError, TypeError, ValueError):
            pass
    return body.decode("utf-8", "replace")


def _model_doc(tool: HttpFetch) -> str:
    kind = "text" if tool._response == "text" else "bytes"  # noqa: SLF001
    return (
        "Fetch a URL with HTTP GET and return "
        f"{{status, headers, body ({kind}), truncated, url}}. "
        "Only these URLs are allowed (exact host, path prefix): "
        + ", ".join(tool.allowed)
        + ". Error statuses are returned, not thrown; a refused URL throws HttpFetchBlocked."
    )


def _allow_loopback_for_tests(tool: HttpFetch) -> HttpFetch:
    """Let `tool` connect to 127.0.0.0/8 and ::1. For pydeno's own tests only; not public API.

    Everything else in the policy stays: private, link-local, metadata and the rest are still
    refused, as are the numeric-host forms and the allow-list."""
    tool._allow_loopback = True  # noqa: SLF001
    return tool
