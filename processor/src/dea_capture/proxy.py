"""A forward proxy on localhost that puts the page crawler's browser behind the guard.

The browser runs whatever a page tells it to, so its traffic gets the same
destination check as every other request (``guard.check_destination``): each
host must resolve only to public addresses, and the proxy connects to one of
the addresses it checked, never resolving again. The browser is started with
``--proxy-bypass-list=<-loopback>``, so even ``localhost`` goes through here
and is refused: the crawler's redis, the browser's debugging port and
courier's metrics all listen on loopback inside the pod.

Two kinds of request are served:

- ``CONNECT host:443``, a TLS tunnel. Port 443 only. The browser can't check
  certificates through it (browsertrix starts it with
  ``--ignore-certificate-errors`` whenever a proxy is set), so the proxy does:
  before opening a tunnel it completes its own verified TLS handshake with the
  same address, once per host and address. A server that leaves its
  intermediate certificate out gets it fetched from the address its own
  certificate gives (``caIssuers``), as browsers do; the chain must still end
  at a trusted root.
- Plain ``http://host/...`` requests on port 80, forwarded with
  ``Connection: close``.

Everything else is refused with 403. Nothing is logged here: ``ProxyStats``
keeps counts, and the hosts it refused, for the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import NoReturn
from urllib.parse import urlsplit

from .fetch import FetchError, HttpFetcher, TooLarge
from .guard import (
    ForbiddenFetch,
    IPAddress,
    IPNetwork,
    Resolver,
    check_destination,
    system_resolver,
)

# checks that the server at an address holds a valid certificate for a host
Verifier = Callable[[str, IPAddress], Awaitable[None]]
# a CA certificate's URL -> the certificate (DER), or None
IssuerFetcher = Callable[[str], bytes | None]

MAX_HEAD = 64 * 1024
_HOP_HEADERS = frozenset(
    {b"connection", b"keep-alive", b"proxy-connection", b"proxy-authorization"}
)


@dataclass
class ProxyStats:
    tunnels: int = 0
    forwarded: int = 0
    refused: Counter[str] = field(default_factory=Counter)  # by host
    failed: int = 0  # couldn't connect, or the certificate check failed

    def to_json(self) -> dict[str, int]:
        return {
            "tunnels": self.tunnels, "forwarded": self.forwarded,
            "refused": sum(self.refused.values()), "refused_hosts": len(self.refused),
            "failed": self.failed,
        }  # fmt: skip


# X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY: the chain is missing a link
_MISSING_ISSUER = 20
# id-ad-caIssuers (1.3.6.1.5.5.7.48.2), which a URI ([6]) follows
_CA_ISSUERS = bytes.fromhex("06082b06010505073002")
ISSUER_MAX_BYTES = 64 * 1024


def ca_issuers(der: bytes) -> list[str]:
    """The http(s) URLs a certificate gives for its issuer's certificate."""
    urls: list[str] = []
    i = 0
    while (i := der.find(_CA_ISSUERS, i)) >= 0:
        i += len(_CA_ISSUERS)
        if i + 2 > len(der) or der[i] != 0x86 or der[i + 1] & 0x80:
            continue
        url = der[i + 2 : i + 2 + der[i + 1]].decode("ascii", "replace")
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _tlv(der: bytes, i: int) -> tuple[int, int, int]:
    """The DER element at ``i``: (tag, start of its contents, its end)."""
    tag, n, j = der[i], der[i + 1], i + 2
    if n & 0x80:
        k = n & 0x7F
        n, j = int.from_bytes(der[j : j + k]), j + k
    if j + n > len(der):
        raise ValueError("truncated")
    return tag, j, j + n


def self_issued(der: bytes) -> bool:
    """Whether a certificate names itself as its issuer (a root, which must
    never be taken from a server); unreadable ones count as self-issued."""
    try:
        _, i, _ = _tlv(der, 0)  # Certificate
        _, i, _ = _tlv(der, i)  # tbsCertificate
        tag, _, end = _tlv(der, i)
        i = end if tag == 0xA0 else i  # version
        i = _tlv(der, i)[2]  # serialNumber
        i = _tlv(der, i)[2]  # signature
        issuer_end = _tlv(der, i)[2]
        issuer = der[i:issuer_end]
        i = _tlv(der, issuer_end)[2]  # validity
        subject = der[i : _tlv(der, i)[2]]
    except (IndexError, ValueError):
        return True
    return issuer == subject


