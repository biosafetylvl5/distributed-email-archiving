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
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from . import __version__
from .fetch import FetchError, HostPacer, TooLarge, follow
from .guard import ForbiddenFetch, GuardedFetcher
from .store import Store, ext_for, put_json

Kind = Literal["page", "direct", "media"]
KINDS: tuple[Kind, ...] = ("page", "direct", "media")
Status = Literal["queued", "retrying", "ok", "failed", "refused", "deferred"]
FINAL: frozenset[str] = frozenset({"ok", "failed", "refused", "deferred"})

# after the first attempt fails: retry 1 h, 6 h, 1 d, 7 d and 30 d later,
# then take the closest Wayback Machine snapshot
RETRY_AFTER = (
    timedelta(hours=1), timedelta(hours=6), timedelta(days=1), timedelta(days=7),
    timedelta(days=30),
)  # fmt: skip
# longer than any stage's courier timeout
LEASE = {"page": timedelta(hours=1), "direct": timedelta(hours=1), "media": timedelta(hours=6)}

_STAMP = "%Y%m%dT%H%M%SZ"
_DUE_RE = re.compile(r"^captures/due/(page|direct|media)/(\d{8}T\d{6}Z)-([0-9a-f]{64})\.json$")

MEDIA_HOSTS = frozenset(
    {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com",
        "vimeo.com", "player.vimeo.com", "soundcloud.com", "www.tiktok.com",
    }
)  # fmt: skip
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


def queue(store: Store, canonical: str, kind: Kind, now: datetime) -> str:
    """Queue ``canonical`` unless some issue already has. Returns its status:
    ``new`` when this call queued it, else the record's current status."""
    h = url_hash(canonical)
    when = now.astimezone(UTC).isoformat(timespec="seconds")
    rec = CaptureRecord(canonical, h, kind, "queued", when, when, due=due_key(kind, now, h))
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
) -> CaptureResult:
    """Run the capture a due key asks for."""
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
    if kind != "direct":
        raise NotImplementedError(f"no {kind} capture stage yet")

    # take a lease: if this process dies, the lease falls due and it runs again
    lease = _due(store, kind, now + LEASE[kind], h)
    rec.due = lease
    save(store, rec)
    store.delete(key)

    attempt = capture_direct(store, rec, fetcher, pacer, now)
    rec.attempts.append(attempt)
    rec.updated_at = attempt.at
    if attempt.outcome == "ok":
        rec.status, rec.capture, rec.due = "ok", attempt.capture, None
    elif attempt.outcome == "rerouted":
        rec.kind = "page"
        rec.due = _due(store, "page", now, h)
    elif attempt.outcome in {"refused", "deferred"}:
        rec.status, rec.due = attempt.outcome, None
    else:
        failures = sum(1 for a in rec.attempts if a.outcome == "failed")
        if failures <= len(RETRY_AFTER):
            rec.status = "retrying"
            rec.due = _due(store, kind, now + RETRY_AFTER[failures - 1], h)
        else:
            rec.status, rec.due = "failed", None
            if wayback_fetcher is not None:
                rec.wayback = fetch_wayback(store, rec, wayback_fetcher, pacer, now)
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
            "headers": dict(headers),
            "file": name,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at": _iso(now),
            "tool": f"dea-capture {__version__}",
        },
    )
    return folder


def capture_direct(
    store: Store, rec: CaptureRecord, fetcher: GuardedFetcher, pacer: HostPacer, now: datetime
) -> Attempt:
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
    if guess_kind(got.chain[-1], resp.headers.get("content-type")) == "page":
        return Attempt(at, "rerouted", resp.status, "HTML: needs capture-page")
    folder = _store_capture(store, rec, now, got.chain, resp.status, resp.headers, resp.body,
                            "direct")  # fmt: skip
    return Attempt(at, "ok", resp.status, capture=folder)


def wayback_raw(snapshot_url: str, timestamp: str) -> str:
    """The snapshot's original bytes (``id_``), not the Wayback page around them."""
    parts = urlsplit(snapshot_url)
    path = parts.path.replace(f"/web/{timestamp}/", f"/web/{timestamp}id_/", 1)
    return f"https://web.archive.org{path}" + (f"?{parts.query}" if parts.query else "")


def fetch_wayback(
    store: Store, rec: CaptureRecord, fetcher: GuardedFetcher, pacer: HostPacer, now: datetime
) -> dict[str, str] | None:
    """Store the Wayback snapshot closest to when the URL was first queued."""
    first = datetime.fromisoformat(rec.created_at)
    api = "https://archive.org/wayback/available?" + urlencode(
        {"url": rec.canonical, "timestamp": stamp(first)[:8] + stamp(first)[9:15]}
    )
    try:
        pacer.wait(api)
        meta = json.loads(follow(fetcher, api, "link").response.body)
        closest: dict[str, Any] = meta["archived_snapshots"]["closest"]
        if not closest.get("available") or not str(closest.get("status", "")).startswith("2"):
            return None
        ts = str(closest["timestamp"])
        raw = wayback_raw(str(closest["url"]), ts)
        pacer.wait(raw)
        got = follow(fetcher, raw, "link")
    except (ForbiddenFetch, FetchError, TooLarge, KeyError, TypeError, ValueError):
        return None
    if not 200 <= got.response.status < 300:
        return None
    r = got.response
    folder = _store_capture(store, rec, now, got.chain, r.status, r.headers, r.body,
                            f"wayback:{ts}")  # fmt: skip
    return {"timestamp": ts, "capture": folder}
