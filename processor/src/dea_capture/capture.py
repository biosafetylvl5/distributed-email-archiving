"""Capture once per canonical URL, retry on a schedule, fall back to Wayback.

Keys, with ``h = sha256(canonical URL)``:

- ``captures/index/<h>.json``: the URL's record: kind, status, every
  attempt, and where the good capture is. Its counts are also set as R2
  metadata for the dashboard. Created with ``If-None-Match: *``, so however
  many issues link a URL, exactly one of them queues it.
- ``captures/due/<kind>/<UTC stamp>-<h>.json``: "capture ``h`` at or after
  this time". The ``s3_due_poller`` monitor for each kind emits keys whose
  time has passed. This is the queue, the retry schedule and crash recovery
  in one: a capture first moves its key to a lease an hour or more ahead,
  so a capture that dies is picked up again when the lease falls due.
- ``captures/<h[0:2]>/<h>/<stamp>/``: one attempt's files. A good capture
  is never written again; a later attempt gets a new stamp.

Only canonical URLs, which have tracking and auth parameters stripped, are
stored here. The original hrefs stay in each issue's private files.

Size limits: each kind has its own cap. A subscription can set a lower one
(the dashboard writes them all to ``LIMITS_KEY``, in bytes by ``user/tag``).
A record lists the subscriptions whose messages linked its URL; the most
permissive of their limits applies, and none at all if any of them has none,
or a message with no known subscription linked it (as with records from
before there were limits). A capture deferred by a limit is queued again when
a subscription with a higher limit, or none, links it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from . import __version__
from .classify import tracking_vendor
from .fetch import FetchError, HostPacer, TooLarge, follow
from .gallery import store_gallery
from .guard import ForbiddenFetch, GuardedFetcher
from .media import MediaOutcome, store_media
from .page import PageOutcome, store_page
from .store import Store, ext_for, put_json

Kind = Literal["page", "direct", "media", "gallery"]
KINDS: tuple[Kind, ...] = ("page", "direct", "media", "gallery")
Status = Literal["queued", "retrying", "ok", "failed", "refused", "deferred"]
FINAL: frozenset[str] = frozenset({"ok", "failed", "refused", "deferred"})

LIMITS_KEY = "config/capture-limits.json"

# after the first attempt fails: retry 1 h, 6 h, 1 d, 7 d and 30 d later
RETRY_AFTER = (
    timedelta(hours=1), timedelta(hours=6), timedelta(days=1), timedelta(days=7),
    timedelta(days=30),
)  # fmt: skip
# A page or file that fails gets the Wayback Machine snapshot closest to when
# it was queued: at once if the site turned us away, else after a second
# failure, since most one-off failures clear on the 1 h retry. Retries go on,
# so a capture of our own can still follow. Other kinds only get one once
# their retries run out.
WAYBACK_KINDS = frozenset({"page", "direct"})
WAYBACK_AT_ONCE = frozenset({401, 403, 429, 451})
WAYBACK_AFTER_FAILURES = 2
# longer than any stage's courier timeout
LEASE = {
    "page": timedelta(hours=1), "direct": timedelta(hours=1), "media": timedelta(hours=6),
    "gallery": timedelta(hours=2),
}  # fmt: skip

_STAMP = "%Y%m%dT%H%M%SZ"
_DUE_RE = re.compile(
    r"^captures/due/(page|direct|media|gallery)/(\d{8}T\d{6}Z)-([0-9a-f]{64})\.json$"
)

MEDIA_HOSTS = frozenset(
    {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com",
        "vimeo.com", "player.vimeo.com", "soundcloud.com", "www.tiktok.com",
    }
)  # fmt: skip
# one post on a site gallery-dl reads without an account: (hosts, path)
GALLERY_POSTS = (
    ({"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com",
      "mobile.twitter.com"}, re.compile(r"^/[^/]+/status/\d+")),
    ({"bsky.app"}, re.compile(r"^/profile/[^/]+/post/[^/]+")),
    ({"www.instagram.com", "instagram.com"}, re.compile(r"^/(?:[^/]+/)?(?:p|reel|tv)/[^/]+")),
    ({"www.facebook.com", "facebook.com", "m.facebook.com"},
     re.compile(r"^/(?:photo(?:\.php)?/?$|media/set/?$|[^/]+/(?:posts|photos)/)")),
    ({"www.flickr.com", "flickr.com"}, re.compile(r"^/photos/[^/]+/\d+")),
    ({"imgur.com"}, re.compile(r"^/(?:a|gallery)/[^/]+")),
)  # fmt: skip
_TUMBLR_POST = re.compile(r"^/post/\d+")


def gallery_post(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith(".tumblr.com") and _TUMBLR_POST.match(parts.path):
        return True
    return any(host in hosts and path.match(parts.path) for hosts, path in GALLERY_POSTS)


_DIRECT_EXT = re.compile(
    r"\.(pdf|png|jpe?g|gif|webp|avif|svg|ico|zip|gz|epub|mobi|txt|csv|ics|vcf|docx?|xlsx?|pptx?|odt)$",
    re.IGNORECASE,
)
_MEDIA_EXT = re.compile(
    r"\.(mp3|m4a|aac|ogg|oga|opus|wav|flac|mp4|m4v|mov|webm|mkv)$", re.IGNORECASE
)


def url_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode()).hexdigest()


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime(_STAMP)


def index_key(h: str) -> str:
    return f"captures/index/{h}.json"


def due_key(kind: Kind, when: datetime, h: str) -> str:
    return f"captures/due/{kind}/{stamp(when)}-{h}.json"


def parse_due_key(key: str) -> tuple[Kind, datetime, str]:
    m = _DUE_RE.match(key)
    if not m:
        raise ValueError(f"not a due key: {key}")
    when = datetime.strptime(m.group(2), _STAMP).replace(tzinfo=UTC)
    return m.group(1), when, m.group(3)  # type: ignore[return-value]


def capture_dir(h: str, when: datetime) -> str:
    return f"captures/{h[:2]}/{h}/{stamp(when)}/"


def guess_kind(url: str, content_type: str | None = None) -> Kind:
    """Which stage captures ``url``. A guess from the URL; a capture-direct
    that gets HTML back hands the URL on to capture-page."""
    parts = urlsplit(url)
    if gallery_post(url):
        return "gallery"
    if (parts.hostname or "").lower() in MEDIA_HOSTS:
        return "media"
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        if ctype in {"text/html", "application/xhtml+xml"}:
            return "page"
        if ctype.startswith(("audio/", "video/")):
            return "media"
        if ctype and ctype != "application/octet-stream":
            return "direct"
    if _MEDIA_EXT.search(parts.path):
        return "media"
    if _DIRECT_EXT.search(parts.path):
        return "direct"
    return "page"


@dataclass
class Attempt:
    at: str
    outcome: Literal["ok", "failed", "refused", "deferred", "rerouted"]
    http_status: int | None = None
    error: str | None = None
    capture: str | None = None
    rerouted_to: Kind | None = None  # the kind a "rerouted" attempt handed it on to
    limit: int | None = None  # the subscription limit that deferred it, in bytes


@dataclass
class CaptureRecord:
    canonical: str
    h: str
    kind: Kind
    status: Status
    created_at: str
    updated_at: str
    attempts: list[Attempt] = field(default_factory=list)
    # the directory of the good capture
    capture: str | None = None
    # the due key that will run it next, while queued or retrying
    due: str | None = None
    wayback: dict[str, str] | None = None
    # the subscriptions ("user/tag") whose messages linked it; None: one with
    # no subscription did, so no subscription limit applies
    subs: list[str] | None = None

    @classmethod
    def load(cls, data: bytes) -> CaptureRecord:
        d = json.loads(data)
        d["attempts"] = [Attempt(**a) for a in d.get("attempts", [])]
        return cls(**d)

    def metadata(self) -> dict[str, str]:
        meta = {
            "status": self.status, "kind": self.kind, "attempts": str(len(self.attempts)),
            "updated_at": self.updated_at,
        }  # fmt: skip
        if self.wayback:
            meta["wayback"] = self.wayback.get("timestamp", "")
        return meta


def save(store: Store, rec: CaptureRecord, *, if_absent: bool = False) -> bool:
    return put_json(store, index_key(rec.h), asdict(rec), if_absent=if_absent,
                    metadata=rec.metadata())  # fmt: skip


def _due(store: Store, kind: Kind, when: datetime, h: str) -> str:
    key = due_key(kind, when, h)
    put_json(store, key, {"h": h, "kind": kind})
    return key


def load_limits(store: Store) -> dict[str, int]:
    """Subscriptions' size limits in bytes, by ``user/tag``."""
    try:
        doc = json.loads(store.get(LIMITS_KEY) or b"{}")
    except ValueError:
        return {}
    got = doc.get("limits") if isinstance(doc, dict) else None
    if not isinstance(got, dict):
        return {}
    return {k: v for k, v in got.items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0}  # fmt: skip


