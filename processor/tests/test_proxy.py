from __future__ import annotations

import asyncio
import ipaddress
import socket
import socketserver
import ssl
import threading
from collections.abc import Iterator

import pytest

from dea_capture import proxy as proxy_mod
from dea_capture.guard import ForbiddenFetch, IPAddress
from dea_capture.proxy import GuardProxy

from .conftest import fake_resolver

LOOPBACK = ipaddress.ip_address("127.0.0.1")


class _Upstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    received: bytes = b""


class _Echo(socketserver.BaseRequestHandler):
    """Answers a plain HTTP request with the request it got; echoes anything else."""

    def handle(self) -> None:
        data = self.request.recv(65536)
        server: _Upstream = self.server  # type: ignore[assignment]
        server.received = data
        if data.startswith(b"GET "):
            body = b"hello"
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n" + body)
            return
        while data:
            self.request.sendall(data)
            data = self.request.recv(65536)


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    server = _Upstream(("127.0.0.1", 0), _Echo)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


def _allow_loopback(monkeypatch: pytest.MonkeyPatch, refuse: set[str] = frozenset()) -> None:  # type: ignore[assignment]
    """Let the proxy reach the local upstream server as if it were public."""

    def check(url: str, resolve: object = None, extra_blocked: object = ()) -> list[IPAddress]:
        host = url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
        if host in refuse:
            raise ForbiddenFetch("private")
        return [LOOPBACK]

    monkeypatch.setattr(proxy_mod, "check_destination", check)


