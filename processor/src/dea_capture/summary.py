"""The dashboard's summary: one object the admin Worker reads instead of
listing the bucket, which costs more CPU than a Worker on the free plan has.

``dashboard/summary.json`` holds, as of ``generated_at``:

- ``incoming``: each archived and quarantined message's key, size, upload
  time and the metadata the dashboard shows (user, tag, envelope sender);
- ``statuses``: each ``issue-status/`` record's key and the metadata the
  dashboard reads (``STATUS_META``: states, the message's key and counts, no
  URLs); a count of 0 is left out, which the dashboard reads as 0;
- ``aggregates``: precomputed overview statistics and capture progress, including
  all message rows for browsing; the Worker rehydrates dates and maps only;
- ``retries``: how many of the dashboard's retry requests are waiting, and
  what the latest did (``dashboard/retries.json``);
- ``urls``: how many ``captures/index/`` records there are of each kind and
  status, how many have a Wayback snapshot, and the latest update;
- ``storage``: what the bucket holds, as of ``computed_at``: objects and
  bytes by area (``AREAS``), the link captures by kind, and each message's
  ``issues/<sha>/`` in bytes. Listing every object takes a while, so this is
  redone every ``STORAGE_EVERY``, and kept in the state in between.

With each storage count, ``dashboard/largest.json`` lists the ``LARGEST``
biggest objects: key, size, area, and for a link capture its kind and the
URL captured (from its index record), for a message's file the message. It's
a separate object, read only by the dashboard's storage page, so the summary
stays small and has no URLs.

It is written compact, since the Worker parses it on every page view. It is
rebuilt from listings, so deleted objects drop out. Archive and capture
aggregates are precomputed here, so the Worker only
rehydrates their dates and maps. Full message rows are retained for browsing.
Metadata is read (a HEAD request) when ETag or modification time changed; the
ETags and metadata seen are kept in ``dashboard/summary-state.json``.
"""

from __future__ import annotations

import heapq
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .store import Listed, Store

SUMMARY_KEY = "dashboard/summary.json"
STATE_KEY = "dashboard/summary-state.json"
# the dashboard's retry requests and what they did (see retry.py)
RETRY_REQUESTS = "captures/retry/"
RETRY_RESULTS = "dashboard/retries.json"
VERSION = 1
STORAGE_EVERY = timedelta(hours=1)
LARGEST_KEY = "dashboard/largest.json"
LARGEST = 100
# the parts of the bucket the dashboard shows the size of
AREAS = ("archive", "quarantine", "embeds", "views", "link_records", "captures",
         "capture_index", "queue", "dashboard", "other")  # fmt: skip

_INCOMING = re.compile(r"^incoming/(archive|quarantine)/\d{4}-\d{2}-\d{2}/")
_STATUS = re.compile(r"^issue-status/[0-9a-f]{64}\.json$")
_INDEX = re.compile(r"^captures/index/[0-9a-f]{64}\.json$")
INCOMING_META = ("user", "tag", "envelope_from")
INDEX_META = ("kind", "status", "wayback", "updated_at")
# what the dashboard reads of an issue-status record (cloudflare/src/admin/captures.ts)
STATUS_STATES = ("source", "state", "links_state", "completed_at")
STATUS_COUNTS = (
    "assets_ok", "assets_failed", "assets_refused", "assets_deferred", "pixels", "links",
    "links_capture", "links_resolve", "links_skip", "links_resolved", "links_unresolved",
    "links_refused", "captures_queued",
)  # fmt: skip
_BUCKET = re.compile(r"^s3://[^/]+/")


class Listable(Store, Protocol):
    def listing(self, prefix: str) -> list[Listed]: ...

    def metadata(self, key: str) -> dict[str, str] | None: ...


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def _status_meta(meta: dict[str, str]) -> dict[str, str]:
    """The fields the dashboard reads; ``source`` as a key, without the bucket."""
    out = {k: meta[k] for k in STATUS_STATES if meta.get(k)}
    if "source" in out:
        out["source"] = _BUCKET.sub("", out["source"])
    out.update((k, meta[k]) for k in STATUS_COUNTS if meta.get(k) not in (None, "", "0"))
    return out