def size_limit(subs: list[str] | None, limits: Mapping[str, int]) -> int | None:
    """The size limit for a capture linked by ``subs``: the highest of their
    limits, or None if any of them has none."""
    if not subs:
        return None
    got = [limits.get(s) for s in subs]
    return None if None in got else max(g for g in got if g is not None)


def _add_sub(rec: CaptureRecord, subscription: str | None) -> bool:
    """Note that ``subscription`` links ``rec``; True if that changed it."""
    if rec.subs is None or subscription in rec.subs:
        return False
    if subscription is None:
        rec.subs = None
    else:
        rec.subs.append(subscription)
    return True


def _held_by_limit(rec: CaptureRecord, limits: Mapping[str, int]) -> bool:
    """Deferred by a subscription limit that no longer applies."""
    last = rec.attempts[-1] if rec.attempts else None
    if rec.status != "deferred" or last is None or last.limit is None:
        return False
    now_limit = size_limit(rec.subs, limits)
    return now_limit is None or now_limit > last.limit


def queue(
    store: Store, canonical: str, kind: Kind, now: datetime, subscription: str | None = None
) -> str:
    """Queue ``canonical`` unless some issue already has, for a message from
    ``subscription`` (``user/tag``, or None if it has none). Returns its
    status: ``new`` when this call queued it, else the record's current one."""
    h = url_hash(canonical)
    when = now.astimezone(UTC).isoformat(timespec="seconds")
    rec = CaptureRecord(canonical, h, kind, "queued", when, when, due=due_key(kind, now, h),
                        subs=[subscription] if subscription else None)  # fmt: skip
    if save(store, rec, if_absent=True):
        _due(store, kind, now, h)
        return "new"
    existing = store.get(index_key(h))
    if existing is None:  # deleted in between; leave it to the next pass
        return "queued"
    old = CaptureRecord.load(existing)
    # a crash between the claim and the due key would strand it
    if old.status in {"queued", "retrying"} and old.due and not store.exists(old.due):
        put_json(store, old.due, {"h": h, "kind": old.kind})
    # not atomic: a capture finishing meanwhile can drop this subscription
    if _add_sub(old, subscription) and old.status in {"queued", "retrying", "deferred"}:
        if _held_by_limit(old, load_limits(store)):
            old.status, old.updated_at = "queued", when
            old.due = _due(store, old.kind, now, h)
        save(store, old)
    return old.status