def _ask(port: int, head: bytes, then: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(head)
        if then:
            reply = s.recv(65536)
            s.sendall(then)
            return reply + b"|" + s.recv(65536)
        chunks = []
        while data := s.recv(65536):
            chunks.append(data)
        return b"".join(chunks)


@pytest.mark.parametrize(
    "head",
    [
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
        b"CONNECT localhost:443 HTTP/1.1\r\n\r\n",
        b"CONNECT intranet.example.com:443 HTTP/1.1\r\n\r\n",  # resolves to a private address
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        b"GET http://127.0.0.1:6379/ HTTP/1.1\r\n\r\n",
        b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\n\r\n",
    ],
)
def test_refuses_local_destinations(head: bytes) -> None:
    def resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1"] if host == "localhost" else fake_resolver(host, port)

    with GuardProxy(resolve=resolve, verify=None) as p:
        reply = _ask(p.start(), head)
    assert reply.startswith(b"HTTP/1.1 403")
    assert sum(p.stats.refused.values()) == 1
    assert p.stats.tunnels == p.stats.forwarded == 0


@pytest.mark.parametrize(
    "head",
    [
        b"CONNECT example.org:22 HTTP/1.1\r\n\r\n",
        b"CONNECT example.org:8443 HTTP/1.1\r\n\r\n",
        b"GET http://example.org:8080/ HTTP/1.1\r\n\r\n",
        b"GET ftp://example.org/file HTTP/1.1\r\n\r\n",
        b"GET /relative HTTP/1.1\r\n\r\n",
    ],
)
def test_refuses_other_ports_and_schemes(head: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_loopback(monkeypatch)
    with GuardProxy(verify=None) as p:
        assert _ask(p.start(), head).startswith(b"HTTP/1.1 403")


def test_forwards_plain_http_in_origin_form(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_loopback(monkeypatch)
    with GuardProxy(verify=None) as p:
        p.http_port = upstream.server_address[1]
        reply = _ask(
            p.start(),
            b"GET http://news.example.org/a?b=1 HTTP/1.1\r\nHost: news.example.org\r\n"
            b"Proxy-Connection: keep-alive\r\nProxy-Authorization: x\r\nAccept: */*\r\n\r\n",
        )
    assert reply.endswith(b"hello")
    sent = upstream.received.split(b"\r\n")
    assert sent[0] == b"GET /a?b=1 HTTP/1.1"
    assert b"Host: news.example.org" in sent
    assert b"Accept: */*" in sent
    assert b"Connection: close" in sent
    assert not any(line.lower().startswith(b"proxy-") for line in sent)
    assert p.stats.forwarded == 1


def test_tunnels_after_checking_the_certificate(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_loopback(monkeypatch)
    checked: list[tuple[str, IPAddress]] = []

    async def verify(host: str, addr: IPAddress) -> None:
        checked.append((host, addr))

    with GuardProxy(verify=verify) as p:
        p.https_port = upstream.server_address[1]
        port = p.start()
        head = b"CONNECT news.example.org:443 HTTP/1.1\r\n\r\n"
        first = _ask(port, head, then=b"tls bytes")
        _ask(port, head, then=b"again")
    assert first == b"HTTP/1.1 200 Connection Established\r\n\r\n|tls bytes"
    # once per host and address
    assert checked == [("news.example.org", LOOPBACK)]
    assert p.stats.tunnels == 2


def test_no_tunnel_when_the_certificate_check_fails(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_loopback(monkeypatch)

    async def verify(host: str, addr: IPAddress) -> None:
        raise OSError("certificate verify failed")

    with GuardProxy(verify=verify) as p:
        p.https_port = upstream.server_address[1]
        reply = _ask(p.start(), b"CONNECT news.example.org:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 502")
    assert p.stats.tunnels == 0
    assert p.stats.failed == 1


def test_unreachable_upstream_is_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_loopback(monkeypatch)
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    with GuardProxy(verify=None, connect_timeout=2) as p:
        p.http_port = closed
        reply = _ask(p.start(), b"GET http://news.example.org/ HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 502")
    assert p.stats.to_json()["failed"] == 1


# ── missing intermediate certificates ───────────────────────────────────────


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    length = bytes([n]) if n < 0x80 else bytes([0x82]) + n.to_bytes(2)
    return bytes([tag]) + length + body


def _name(cn: bytes) -> bytes:
    return _der(0x30, _der(0x31, _der(0x30, _der(0x0C, cn))))


def _cert(issuer: bytes, subject: bytes, extra: bytes = b"") -> bytes:
    tbs = (
        _der(0xA0, _der(0x02, b"\x02")) + _der(0x02, b"\x01") + _der(0x30, b"")
        + _name(issuer) + _der(0x30, b"") + _name(subject) + extra
    )  # fmt: skip
    return _der(0x30, _der(0x30, tbs) + _der(0x30, b"") + _der(0x03, b"\x00"))


def _aia(url: bytes) -> bytes:
    return _der(0x30, proxy_mod._CA_ISSUERS + _der(0x86, url))


LEAF = _cert(b"Example CA", b"news.example.org", _aia(b"http://ca.example.com/int.crt"))
INTERMEDIATE = _cert(b"Example Root", b"Example CA")
ROOT = _cert(b"Example Root", b"Example Root")


def test_reads_the_issuer_url_from_a_certificate() -> None:
    assert proxy_mod.ca_issuers(LEAF) == ["http://ca.example.com/int.crt"]
    assert proxy_mod.ca_issuers(_cert(b"a", b"b", _aia(b"ldap://ca.example.com/x"))) == []
    assert proxy_mod.ca_issuers(b"\x30\x00") == []


def test_tells_roots_from_intermediates() -> None:
    assert proxy_mod.self_issued(ROOT)
    assert not proxy_mod.self_issued(INTERMEDIATE)
    assert proxy_mod.self_issued(b"\x30\x05junk")  # unreadable: never trusted


class _Ctx:
    def __init__(self) -> None:
        self.check_hostname, self.verify_mode = True, ssl.CERT_REQUIRED
        self.verify_flags = ssl.VERIFY_X509_PARTIAL_CHAIN
        self.cadata: bytes | None = None

    def load_verify_locations(self, cadata: bytes) -> None:
        self.cadata = cadata


def _verify(
    monkeypatch: pytest.MonkeyPatch, code: int, fetched: bytes | None,
    asked: list[str] | None = None,
) -> tuple[list[_Ctx], list[str]]:  # fmt: skip
    contexts: list[_Ctx] = []
    asked = [] if asked is None else asked

    async def handshake(host: str, addr: IPAddress, port: int, ctx: _Ctx, t: float) -> bytes:
        contexts.append(ctx)
        if ctx.verify_mode == ssl.CERT_NONE:
            return LEAF
        if ctx.cadata == INTERMEDIATE:
            return LEAF
        exc = ssl.SSLCertVerificationError("certificate verify failed")
        exc.verify_code = code
        raise exc

    def issuers(url: str) -> bytes | None:
        asked.append(url)
        return fetched

    monkeypatch.setattr(proxy_mod, "_handshake", handshake)
    asyncio.run(
        proxy_mod.verify_tls(
            "news.example.org",
            LOOPBACK,
            issuers=issuers,
            context=_Ctx,  # type: ignore[arg-type]
        )
    )
    return contexts, asked


def test_fetches_a_missing_intermediate(monkeypatch: pytest.MonkeyPatch) -> None:
    contexts, asked = _verify(monkeypatch, 20, INTERMEDIATE)
    assert asked == ["http://ca.example.com/int.crt"]
    last = contexts[-1]
    assert last.cadata == INTERMEDIATE and last.verify_mode == ssl.CERT_REQUIRED
    # it links the chain to a trusted root; it can't be one
    assert not last.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def test_never_takes_a_root_from_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ssl.SSLCertVerificationError):
        _verify(monkeypatch, 20, ROOT)
    with pytest.raises(ssl.SSLCertVerificationError):
        _verify(monkeypatch, 20, None)


def test_other_certificate_errors_fetch_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    with pytest.raises(ssl.SSLCertVerificationError):
        _verify(monkeypatch, 10, INTERMEDIATE, asked)  # expired
    assert asked == []
