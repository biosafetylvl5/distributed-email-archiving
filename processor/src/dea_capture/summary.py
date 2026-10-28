"""The dashboard's summary: one object the admin Worker reads instead of
listing the bucket, which costs more CPU than a Worker on the free plan has.

``dashboard/summary.json`` holds, as of ``generated_at``:

- ``incoming``: each archived and quarantined message's key, size, upload
  time and the metadata the dashboard shows (user, tag, envelope sender);
- ``statuses``: each ``issue-status/`` record's key and metadata (counts and
  states, no URLs);
- ``urls``: how many ``captures/index/`` records there are of each kind and
  status, how many have a Wayback snapshot, and the latest update.

It is rebuilt from listings, so deleted objects drop out. Metadata is read
(a HEAD request) only for objects whose ETag changed since the last run; the
ETags and metadata seen are kept in ``dashboard/summary-state.json``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol

from .store import Listed, Store, put_json

SUMMARY_KEY = "dashboard/summary.json"
STATE_KEY = "dashboard/summary-state.json"
VERSION = 1

_INCOMING = re.compile(r"^incoming/(archive|quarantine)/\d{4}-\d{2}-\d{2}/")
_STATUS = re.compile(r"^issue-status/[0-9a-f]{64}\.json$")
_INDEX = re.compile(r"^captures/index/[0-9a-f]{64}\.json$")
INCOMING_META = ("user", "tag", "envelope_from")
INDEX_META = ("kind", "status", "wayback", "updated_at")


class Listable(Store, Protocol):
    def listing(self, prefix: str) -> list[Listed]: ...

    def metadata(self, key: str) -> dict[str, str] | None: ...


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def _metadata_for(
    store: Listable, objects: list[Listed], seen: dict[str, list[Any]],
    keep: tuple[str, ...] | None, workers: int,
) -> tuple[dict[str, dict[str, str]], int]:  # fmt: skip
    """Each object's metadata (only the ``keep`` fields, if given): from
    ``seen`` when its ETag hasn't changed, else read. Returns it and the
    number read. Updates ``seen``."""
    got: dict[str, dict[str, str]] = {}
    stale = []
    for o in objects:
        prior = seen.get(o.key)
        if prior and prior[0] == o.etag:
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
            seen[o.key] = [o.etag, meta]
    for key in set(seen) - {o.key for o in objects}:
        del seen[key]
    return got, len(stale)


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
        {"key": o.key, "customMetadata": meta[o.key]} for o in statuses if o.key in meta
    ]

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
    summary = {
        "version": VERSION,
        "generated_at": _iso(now()),
        "incoming": incoming_out,
        "statuses": statuses_out,
        "urls": {
            "total": total,
            "byKind": by_kind,
            "wayback": wayback,
            "lastUpdated": last,
        },
    }
    put_json(store, STATE_KEY, state)
    put_json(store, SUMMARY_KEY, summary)
    return {
        "incoming": len(incoming_out), "statuses": len(statuses_out),
        "urls": total, "metadata_read": reads,
    }  # fmt: skip