@dataclass
class CaptureResult:
    h: str
    kind: str
    outcome: str
    status: str
    attempts: int = 0

    def line(self) -> str:
        """One stdout line for courier's log: no URLs."""
        return (
            f"CAPTURE {self.h} kind={self.kind} outcome={self.outcome} "
            f"status={self.status} attempts={self.attempts}"
        )


def run(
    store: Store,
    key: str,
    fetcher: GuardedFetcher,
    *,
    now: datetime,
    pacer: HostPacer | None = None,
    wayback_fetcher: GuardedFetcher | None = None,
    page_crawler: Callable[[str], PageOutcome] | None = None,
    media_grabber: MediaGrabber | None = None,
    gallery_collector: MediaGrabber | None = None,
) -> CaptureResult:
    """Run the capture a due key asks for. Page keys need ``page_crawler``,
    which takes the canonical URL (see ``page.crawl_page``); media keys need
    ``media_grabber`` (see ``media.grab_media``), and gallery keys
    ``gallery_collector`` (see ``gallery.collect``)."""
    kind, _, h = parse_due_key(key)
    pacer = pacer or HostPacer()
    data = store.get(index_key(h))
    if data is None:
        store.delete(key)
        return CaptureResult(h, kind, "orphan", "none")
    rec = CaptureRecord.load(data)
    if rec.status in FINAL or rec.due != key:
        # done already, or superseded by a newer due key
        store.delete(key)
        return CaptureResult(h, kind, "stale", rec.status, len(rec.attempts))
    if rec.kind != kind:
        rec.due = _due(store, rec.kind, now, h)
        save(store, rec)
        store.delete(key)
        return CaptureResult(h, kind, "rerouted", rec.status, len(rec.attempts))
    if tracking_vendor(rec.canonical):
        # a click-tracker's URL carries the reader's token: never requested,
        # here or at the Wayback Machine (older resolves could queue one)
        rec.attempts.append(Attempt(_iso(now), "refused", error="click-tracker URL"))
        rec.status, rec.due, rec.updated_at = "refused", None, _iso(now)
        save(store, rec)
        store.delete(key)
        return CaptureResult(h, kind, "refused", rec.status, len(rec.attempts))
    if (kind == "page" and gallery_post(rec.canonical)
            and not any(a.rerouted_to == "page" for a in rec.attempts)):  # fmt: skip
        # queued as a page before there was a gallery stage; one that stage
        # (or another) handed on to capture-page stays here
        rec.kind = "gallery"
        rec.due = _due(store, rec.kind, now, h)
        save(store, rec)
        store.delete(key)
        return CaptureResult(h, kind, "rerouted", rec.status, len(rec.attempts))
    stages = {"page": page_crawler, "media": media_grabber, "gallery": gallery_collector}
    if kind in stages and stages[kind] is None:
        raise NotImplementedError(f"no {kind} capture stage here")

    # take a lease: if this process dies, the lease falls due and it runs again
    lease = _due(store, kind, now + LEASE[kind], h)
    rec.due = lease
    save(store, rec)
    store.delete(key)

    limit = size_limit(rec.subs, load_limits(store))
    if kind == "page":
        assert page_crawler is not None
        attempt = capture_page(store, rec, page_crawler, now, limit)
    elif kind == "media":
        assert media_grabber is not None
        attempt = capture_media(store, rec, media_grabber, now, limit=limit)
    elif kind == "gallery":
        assert gallery_collector is not None
        attempt = capture_media(store, rec, gallery_collector, now, store_gallery, limit)
    else:
        attempt = capture_direct(store, rec, fetcher, pacer, now, limit)
    rec.attempts.append(attempt)
    rec.updated_at = attempt.at
    if attempt.outcome == "ok":
        rec.status, rec.capture, rec.due = "ok", attempt.capture, None
    elif attempt.outcome == "rerouted":
        rec.kind = attempt.rerouted_to or "page"
        rec.due = _due(store, rec.kind, now, h)
    elif attempt.outcome in {"refused", "deferred"}:
        rec.status, rec.due = attempt.outcome, None
    else:
        failures = sum(1 for a in rec.attempts if a.outcome == "failed")
        if failures <= len(RETRY_AFTER):
            rec.status = "retrying"
            rec.due = _due(store, kind, now + RETRY_AFTER[failures - 1], h)
        else:
            rec.status, rec.due = "failed", None
        early = kind in WAYBACK_KINDS and (
            failures >= WAYBACK_AFTER_FAILURES or attempt.http_status in WAYBACK_AT_ONCE
        )
        if (
            wayback_fetcher is not None
            and rec.wayback is None
            and (early or rec.status == "failed")
        ):
            rec.wayback = fetch_wayback(store, rec, wayback_fetcher, pacer, now,
                                        page_crawler if kind == "page" else None)  # fmt: skip
    save(store, rec)
    if rec.due != lease:  # a 1 h retry lands on the lease's own key
        store.delete(lease)
    return CaptureResult(h, kind, attempt.outcome, rec.status, len(rec.attempts))


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def _store_capture(
    store: Store, rec: CaptureRecord, now: datetime, chain: tuple[str, ...], status: int,
    headers: Mapping[str, str], body: bytes, via: str,
) -> str:  # fmt: skip
    folder = capture_dir(rec.h, now)
    ctype = headers.get("content-type")
    name = "original" + ext_for(ctype, chain[-1])
    store.put(folder + name, body, ctype or "application/octet-stream")
    put_json(
        store,
        folder + "capture.json",
        {
            "canonical": rec.canonical,
            "via": via,
            # the canonical URL's own redirects; no subscriber tokens
            "chain": list(chain),
            "http_status": status,
            # cookies are the server's for this fetch, not part of what was captured
            "headers": {k: v for k, v in headers.items() if k.lower() != "set-cookie"},
            "file": name,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at": _iso(now),
            "tool": f"dea-capture {__version__}",
        },
    )
    return folder


