from __future__ import annotations

import ipaddress
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dea_capture.fetch import FetchError, HttpFetcher, TooLarge, follow
from dea_capture.guard import ForbiddenFetch, GuardedFetcher, IPAddress, Response

from .conftest import fake_resolver

LOCAL = [ipaddress.ip_address("127.0.0.1")]


class _Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None, str | None]] = []  # noqa: RUF012

    def do_GET(self) -> None:
        self.seen.append((self.path, self.headers.get("Host"), self.headers.get("User-Agent")))
        if self.path == "/big":
            body = b"x" * 5000
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/img.png")
            self.end_headers()
            return
        else:
            body = b"\x89PNG fake"
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[int]:
    _Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_connects_to_the_pinned_address_not_the_name(server: int) -> None:
    """The name never resolves; the request still reaches the pinned address
    and carries the original host."""
    f = HttpFetcher(user_agent="test-agent", _https=False)
    resp = f.get(f"http://pinned.invalid:{server}/img.png?a=1", addresses=LOCAL)
    assert (resp.status, resp.body) == (200, b"\x89PNG fake")
    assert resp.headers["content-type"] == "image/png"
    assert _Handler.seen == [("/img.png?a=1", f"pinned.invalid:{server}", "test-agent")]


def test_does_not_follow_redirects_itself(server: int) -> None:
    resp = HttpFetcher(_https=False).get(f"http://x.invalid:{server}/redirect", addresses=LOCAL)
    assert resp.status == 302
    assert resp.headers["location"] == "/img.png"
    assert len(_Handler.seen) == 1


def test_size_cap(server: int) -> None:
    with pytest.raises(TooLarge):
        HttpFetcher(max_bytes=1000, _https=False).get(
            f"http://x.invalid:{server}/big", addresses=LOCAL
        )


def test_unreachable_is_retryable() -> None:
    with pytest.raises(FetchError):
        HttpFetcher(connect_timeout=1, _https=False).get(
            "http://x.invalid:9/", addresses=[ipaddress.ip_address("127.0.0.1")]
        )


@dataclass
class Scripted:
    """Answers from a table; anything else is a test failure."""

    table: Mapping[str, Response]
    requested: list[str] = field(default_factory=list)

    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
        self.requested.append(url)
        if url not in self.table:
            pytest.fail(f"unexpected request {url}")
        return self.table[url]


def _redirect(url: str, to: str) -> Response:
    return Response(url, 302, {"location": to}, b"")


def _ok(url: str) -> Response:
    return Response(url, 200, {"content-type": "image/png"}, b"png")


def test_follow_checks_every_hop() -> None:
    a, b = "https://cdn.example.com/a.png", "https://cdn.example.com/b.png"
    inner = Scripted({a: _redirect(a, "/b.png"), b: _ok(b)})
    got = follow(GuardedFetcher(inner, resolve=fake_resolver), a, "embed")
    assert got.chain == (a, b)
    assert got.response.body == b"png"


def test_embed_redirect_into_private_network_is_refused() -> None:
    a = "https://cdn.example.com/a.png"
    inner = Scripted({a: _redirect(a, "http://intranet.example.com/x.png")})
    with pytest.raises(ForbiddenFetch):
        follow(GuardedFetcher(inner, resolve=fake_resolver), a, "embed")
    assert inner.requested == [a]


def test_link_redirect_onto_unsubscribe_is_refused() -> None:
    a = "https://click.example.com/abc"
    inner = Scripted({a: _redirect(a, "https://example.org/unsubscribe?e=1")})
    with pytest.raises(ForbiddenFetch):
        follow(GuardedFetcher(inner, resolve=fake_resolver), a, "link")
    assert inner.requested == [a]


def test_redirect_loop_and_limit() -> None:
    a, b = "https://x.example.com/a", "https://x.example.com/b"
    inner = Scripted({a: _redirect(a, b), b: _redirect(b, a)})
    with pytest.raises(FetchError, match="loop"):
        follow(GuardedFetcher(inner, resolve=fake_resolver), a, "embed")
    chain = {f"https://x.example.com/{i}": _redirect("", f"/{i + 1}") for i in range(5)}
    with pytest.raises(FetchError, match="more than 3"):
        follow(
            GuardedFetcher(Scripted(chain), resolve=fake_resolver),
            "https://x.example.com/0", "embed", max_hops=3,
        )  # fmt: skip
