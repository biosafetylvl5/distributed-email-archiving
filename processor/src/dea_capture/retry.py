"""Retry requests from the admin dashboard.

The dashboard never edits a capture record: a capture that is running holds
its record and saves it when done, so it would undo the edit. It writes a
request under ``captures/retry/`` instead, and ``apply_requests`` (run by the
summary stage, every 5 minutes) applies it with the same record logic as the
capture stages. A request is one of:

- ``{"scope": "message", "sha": <sha256>}``: follow again the message's
  redirects that couldn't be followed, then retry its URLs that failed;
- ``{"scope": "urls", "kind": <kind>, "status": "retrying" | "failed"}``:
  retry every URL of that kind and status.

Retrying a URL makes it due now. One due within its kind's lease time is
left alone: it may be a running capture's lease, and is due soon anyway.
A ``failed`` URL goes back to ``retrying``; it is ``failed`` again if this
attempt fails too.

What each request did is kept in ``dashboard/retries.json`` (the latest
``RESULTS_KEPT``), counts only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .capture import (
    KINDS,
    LEASE,
    CaptureRecord,
    due_key,
    index_key,
    parse_due_key,
    save,
)
from .issue import status_key
from .links import Relinked, load_links
from .store import Store, put_json
from .summary import RETRY_REQUESTS, RETRY_RESULTS, STATE_KEY

REQUESTS, RESULTS_KEY = RETRY_REQUESTS, RETRY_RESULTS
RESULTS_KEPT = 20
# seconds between requests to one host when following redirects again
RELINK_INTERVAL = 10.0
RETRYABLE = ("retrying", "failed")
_REQUEST = re.compile(r"^captures/retry/\d{8}T\d{6}Z-[0-9a-f]{8,32}\.json$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^s3://[^/]+/")

# (the message's raw bytes, a time.monotonic() deadline) -> what relinking did
Relinker = Callable[[bytes, float], Relinked]


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def reschedule(store: Store, h: str, now: datetime) -> bool:
    """Make a ``retrying`` or ``failed`` URL due now; False if left alone."""
    data = store.get(index_key(h))
    if data is None:
        return False
    rec = CaptureRecord.load(data)
    if rec.status not in RETRYABLE:
        return False
    old = rec.due
    if old is not None:
        try:
            _, when, _ = parse_due_key(old)
        except ValueError:
            return False
        if when - now <= LEASE[rec.kind]:
            return False
    rec.status, rec.due = "retrying", due_key(rec.kind, now, h)
    # the record first: a due key whose record doesn't name it is dropped as stale
    save(store, rec)
    put_json(store, rec.due, {"h": h, "kind": rec.kind})
    if old is not None:
        store.delete(old)
    return True


def _index_cache(store: Store) -> dict[str, Any]:
    """The summary's cache of each index record's metadata (kind, status)."""
    try:
        state = json.loads(store.get(STATE_KEY) or b"{}")
    except ValueError:
        return {}
    index = state.get("index")
    return index if isinstance(index, dict) else {}


def _validate(req: Any) -> str | None:
    if not isinstance(req, dict):
        return "not an object"
    if req.get("scope") == "message":
        return None if _SHA.match(str(req.get("sha", ""))) else "bad sha"
    if req.get("scope") == "urls":
        if req.get("kind") not in KINDS:
            return "bad kind"
        return None if req.get("status") in RETRYABLE else "bad status"
    return "bad scope"


def _apply_message(
    store: Store, sha: str, now: datetime, relink: Relinker | None, deadline: float,
    result: dict[str, Any],
) -> bool:  # fmt: skip
    """Returns False if it ran out of time and should be tried again."""
    status = store.get(status_key(sha))
    if status is None:
        result["error"] = "message not processed yet"
        return True
    source = _BUCKET.sub("", str(json.loads(status).get("source", "")))
    raw = store.get(source) if source.startswith("incoming/") else None
    if relink is not None and raw is not None:
        got = relink(raw, deadline)
        result["relinked"] = {k: v for k, v in asdict(got).items() if v}
        if got.out_of_time:
            return False
    doc = load_links(store, sha) or {}
    hashes = sorted({str(r["h"]) for r in doc.get("links", []) if r.get("h")})
    done = sum(reschedule(store, h, now) for h in hashes)
    result["rescheduled"], result["left"] = done, len(hashes) - done
    return True


def _apply_urls(store: Store, kind: str, status: str, now: datetime,
                result: dict[str, Any]) -> None:  # fmt: skip
    hashes = [
        key.removeprefix("captures/index/").removesuffix(".json")
        for key, (_, meta) in _index_cache(store).items()
        if meta.get("kind") == kind and meta.get("status") == status
    ]
    done = sum(reschedule(store, h, now) for h in hashes)
    result["rescheduled"], result["left"] = done, len(hashes) - done


def apply_requests(
    store: Store, keys: list[str], now: datetime, relink: Relinker | None, *,
    budget_seconds: float = 300.0,
) -> list[dict[str, Any]]:  # fmt: skip
    """Apply the requests at ``keys`` (from a listing of ``REQUESTS``), oldest
    first, within ``budget_seconds``; returns what each did. A request applied,
    or invalid, is deleted; one that ran out of time stays for the next run."""
    deadline = time.monotonic() + budget_seconds
    results = []
    for key in sorted(k for k in keys if _REQUEST.match(k)):
        if time.monotonic() >= deadline:
            break
        data = store.get(key)
        if data is None:
            continue
        try:
            req = json.loads(data)
        except ValueError:
            req = None
        result: dict[str, Any] = {"applied_at": _iso(now)}
        error = _validate(req)
        finished = True
        if error is not None:
            result["error"] = f"invalid request: {error}"
        else:
            result.update({k: req[k] for k in ("scope", "sha", "kind", "status", "requested_at")
                           if k in req})  # fmt: skip
            if req["scope"] == "message":
                finished = _apply_message(store, req["sha"], now, relink, deadline, result)
            else:
                _apply_urls(store, req["kind"], req["status"], now, result)
        if finished:
            store.delete(key)
        results.append(result)
    if results:
        try:
            kept = json.loads(store.get(RESULTS_KEY) or b"[]")
        except ValueError:
            kept = []
        kept = (results[::-1] + (kept if isinstance(kept, list) else []))[:RESULTS_KEPT]
        put_json(store, RESULTS_KEY, kept)
    return results