def _over_limit(at: str, size: int, limit: int | None, status: int | None) -> Attempt | None:
    if limit is None or size <= limit:
        return None
    why = f"{size // 2**20} MiB, over the subscription's {limit // 2**20} MiB limit"
    return Attempt(at, "deferred", status, why, limit=limit)


def capture_direct(
    store: Store, rec: CaptureRecord, fetcher: GuardedFetcher, pacer: HostPacer, now: datetime,
    limit: int | None = None,
) -> Attempt:  # fmt: skip
    at = _iso(now)
    pacer.wait(rec.canonical)
    try:
        got = follow(fetcher, rec.canonical, "link")
    except ForbiddenFetch as exc:
        return Attempt(at, "refused", error=str(exc))
    except TooLarge as exc:
        return Attempt(at, "deferred", error=str(exc))
    except FetchError as exc:
        return Attempt(at, "failed", error=str(exc))
    resp = got.response
    if not 200 <= resp.status < 300:
        return Attempt(at, "failed", resp.status, f"HTTP {resp.status}")
    # an HTML page, or a video site's player page: not a file
    kind = guess_kind(got.chain[-1], resp.headers.get("content-type"))
    if kind != "direct":
        return Attempt(at, "rerouted", resp.status, f"needs capture-{kind}", rerouted_to=kind)
    if over := _over_limit(at, len(resp.body), limit, resp.status):
        return over
    folder = _store_capture(store, rec, now, got.chain, resp.status, resp.headers, resp.body,
                            "direct")  # fmt: skip
    return Attempt(at, "ok", resp.status, capture=folder)


