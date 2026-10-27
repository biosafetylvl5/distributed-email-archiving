"""The checks every outbound request goes through.

``GuardedFetcher`` is the only way the rest of the package makes a request.
It enforces two independent rules, whatever the classifier decided:

1. **Never act as the subscriber.** A link, or any redirect hop, that
   classifies as unsubscribe, action, non-web or an in-message anchor is
   refused, and so is one that still carries an auth parameter. So is any
   URL in ``refused_urls``: every href the classifier skipped in this
   message, plus its ``List-Unsubscribe`` URIs. Some links are recognisable
   only by their anchor text, which a URL-only check can't see.
2. **Never reach the local network.** The host must resolve only to public
   addresses: no private, loopback, link-local, CGNAT, ULA, multicast,
   reserved or metadata addresses, and nothing in ``extra_blocked`` (the
   cluster's pod and service ranges). The checked addresses are handed to the
   inner fetcher, which must connect to one of them instead of resolving
   again, so DNS rebinding can't swap in a private address afterwards.

Embeds are only subject to rule 2. Loading them, tracking pixels included, is
what a reader's mail client does when it opens the message.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

from .classify import NEVER_FETCH, classify, has_auth_param

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Sequence[str]]
Purpose = Literal["embed", "link", "redirect-hop"]

# not all of these are covered by is_global on every Python version
ALWAYS_BLOCKED: tuple[IPNetwork, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
        "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
        "64:ff9b::/96", "2002::/16",
    )
)  # fmt: skip


class ForbiddenFetch(Exception):
    """A request the guard refuses. Never retried."""


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


class Fetcher(Protocol):
    """Makes one request, without following redirects, connecting only to one
    of ``addresses``."""

    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response: ...


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _unmapped(addr: IPAddress) -> IPAddress:
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr.sixtofour is not None:
            return addr.sixtofour
    return addr


def is_public(addr: IPAddress, extra_blocked: Iterable[IPNetwork] = ()) -> bool:
    addr = _unmapped(addr)
    if not addr.is_global or addr.is_multicast:
        return False
    return not any(addr in net for net in (*ALWAYS_BLOCKED, *extra_blocked) if
                   addr.version == net.version)  # fmt: skip


def check_destination(
    url: str, resolve: Resolver = system_resolver, extra_blocked: Iterable[IPNetwork] = ()
) -> list[IPAddress]:
    """The addresses ``url`` may be fetched from, or ForbiddenFetch."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ForbiddenFetch(f"scheme '{parts.scheme}' is not http or https")
    if parts.username is not None or parts.password is not None:
        raise ForbiddenFetch("credentials in URL")
    host = parts.hostname
    if not host:
        raise ForbiddenFetch("no host")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ForbiddenFetch(f"bad port: {exc}") from exc
    blocked = tuple(extra_blocked)
    try:
        raw = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            raw = [ipaddress.ip_address(a.split("%")[0]) for a in resolve(host, port)]
        except (OSError, UnicodeError) as exc:
            raise ForbiddenFetch(f"cannot resolve {host}: {exc}") from exc
    if not raw:
        raise ForbiddenFetch(f"{host} has no addresses")
    # one private address is enough to refuse: the connection could pick it
    for addr in raw:
        if not is_public(addr, blocked):
            raise ForbiddenFetch(f"{host} resolves to non-public address {addr}")
    return raw


def check_link(url: str, unsubscribe_urls: Iterable[str] = ()) -> None:
    """ForbiddenFetch unless ``url`` is safe to request as a link or hop."""
    c = classify(url, "", unsubscribe_urls)
    if c.link_class in NEVER_FETCH:
        raise ForbiddenFetch(f"{c.link_class}: {c.reason}")
    if has_auth_param(url):
        raise ForbiddenFetch("auth parameter present; request the canonical URL instead")


@dataclass
class GuardedFetcher:
    inner: Fetcher
    resolve: Resolver = system_resolver
    extra_blocked: Sequence[IPNetwork] = ()
    refused_urls: frozenset[str] = field(default_factory=frozenset)

    def get(self, url: str, purpose: Purpose) -> Response:
        if purpose != "embed":
            if url.strip() in self.refused_urls:
                raise ForbiddenFetch("skipped by the classifier for this message")
            check_link(url, self.refused_urls)
        addresses = check_destination(url, self.resolve, self.extra_blocked)
        return self.inner.get(url, addresses=addresses)