class GuardedIssuers:
    """Fetches CA certificates through the guard, remembering each URL's."""

    def __init__(self, resolve: Resolver, extra_blocked: Sequence[IPNetwork]) -> None:
        self.resolve, self.extra_blocked = resolve, extra_blocked
        self.http = HttpFetcher(connect_timeout=5, total_timeout=10, max_bytes=ISSUER_MAX_BYTES)
        self.seen: dict[str, bytes | None] = {}

    def __call__(self, url: str) -> bytes | None:
        if url not in self.seen:
            self.seen[url] = self._fetch(url)
        return self.seen[url]

    def _fetch(self, url: str) -> bytes | None:
        try:
            addresses = check_destination(url, self.resolve, self.extra_blocked)
            resp = self.http.get(url, addresses=addresses)
        except (ForbiddenFetch, FetchError, TooLarge, OSError, ValueError):
            return None
        body = resp.body
        if resp.status != 200 or not body:
            return None
        if body.startswith(b"-----BEGIN CERTIFICATE-----"):
            try:
                return ssl.PEM_cert_to_DER_cert(body.decode("ascii"))
            except ValueError:
                return None
        return body if body[0] == 0x30 else None


async def _handshake(
    host: str, addr: IPAddress, port: int, ctx: ssl.SSLContext, timeout: float
) -> bytes | None:
    """A TLS handshake; returns the server's certificate (DER)."""
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(str(addr), port, ssl=ctx, server_hostname=host), timeout
    )
    tls = writer.get_extra_info("ssl_object")
    der: bytes | None = tls.getpeercert(binary_form=True) if tls else None
    writer.close()
    with contextlib.suppress(OSError, ssl.SSLError):
        await writer.wait_closed()
    return der


async def verify_tls(
    host: str, addr: IPAddress, timeout: float = 10.0, *, port: int = 443,
    issuers: IssuerFetcher | None = None,
    context: Callable[[], ssl.SSLContext] = ssl.create_default_context,
) -> None:  # fmt: skip
    """Raises unless the server at ``addr`` has a valid certificate for
    ``host``. With ``issuers``, a missing intermediate certificate is fetched
    from where the server's certificate says (never a root: the chain must
    still end at one ``context`` trusts)."""
    try:
        await _handshake(host, addr, port, context(), timeout)
        return
    except ssl.SSLCertVerificationError as exc:
        if issuers is None or exc.verify_code != _MISSING_ISSUER:
            raise
        error = exc
    unchecked = context()
    unchecked.check_hostname, unchecked.verify_mode = False, ssl.CERT_NONE
    leaf = await _handshake(host, addr, port, unchecked, timeout)
    loop = asyncio.get_running_loop()
    for url in ca_issuers(leaf or b"")[:2]:
        der = await loop.run_in_executor(None, issuers, url)
        if not der or self_issued(der):
            continue
        ctx = context()
        # the fetched certificate may only link the chain, never end it
        ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.load_verify_locations(cadata=der)
        await _handshake(host, addr, port, ctx, timeout)
        return
    raise error


class _Refused(Exception):
    pass