def capture_page(
    store: Store, rec: CaptureRecord, crawler: Callable[[str], PageOutcome], now: datetime,
    limit: int | None = None,
) -> Attempt:  # fmt: skip
    at = _iso(now)
    got = crawler(rec.canonical)
    if got.outcome == "refused":
        return Attempt(at, "refused", got.http_status, got.error)
    if got.outcome != "ok":
        return Attempt(at, "failed", got.http_status, got.error)
    size = sum(len(b) for b in got.files.values())
    if over := _over_limit(at, size, limit, got.http_status):
        return over
    folder = capture_dir(rec.h, now)
    store_page(store, folder, rec.canonical, got, at)
    return Attempt(at, "ok", got.http_status, capture=folder)


# takes the canonical URL, what to call with a good outcome while its files
# exist, and the subscription's size limit, if any
MediaGrabber = Callable[[str, Callable[[MediaOutcome], None], "int | None"], MediaOutcome]


def capture_media(
    store: Store, rec: CaptureRecord, grabber: MediaGrabber, now: datetime,
    keep: Callable[..., None] = store_media, limit: int | None = None,
) -> Attempt:  # fmt: skip
    """capture-media, or capture-gallery with ``keep=store_gallery``: both hand
    anything they can't take to capture-page."""
    at = _iso(now)
    folder = capture_dir(rec.h, now)
    got = grabber(rec.canonical, lambda g: keep(store, folder, rec.canonical, g, at), limit)
    if got.outcome == "ok":
        return Attempt(at, "ok", capture=folder)
    if got.outcome == "rerouted":
        return Attempt(at, "rerouted", error=got.error, rerouted_to="page")
    return Attempt(at, got.outcome, error=got.error, limit=limit if got.limited else None)