def _put_compact(store: Store, key: str, obj: object) -> None:
    store.put(key, json.dumps(obj, separators=(",", ":")).encode(), "application/json")


def _retries(store: Listable) -> dict[str, Any]:
    """How many retry requests are waiting, and what the latest ones did."""
    try:
        done = json.loads(store.get(RETRY_RESULTS) or b"[]")
    except ValueError:
        done = []
    waiting = [o for o in store.listing(RETRY_REQUESTS) if o.key.endswith(".json")]
    return {"waiting": len(waiting), "recent": done if isinstance(done, list) else []}


def _area(parts: list[str]) -> str:
    top, second = parts[0], parts[1] if len(parts) > 1 else ""
    if top == "incoming":
        return {"archive": "archive", "quarantine": "quarantine"}.get(second, "other")
    if top == "issues":
        third = parts[2] if len(parts) > 3 else ""
        return {"assets": "embeds", "view": "views"}.get(third, "link_records")
    if top == "issue-status":
        return "link_records"
    if top == "captures":
        if second == "index":
            return "capture_index"
        return "queue" if second in ("due", "retry") else "captures"
    return "dashboard" if top == "dashboard" else "other"


def _largest(store: Store, biggest: list[tuple[int, str, Listed]],
             index: dict[str, dict[str, str]], now: datetime) -> dict[str, Any]:  # fmt: skip
    """The biggest objects, largest first, with what they belong to."""
    files = []
    for _, _, o in sorted(biggest, key=lambda t: (-t[0], t[1])):
        parts = o.key.split("/")
        area = _area(parts)
        f: dict[str, Any] = {"key": o.key, "size": o.size, "modified": _iso(o.modified),
                             "area": area}  # fmt: skip
        if area == "captures" and len(parts) > 3:
            ix = f"captures/index/{parts[2]}.json"
            f["kind"] = index.get(ix, {}).get("kind", "unknown")
            try:
                url = json.loads(store.get(ix) or b"{}").get("canonical")
            except (ValueError, AttributeError):
                url = None
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                f["url"] = url
        elif parts[0] == "issues" and len(parts) > 2:
            f["sha"] = parts[1]
        elif area in ("archive", "quarantine"):
            f["sha"] = parts[-1].removesuffix(".eml")
        files.append(f)
    return {"computed_at": _iso(now), "files": files}


def _storage(store: Listable, index: dict[str, dict[str, str]], now: datetime) -> dict[str, Any]:
    """Objects and bytes by area, link captures by kind (from ``index``, each
    index record's metadata) and each message's ``issues/<sha>/`` bytes.
    Writes ``LARGEST_KEY`` as well."""
    areas = {a: {"count": 0, "bytes": 0} for a in AREAS}
    kinds: dict[str, dict[str, Any]] = {}
    messages: dict[str, int] = {}
    biggest: list[tuple[int, str, Listed]] = []  # a min-heap of the LARGEST
    for o in store.listing(""):
        if len(biggest) < LARGEST:
            heapq.heappush(biggest, (o.size, o.key, o))
        elif o.size > biggest[0][0]:
            heapq.heapreplace(biggest, (o.size, o.key, o))
        parts = o.key.split("/")
        area = _area(parts)
        areas[area]["count"] += 1
        areas[area]["bytes"] += o.size
        if parts[0] == "issues" and len(parts) > 2:
            messages[parts[1]] = messages.get(parts[1], 0) + o.size
        elif area == "captures" and len(parts) > 3:
            kind = index.get(f"captures/index/{parts[2]}.json", {}).get("kind", "unknown")
            k = kinds.setdefault(kind, {"count": 0, "bytes": 0, "urls": set()})
            k["count"] += 1
            k["bytes"] += o.size
            k["urls"].add(parts[2])
    _put_compact(store, LARGEST_KEY, _largest(store, biggest, index, now))
    return {
        "computed_at": _iso(now),
        "areas": areas,
        "captures": {
            kind: {"count": k["count"], "bytes": k["bytes"], "urls": len(k["urls"])}
            for kind, k in sorted(kinds.items())
        },
        "messages": messages,
    }


