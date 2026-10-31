"""Follow a tracking redirect the way a reader's click would, to find where it
lands.

Only status lines and headers are read: the fetcher passed in should be an
``HttpFetcher(max_bytes=0, truncate=True)``. Every hop goes through the guard
as a ``redirect-hop``, so a tracker that lands on an unsubscribe or action
page is refused before that request is made.

A hop that carries an auth parameter is never requested: it would log the
reader in. The chain stops there, and the landing page is its canonical URL,
which the capture stage requests logged out.

A chain that ends on a tracker (one that answered 429, 403 or an
interstitial page instead of redirecting) has failed: the tracker's URL
carries the reader's token, and is never a canonical URL.

A chain that has left the trackers, but whose destination doesn't answer
plain HTTP (it times out, as sites behind bot protection do with anything
that isn't a browser; its TLS fails; or it loops through a cookie check),
is handed to the browser: the outcome is ok, ``browser`` is set, and the
canonical URL is the destination as far as it's known. capture-page loads it
in a real browser, which follows the rest of the way, and falls back to the
Wayback Machine if the site turns the browser away too.

The chain holds the original, tokenized hrefs, so a ``Resolution`` belongs in
the issue's private files only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin

from .classify import canonicalize, has_auth_param, tracking_vendor
from .fetch import REDIRECTS, FetchError, HostPacer
from .guard import ForbiddenFetch, GuardedFetcher

MAX_HOPS = 10

Outcome = Literal["ok", "refused", "failed"]


@dataclass
class Resolution:
    outcome: Outcome
    # every URL requested or reached, starting with the href
    chain: list[str] = field(default_factory=list)
    # where the click lands: the last URL of the chain, when ok
    final: str | None = None
    canonical: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    # the chain stopped before an auth-parameter hop instead of requesting it
    stopped_at_auth: bool = False
    error: str | None = None
    # the destination didn't answer plain HTTP; left to the browser (`error` says why)
    browser: bool = False


def _to_browser(chain: list[str], at: str, error: str) -> Resolution | None:
    """Hand ``at``, a destination past the trackers, to the browser; None if
    the chain hasn't left the trackers (or ``at`` would log the reader in)."""
    if at == chain[0] or tracking_vendor(at) or has_auth_param(at):
        return None
    return Resolution("ok", chain, at, canonicalize(at), error=error, browser=True)


def resolve(
    href: str, fetcher: GuardedFetcher, pacer: HostPacer, *, max_hops: int = MAX_HOPS
) -> Resolution:
    chain = [href]
    for hop in range(max_hops + 1):
        url = chain[-1]
        if hop and has_auth_param(url):
            return Resolution(
                "ok", chain, url, canonicalize(url), stopped_at_auth=True,
            )  # fmt: skip
        pacer.wait(url)
        try:
            resp = fetcher.get(url, "link" if hop == 0 else "redirect-hop")
        except ForbiddenFetch as exc:
            return Resolution("refused", chain, error=str(exc))
        except FetchError as exc:
            return _to_browser(chain, url, str(exc)) or Resolution("failed", chain, error=str(exc))
        location = resp.headers.get("location")
        if (resp.status not in REDIRECTS or not location) and tracking_vendor(url):
            return Resolution(
                "failed", chain, http_status=resp.status,
                error=f"tracker answered HTTP {resp.status} without redirecting",
            )  # fmt: skip
        if resp.status not in REDIRECTS or not location:
            return Resolution(
                "ok", chain, url, canonicalize(url), resp.status,
                resp.headers.get("content-type"),
            )  # fmt: skip
        nxt = urljoin(url, location.strip())
        if nxt in chain:
            # the page as the tracker sent the reader there, before the loop
            landing = next((u for u in chain[1:] if not tracking_vendor(u)), url)
            return _to_browser(chain, landing, "redirect loop") or Resolution(
                "failed", chain, error="redirect loop"
            )
        chain.append(nxt)
    return Resolution("failed", chain, error=f"more than {max_hops} redirects")