def wayback_url(timestamp: str, original: str, mode: str = "") -> str:
    """A snapshot's address. ``mode`` ``id_`` is its original bytes; ``if_``
    the page as Wayback replays it, without the toolbar around it."""
    return f"https://web.archive.org/web/{timestamp}{mode}/{original}"


def wayback_lookup(
    rec: CaptureRecord, fetcher: GuardedFetcher, pacer: HostPacer
) -> tuple[str, str] | None:
    """The timestamp and archived URL of the good (HTTP 200) snapshot closest
    to when ``rec`` was first queued, from the CDX API. (The availability API
    misses snapshots that CDX has.)"""
    first = stamp(datetime.fromisoformat(rec.created_at))
    api = "https://web.archive.org/cdx/search/cdx?" + urlencode({
        "url": rec.canonical, "output": "json", "fl": "timestamp,original",
        "filter": "statuscode:200", "closest": first[:8] + first[9:15], "sort": "closest",
        "limit": "1",
    })  # fmt: skip
    pacer.wait(api)
    resp = follow(fetcher, api, "link").response
    if not 200 <= resp.status < 300:
        return None
    rows: Any = json.loads(resp.body or b"[]")
    if not isinstance(rows, list) or len(rows) < 2:  # the first row is the field names
        return None
    ts, original = str(rows[1][0]), str(rows[1][1])
    if not re.fullmatch(r"\d{14}", ts) or not original.startswith(("http://", "https://")):
        return None
    return ts, original


def fetch_wayback(
    store: Store, rec: CaptureRecord, fetcher: GuardedFetcher, pacer: HostPacer, now: datetime,
    page_crawler: Callable[[str], PageOutcome] | None = None,
) -> dict[str, str] | None:  # fmt: skip
    """Store the Wayback snapshot closest to when the URL was first queued.
    With ``page_crawler``, the replayed snapshot is crawled like any page, into
    a WACZ; else, or if that fails, its original bytes are stored."""
    try:
        found = wayback_lookup(rec, fetcher, pacer)
    except (ForbiddenFetch, FetchError, TooLarge, IndexError, TypeError, ValueError):
        return None
    if found is None:
        return None
    ts, original = found
    folder = capture_dir(rec.h, now)
    if page_crawler is not None:
        replay = wayback_url(ts, original, "if_")
        crawled = page_crawler(replay)
        if crawled.outcome == "ok":
            store_page(store, folder, rec.canonical, crawled, _iso(now), via=f"wayback:{ts}",
                       snapshot=replay)  # fmt: skip
            return {"timestamp": ts, "capture": folder, "via": "crawl"}
    raw = wayback_url(ts, original, "id_")
    try:
        pacer.wait(raw)
        got = follow(fetcher, raw, "link")
    except (ForbiddenFetch, FetchError, TooLarge):
        return None
    if not 200 <= got.response.status < 300:
        return None
    r = got.response
    _store_capture(store, rec, now, got.chain, r.status, r.headers, r.body, f"wayback:{ts}")
    return {"timestamp": ts, "capture": folder, "via": "raw"}