def cache_current(prior: Any, obj: Listed) -> bool:
    return (
        isinstance(prior, list)
        and len(prior) == 2
        and prior[0] == [obj.etag, obj.modified.isoformat()]
    )


def _metadata_for(
    store: Listable, objects: list[Listed], seen: dict[str, list[Any]],
    keep: tuple[str, ...] | None, workers: int,
) -> tuple[dict[str, dict[str, str]], int]:  # fmt: skip
    """Each object's metadata (only the ``keep`` fields, if given): from
    ``seen`` when its ETag and modification time match, else read. Returns it and the
    number read. Updates ``seen``."""
    got: dict[str, dict[str, str]] = {}
    stale = []
    for o in objects:
        prior = seen.get(o.key)
        if prior is not None and cache_current(prior, o):
            got[o.key] = prior[1]
        else:
            stale.append(o)

    def read(o: Listed) -> tuple[Listed, dict[str, str] | None]:
        return o, store.metadata(o.key)

    with ThreadPoolExecutor(workers) as pool:
        for o, meta in pool.map(read, stale):
            if meta is None:  # deleted since the listing
                continue
            if keep is not None:
                meta = {k: v for k, v in meta.items() if k in keep}
            got[o.key] = meta
            seen[o.key] = [[o.etag, o.modified.isoformat()], meta]
    for key in set(seen) - {o.key for o in objects}:
        del seen[key]
    return got, len(stale)