class GuardProxy:
    """Serve on ``127.0.0.1`` from a thread of its own; ``start()`` returns the port."""

    def __init__(
        self,
        resolve: Resolver = system_resolver,
        extra_blocked: Iterable[IPNetwork] = (),
        verify: Verifier | None = verify_tls,
        connect_timeout: float = 10.0,
        idle_timeout: float = 120.0,
    ) -> None:
        self.resolve = resolve
        self.extra_blocked = tuple(extra_blocked)
        if verify is verify_tls:
            issuers = GuardedIssuers(resolve, self.extra_blocked)

            async def verify_with_issuers(host: str, addr: IPAddress) -> None:
                await verify_tls(host, addr, issuers=issuers)

            verify = verify_with_issuers
        self.verify = verify
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        # where the proxy connects; only tests change these
        self.http_port, self.https_port = 80, 443
        self.stats = ProxyStats()
        self._verified: set[tuple[str, IPAddress]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle

    def start(self) -> int:
        ready = threading.Event()
        port: list[int] = []

        def serve() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._server = loop.run_until_complete(
                asyncio.start_server(self._handle, "127.0.0.1", 0)
            )
            port.append(self._server.sockets[0].getsockname()[1])
            ready.set()
            loop.run_forever()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

        self._thread = threading.Thread(target=serve, name="guard-proxy", daemon=True)
        self._thread.start()
        ready.wait()
        return port[0]

    def stop(self) -> None:
        loop, server = self._loop, self._server
        if loop is None or server is None:
            return

        async def shutdown() -> None:
            server.close()
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()

        asyncio.run_coroutine_threadsafe(shutdown(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(10)

    def __enter__(self) -> GuardProxy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- requests

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.idle_timeout)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                return
            if len(head) > MAX_HEAD:
                return
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            if len(parts) != 3:
                await self._reply(writer, 400, "bad request line")
                return
            method, target = parts[0].decode("latin-1"), parts[1].decode("latin-1")
            if method == "CONNECT":
                await self._connect(reader, writer, target)
            else:
                await self._forward(reader, writer, method, target, lines[1:])
        except _Refused:
            pass
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await writer.wait_closed()

    async def _reply(self, writer: asyncio.StreamWriter, status: int, why: str) -> None:
        body = why.encode()
        writer.write(
            f"HTTP/1.1 {status} {why}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(OSError):
            await writer.drain()

    async def _refuse(self, writer: asyncio.StreamWriter, host: str, why: str) -> NoReturn:
        self.stats.refused[host] += 1
        await self._reply(writer, 403, why)
        raise _Refused

    async def _check(self, writer: asyncio.StreamWriter, url: str, host: str) -> list[IPAddress]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, check_destination, url, self.resolve, self.extra_blocked
            )
        except ForbiddenFetch:
            await self._refuse(writer, host, "refused by the guard")

    async def _open(
        self, writer: asyncio.StreamWriter, addresses: Sequence[IPAddress], port: int
    ) -> tuple[IPAddress, asyncio.StreamReader, asyncio.StreamWriter] | None:
        for addr in addresses:
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(str(addr), port), self.connect_timeout
                )
            except (OSError, TimeoutError):
                continue
            return addr, r, w
        self.stats.failed += 1
        await self._reply(writer, 502, "cannot connect")
        return None

    async def _connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        host, _, port = target.rpartition(":")
        host = host.strip("[]").lower()
        if port != "443" or not host:
            await self._refuse(writer, host or "?", "CONNECT to port 443 only")
        addresses = await self._check(writer, f"https://{target}/", host)
        opened = await self._open(writer, addresses, self.https_port)
        if opened is None:
            return
        addr, up_r, up_w = opened
        if self.verify is not None and (host, addr) not in self._verified:
            try:
                await self.verify(host, addr)
            except (OSError, ssl.SSLError, TimeoutError):
                up_w.close()
                self.stats.failed += 1
                await self._reply(writer, 502, "certificate check failed")
                return
            self._verified.add((host, addr))
        self.stats.tunnels += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe(reader, writer, up_r, up_w)

    async def _forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, method: str,
        target: str, header_lines: list[bytes],
    ) -> None:  # fmt: skip
        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        try:
            port = parts.port or 80
        except ValueError:
            port = 0
        if parts.scheme != "http" or not host or port != 80:
            await self._refuse(writer, host or "?", "plain http on port 80 only")
        addresses = await self._check(writer, target, host)
        opened = await self._open(writer, addresses, self.http_port)
        if opened is None:
            return
        _, up_r, up_w = opened
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        out = [f"{method} {path} HTTP/1.1".encode()]
        for line in header_lines:
            if not line:
                continue
            name = line.split(b":", 1)[0].strip().lower()
            if name not in _HOP_HEADERS:
                out.append(line)
        out.append(b"Connection: close")
        up_w.write(b"\r\n".join(out) + b"\r\n\r\n")
        self.stats.forwarded += 1
        await self._pipe(reader, writer, up_r, up_w)

    async def _pipe(
        self, a_r: asyncio.StreamReader, a_w: asyncio.StreamWriter,
        b_r: asyncio.StreamReader, b_w: asyncio.StreamWriter,
    ) -> None:  # fmt: skip
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await asyncio.wait_for(src.read(65536), self.idle_timeout):
                    dst.write(data)
                    await dst.drain()
            except (OSError, TimeoutError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    if dst.can_write_eof():
                        dst.write_eof()

        tasks = [asyncio.create_task(copy(a_r, b_w)), asyncio.create_task(copy(b_r, a_w))]
        try:
            # the server's side ending ends the exchange
            await tasks[1]
        finally:
            tasks[0].cancel()
            b_w.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await tasks[0]
