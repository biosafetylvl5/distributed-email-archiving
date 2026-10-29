"""The second half of the ``issue`` stage: find where each link lands and
queue it for capture.

``capture`` links already have a canonical URL. ``resolve`` links are
tracking redirects: each distinct href is followed once, headers only (see
``resolve``), and its landing page is queued. Every canonical URL is queued
once per issue, and once across issues (see ``capture.queue``).

The results go to ``issues/<sha>/captures.json``: one record per distinct
href, with its chain. Private, like ``links.json``: the hrefs carry
subscriber tokens. Its presence means the links are done, so a re-run skips
them, and its counts are added to ``issue-status/<sha>.json``.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .capture import Kind, guess_kind, queue, url_hash
from .classify import Classified, Decision, tracking_vendor
from .fetch import HostPacer
from .guard import GuardedFetcher
from .inventory import Inventory
from .resolve import resolve
from .store import Store, put_json

# 1: first version
LINKS_VERSION = 1


def captures_key(sha: str) -> str:
    return f"issues/{sha}/captures.json"


@dataclass
class LinkRecord:
    href: str
    decision: str
    # ok, refused or failed: whether the landing page is known
    outcome: str
    canonical: str | None = None
    h: str | None = None
    kind: Kind | None = None
    # what queueing found: new, or the URL's status from an earlier issue
    queued: str | None = None
    chain: list[str] = field(default_factory=list)
    http_status: int | None = None
    stopped_at_auth: bool = False
    error: str | None = None


def link_counts(doc: dict[str, Any] | None) -> dict[str, int]:
    """The counts ``issue-status`` carries for the link stage."""
    if not doc:
        return {}
    recs = doc.get("links", [])
    outcomes = Counter(str(r.get("outcome")) for r in recs)
    return {
        "links_resolved": sum(
            1 for r in recs if r.get("decision") == "resolve" and r.get("canonical")
        ),
        "links_unresolved": outcomes["failed"],
        "links_refused": outcomes["refused"],
        "captures_queued": len({r["h"] for r in recs if r.get("h")}),
        "captures_new": len({r["h"] for r in recs if r.get("queued") == "new"}),
    }  # fmt: skip


def process_links(
    sha: str,
    inv: Inventory,
    store: Store,
    resolver: GuardedFetcher,
    *,
    now: datetime | None = None,
    pacer: HostPacer | None = None,
    subscription: str | None = None,
) -> dict[str, Any]:
    """Resolve and queue one issue's links; returns the ``captures.json`` doc.
    ``subscription``: the message's ``user/tag``, which sets capture size
    limits (see ``capture.size_limit``).

    ``resolver`` must read headers only; its ``refused_urls`` must come from
    this message's inventory.
    """
    now = now or datetime.now(UTC)
    pacer = pacer or HostPacer()
    records: list[LinkRecord] = []
    seen: set[str] = set()
    queued: dict[str, str] = {}
    for link in inv.to_fetch():
        if link.href in seen:
            continue
        seen.add(link.href)
        records.append(_link_record(link, store, resolver, pacer, now, queued, subscription))
    doc = {
        "sha256": sha,
        "subscription": subscription,
        "links_version": LINKS_VERSION,
        "completed_at": _stamp(now),
        "links": [asdict(r) for r in records],
    }
    put_json(store, captures_key(sha), doc)
    return doc


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _link_record(
    link: Classified, store: Store, resolver: GuardedFetcher, pacer: HostPacer, now: datetime,
    queued: dict[str, str], subscription: str | None = None,
) -> LinkRecord:  # fmt: skip
    """Resolve (if need be) and queue one link. ``queued``: what queueing
    found per URL hash in this issue so far, so each is queued once."""
    rec = LinkRecord(link.href, str(link.decision), "ok")
    if link.decision == Decision.CAPTURE:
        rec.canonical = link.canonical
        content_type = None
    else:
        r = resolve(link.fetch_url or link.href, resolver, pacer)
        rec.outcome, rec.chain, rec.error = r.outcome, r.chain[1:], r.error
        rec.canonical, rec.http_status = r.canonical, r.http_status
        rec.stopped_at_auth = r.stopped_at_auth
        content_type = r.content_type
    if rec.canonical and tracking_vendor(rec.canonical):
        # a tracker's URL holds the reader's token: never shared, never requested again
        rec.outcome, rec.canonical = "failed", None
        rec.error = rec.error or "resolved to a tracker URL"
    if rec.canonical:
        rec.h = url_hash(rec.canonical)
        rec.kind = guess_kind(rec.canonical, content_type)
        if rec.h not in queued:
            queued[rec.h] = queue(store, rec.canonical, rec.kind, now, subscription)
        rec.queued = queued[rec.h]
    return rec


@dataclass
class Relinked:
    tried: int = 0
    resolved: int = 0
    rate_limited: bool = False  # stopped at a 429
    out_of_time: bool = False


def retry_unresolved(
    sha: str, inv: Inventory, store: Store, resolver: GuardedFetcher, *, now: datetime,
    pacer: HostPacer, deadline: float | None = None,
) -> tuple[dict[str, Any] | None, Relinked]:  # fmt: skip
    """Follow again the redirects of an issue's links that couldn't be
    followed, and queue where they land; returns the updated ``captures.json``
    doc (None if the issue has none, or nothing changed) and what happened.

    Stops at the first 429, since the rest would get one too, and before
    ``deadline`` (a ``time.monotonic()`` value). ``pacer`` sets the pace:
    a tracker's links are all on one host.
    """
    got = Relinked()
    doc = load_links(store, sha)
    if doc is None:
        return None, got
    by_href = {link.href: link for link in inv.to_fetch()}
    queued = {str(r["h"]): str(r.get("queued")) for r in doc["links"] if r.get("h")}
    changed = False
    for i, old in enumerate(doc["links"]):
        if old.get("outcome") != "failed" or old.get("decision") != str(Decision.RESOLVE):
            continue
        link = by_href.get(old.get("href", ""))
        if link is None:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            got.out_of_time = True
            break
        got.tried += 1
        rec = _link_record(link, store, resolver, pacer, now, queued, doc.get("subscription"))
        if rec.http_status == 429 and rec.outcome == "failed":
            got.rate_limited = True
            break
        doc["links"][i], changed = asdict(rec), True
        got.resolved += rec.outcome == "ok"
    if not changed:
        return None, got
    doc["relinked_at"] = _stamp(now)
    put_json(store, captures_key(sha), doc)
    return doc, got


def load_links(store: Store, sha: str) -> dict[str, Any] | None:
    data = store.get(captures_key(sha))
    return json.loads(data) if data else None