def _aggregates(
    incoming: list[dict[str, Any]], statuses: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    def zero() -> dict[str, int]:
        return {"archive": 0, "quarantine": 0}

    def states() -> dict[str, int]:
        return {"done": 0, "partial": 0, "pending": 0}

    today = now.astimezone(UTC).date()
    since7, since30 = str(today - timedelta(days=6)), str(today - timedelta(days=29))
    days = {str(today - timedelta(days=i)): zero() for i in range(13, -1, -1)}
    stats: dict[str, Any] = {
        "partial": False,
        "totals": {d: {"count": 0, "bytes": 0} for d in zero()},
        "last7": zero(),
        "last30": zero(),
        "tags": {},
        "oldestQuarantine": None,
    }
    captures: dict[str, Any] = {
        "partial": False,
        "messages": [],
        "states": states(),
        "linkStates": states(),
        "totals": dict.fromkeys(STATUS_COUNTS, 0),
        "lastCompleted": None,
        "bySubscription": {},
    }
    by_source = {}
    for obj in statuses:
        meta = obj["customMetadata"]
        if not meta.get("source"):
            continue
        by_source[meta["source"]] = {
            "sha": obj["key"].split("/")[-1].removesuffix(".json"),
            "source": meta["source"],
            "state": "done" if meta.get("state") == "done" else "partial",
            "linksState": meta.get("links_state")
            if meta.get("links_state") in ("done", "partial")
            else "pending",
            "completedAt": meta.get("completed_at"),
            "counts": {k: _count(meta.get(k)) for k in STATUS_COUNTS},
        }
    senders: dict[str, int] = {}
    for obj in sorted(incoming, key=lambda o: o["key"], reverse=True):
        _, disposition, day, *_ = obj["key"].split("/")
        meta, size = obj["customMetadata"], obj["size"]
        stats["totals"][disposition]["count"] += 1
        stats["totals"][disposition]["bytes"] += size
        for since, field in ((since7, "last7"), (since30, "last30")):
            if day >= since:
                stats[field][disposition] += 1
        if day in days:
            days[day][disposition] += 1
        if disposition == "quarantine":
            stats["oldestQuarantine"] = min(stats["oldestQuarantine"] or day, day)
        user, tag = meta.get("user", ""), meta.get("tag", "")
        subscription = f"{user}/{tag}"
        if user:
            t = stats["tags"].setdefault(
                subscription,
                {
                    "user": user,
                    "tag": tag,
                    **zero(),
                    "bytes": 0,
                    "last": None,
                },
            )
            t[disposition] += 1
            t["bytes"] += size
            t["last"] = max(t["last"] or obj["uploaded"], obj["uploaded"])
        sender = meta.get("envelope_from", "")
        domain = sender.rsplit("@", 1)[-1].strip().lower() if sender.rfind("@") > 0 else ""
        if domain and day >= since30:
            senders[domain] = senders.get(domain, 0) + 1
        if disposition != "archive":
            continue
        capture = by_source.get(obj["key"])
        state = capture["state"] if capture else "pending"
        captures["states"][state] += 1
        if capture:
            captures["linkStates"][capture["linksState"]] += 1
            for k, v in capture["counts"].items():
                captures["totals"][k] += v
            completed = capture["completedAt"]
            if completed:
                captures["lastCompleted"] = max(captures["lastCompleted"] or completed, completed)
        if user:
            captures["bySubscription"].setdefault(subscription, states())[state] += 1
        captures["messages"].append(
            {"key": obj["key"], "day": day, "user": user, "tag": tag, "capture": capture}
        )
    stats["days"] = [{"day": day, **v} for day, v in days.items()]
    stats["senders"] = [
        {"domain": d, "count": n}
        for d, n in sorted(senders.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"stats": stats, "captures": captures}


def _count(value: str | None) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def build(
    store: Listable, now: Callable[[], datetime] = lambda: datetime.now(UTC), workers: int = 16
) -> dict[str, Any]:
    """Rebuild and store the summary. Returns counts for the log, no keys."""
    try:
        state = json.loads(store.get(STATE_KEY) or b"{}")
    except ValueError:
        state = {}
    if state.get("version") != VERSION:
        state = {"version": VERSION}
    reads = {}

    incoming = [o for o in store.listing("incoming/") if _INCOMING.match(o.key)]
    meta, reads["incoming"] = _metadata_for(
        store, incoming, state.setdefault("incoming", {}), INCOMING_META, workers
    )
    incoming_out = [
        {"key": o.key, "size": o.size, "uploaded": _iso(o.modified), "customMetadata": meta[o.key]}
        for o in incoming if o.key in meta
    ]  # fmt: skip

    statuses = [o for o in store.listing("issue-status/") if _STATUS.match(o.key)]
    meta, reads["statuses"] = _metadata_for(
        store, statuses, state.setdefault("statuses", {}), None, workers
    )
    statuses_out = [
        {"key": o.key, "customMetadata": _status_meta(meta[o.key])}
        for o in statuses if o.key in meta
    ]  # fmt: skip

    index = [o for o in store.listing("captures/index/") if _INDEX.match(o.key)]
    meta, reads["index"] = _metadata_for(
        store, index, state.setdefault("index", {}), INDEX_META, workers
    )
    by_kind: dict[str, dict[str, int]] = {}
    wayback = 0
    last: str | None = None
    for m in meta.values():
        kind, status = m.get("kind"), m.get("status")
        if not kind or not status:
            continue
        by_kind.setdefault(kind, {})
        by_kind[kind][status] = by_kind[kind].get(status, 0) + 1
        wayback += bool(m.get("wayback"))
        if m.get("updated_at") and (last is None or m["updated_at"] > last):
            last = m["updated_at"]

    total = sum(sum(s.values()) for s in by_kind.values())

    storage = state.get("storage")
    try:
        due = datetime.fromisoformat(storage["computed_at"]) + STORAGE_EVERY <= now()
    except (TypeError, KeyError, ValueError):
        due = True
    if due:
        storage = state["storage"] = _storage(store, meta, now())
    summary = {
        "version": VERSION,
        "generated_at": _iso(now()),
        "incoming": incoming_out,
        "statuses": statuses_out,
        "aggregates": _aggregates(incoming_out, statuses_out, now()),
        "retries": _retries(store),
        "urls": {
            "total": total,
            "byKind": by_kind,
            "wayback": wayback,
            "lastUpdated": last,
        },
        "storage": storage,
    }
    _put_compact(store, STATE_KEY, state)
    _put_compact(store, SUMMARY_KEY, summary)
    return {
        "incoming": len(incoming_out), "statuses": len(statuses_out),
        "urls": total, "metadata_read": reads, "storage_listed": due,
    }  # fmt: skip
