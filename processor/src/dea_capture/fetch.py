"""Real HTTP, behind the guard.

``HttpFetcher`` is the ``Fetcher`` that ``GuardedFetcher`` wraps in
production. It connects to the address the guard checked, never resolving the
name again, and sends the original host name for TLS (SNI and certificate
checks) and in the ``Host`` header. It never follows redirects itself:
``follow`` does that, sending every hop back through the guard.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import urllib3

from . import __version__
from .classify import normalized_host
from .guard import FetchError, ForbiddenFetch, GuardedFetcher, IPAddress, Purpose, Response

DEFAULT_USER_AGENT = (
    f"dea-capture/{__version__} (newsletter archive; "
    "+https://github.com/biosafetylvl5/distributed-email-archiving)"
)
REDIRECTS = frozenset({301, 302, 303, 307, 308})


class TooLarge(Exception):
    """The body is over the size budget. Deferred, not failed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"body over {limit} bytes")
        self.limit = limit


@dataclass
class HttpFetcher:
    user_agent: str = DEFAULT_USER_AGENT
    connect_timeout: float = 10.0
    # for the whole response, not per read
    total_timeout: float = 60.0
    max_bytes: int = 50 * 1024 * 1024
    # cut bodies at max_bytes instead of raising TooLarge; with max_bytes=0
    # only the status and headers are read (redirect resolution)
    truncate: bool = False
    # never set outside tests: lets a test server on 127.0.0.1 present no TLS
    _https: bool = field(default=True, repr=False)

    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
        parts = urlsplit(url)
        host = normalized_host(url)
        https = parts.scheme.lower() == "https"
        port = parts.port or (443 if https else 80)
        default_port = port == (443 if https else 80)
        host_header = host if default_port else f"{host}:{port}"
        if ":" in host:
            host_header = f"[{host}]" if default_port else f"[{host}]:{port}"
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {"Host": host_header, "User-Agent": self.user_agent, "Accept": "*/*"}
        deadline = time.monotonic() + self.total_timeout
        errors: list[str] = []
        for addr in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("response took too long")
            conn: urllib3.connection.HTTPConnection
            timeout = min(self.connect_timeout, remaining)
            if https and self._https:
                conn = urllib3.connection.HTTPSConnection(
                    str(addr), port, timeout=timeout,
                    server_hostname=host, assert_hostname=host, cert_reqs="CERT_REQUIRED",
                )  # fmt: skip
            else:
                conn = urllib3.connection.HTTPConnection(str(addr), port, timeout=timeout)
            timer: threading.Timer | None = None
            resp: urllib3.HTTPResponse | None = None
            try:
                conn.connect()
                assert conn.sock is not None
                conn.timeout = max(0.001, deadline - time.monotonic())
                conn.sock.settimeout(conn.timeout)
                # Interrupt blocked header/body reads too, even when bytes keep arriving.
                timer = threading.Timer(
                    max(0, deadline - time.monotonic()), _interrupt, args=(conn.sock,)
                )
                timer.daemon = True
                timer.start()
                conn.request("GET", target, headers=headers, preload_content=False)
                resp = conn.getresponse()
                body = self._read(resp, deadline)
                if time.monotonic() >= deadline:
                    raise FetchError("response took too long")
                return Response(
                    url=url, status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()}, body=body,
                )  # fmt: skip
            except (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError) as exc:
                errors.append(f"{addr}: {type(exc).__name__}")
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
                if resp is not None:
                    resp.close()
                conn.close()
        raise FetchError("; ".join(errors) or "no addresses")

    def _read(self, resp: urllib3.BaseHTTPResponse, deadline: float) -> bytes:
        if self.truncate and self.max_bytes == 0:
            return b""
        declared = resp.headers.get("content-length", "")
        if not self.truncate and declared.isdigit() and int(declared) > self.max_bytes:
            raise TooLarge(self.max_bytes)
        chunks: list[bytes] = []
        size = 0
        try:
            for chunk in resp.stream(64 * 1024):
                size += len(chunk)
                if size > self.max_bytes:
                    if self.truncate:
                        chunks.append(chunk[: len(chunk) - (size - self.max_bytes)])
                        break
                    raise TooLarge(self.max_bytes)
                if time.monotonic() > deadline:
                    raise FetchError("response took too long")
                chunks.append(chunk)
        except urllib3.exceptions.HTTPError as exc:
            raise FetchError(f"reading body: {type(exc).__name__}") from exc
        return b"".join(chunks)


def _interrupt(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


@dataclass(frozen=True)
class Fetched:
    response: Response
    # every URL requested, first to last
    chain: tuple[str, ...]


def follow(fetcher: GuardedFetcher, url: str, purpose: Purpose, *, max_hops: int = 8) -> Fetched:
    """Request ``url`` and follow its redirects, each hop through the guard.

    Embeds stay embeds all the way down, as in a mail client. A link's later
    hops are checked as ``redirect-hop``, so a tracker that lands on an
    unsubscribe page is refused before the request.
    """
    chain = [url]
    hop_purpose: Purpose = purpose
    for _ in range(max_hops + 1):
        resp = fetcher.get(chain[-1], hop_purpose)
        location = resp.headers.get("location")
        if resp.status not in REDIRECTS or not location:
            return Fetched(resp, tuple(chain))
        nxt = urljoin(chain[-1], location.strip())
        if nxt in chain:
            raise FetchError("redirect loop")
        chain.append(nxt)
        if purpose != "embed":
            hop_purpose = "redirect-hop"
    raise FetchError(f"more than {max_hops} redirects")


class HostPacer:
    """At most one request per host per ``interval`` seconds, in this process."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = normalized_host(url)
        last = self._last.get(host)
        now = time.monotonic()
        if last is not None and now - last < self.interval:
            time.sleep(self.interval - (now - last))
        self._last[host] = time.monotonic()


__all__ = [
    "DEFAULT_USER_AGENT",
    "FetchError",
    "Fetched",
    "ForbiddenFetch",
    "HostPacer",
    "HttpFetcher",
    "TooLarge",
    "follow",
]
