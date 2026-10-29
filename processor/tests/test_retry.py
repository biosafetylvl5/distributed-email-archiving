from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from dea_capture import retry
from dea_capture.capture import (
    RETRY_AFTER,
    due_key,
    index_key,
    queue,
    run,
    save,
    url_hash,
)
from dea_capture.classify import tracking_vendor
from dea_capture.fetch import HostPacer
from dea_capture.guard import GuardedFetcher, IPAddress, Response
from dea_capture.inventory import Inventory
from dea_capture.issue import process_issue, relink_issue, status_key
from dea_capture.links import Relinked, captures_key
from dea_capture.store import LocalStore
from dea_capture.summary import STATE_KEY

from .conftest import load
from .test_capture import PDF, T0, Web, _due_keys, _guarded, _record

NOW = T0 + timedelta(days=2)
SOURCE = "incoming/archive/2026-10-01/x.eml"


def _retrying(store: LocalStore, url: str = PDF, due_in: timedelta = timedelta(days=7)) -> str:
    """A record that failed and is due again ``due_in`` from NOW."""
    queue(store, url, "direct", T0)
    h = url_hash(url)
    rec = _record(store, url)
    store.delete(rec.due or "")
    rec.status, rec.due = "retrying", due_key("direct", NOW + due_in, h)
    save(store, rec)
    store.put(rec.due, b"{}", "application/json")
    return h


def test_reschedule_makes_a_far_off_retry_due_now(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    h = _retrying(store)
    assert retry.reschedule(store, h, NOW)
    rec = _record(store, PDF)
    assert (rec.status, rec.due) == ("retrying", due_key("direct", NOW, h))
    assert rec.due is not None and _due_keys(tmp_path) == [rec.due]
    # and the capture stage takes it
    web = Web(bodies={PDF: ("application/pdf", b"%PDF")})
    assert run(store, rec.due, _guarded(web), now=NOW, pacer=HostPacer(0)).status == "ok"


def test_reschedule_leaves_alone_what_may_be_running_or_is_done(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    h = _retrying(store, due_in=timedelta(minutes=50))  # could be a lease
    assert not retry.reschedule(store, h, NOW)
    other = "https://docs.example.org/other.pdf"
    queue(store, other, "direct", T0)
    assert not retry.reschedule(store, url_hash(other), NOW)  # queued, not failed
    assert not retry.reschedule(store, "0" * 64, NOW)  # no such record


def test_a_failed_url_is_retried_once_more(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    queue(store, PDF, "direct", T0)
    web = Web(status={PDF: 404})
    now = T0
    while (rec := _record(store, PDF)).status != "failed":
        assert rec.due is not None
        run(store, rec.due, _guarded(web), now=now, pacer=HostPacer(0))
        now += RETRY_AFTER[min(len(rec.attempts), len(RETRY_AFTER) - 1)]
    attempts = len(rec.attempts)
    assert retry.reschedule(store, url_hash(PDF), now)
    rec = _record(store, PDF)
    assert rec.status == "retrying" and rec.due is not None
    run(store, rec.due, _guarded(web), now=now, pacer=HostPacer(0))
    rec = _record(store, PDF)
    assert (rec.status, rec.due, len(rec.attempts)) == ("failed", None, attempts + 1)


def _request(store: LocalStore, name: str, body: Any) -> str:
    key = f"captures/retry/20261002T000000Z-{name}.json"
    store.put(key, json.dumps(body).encode(), "application/json")
    return key


def test_apply_requests_by_kind_and_status(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    h = _retrying(store)
    store.put(STATE_KEY, json.dumps({"index": {
        index_key(h): ["e", {"kind": "direct", "status": "retrying"}],
        index_key("1" * 64): ["e", {"kind": "page", "status": "retrying"}],
    }}).encode(), "application/json")  # fmt: skip
    keys = [
        _request(store, "aaaaaaaa", {"scope": "urls", "kind": "direct", "status": "retrying"}),
        _request(store, "bbbbbbbb", {"scope": "urls", "kind": "direct", "status": "ok"}),
        _request(store, "cccccccc", {"scope": "nope"}),
    ]
    got = retry.apply_requests(store, [*keys, "captures/retry/junk.json"], NOW, None)
    want = {"scope": "urls", "kind": "direct", "status": "retrying", "rescheduled": 1, "left": 0}
    assert got[0] == {"applied_at": got[0]["applied_at"], **want}
    assert [r.get("error") for r in got[1:]] == ["invalid request: bad status",
                                                 "invalid request: bad scope"]  # fmt: skip
    assert not any(store.exists(k) for k in keys)
    assert _record(store, PDF).due == due_key("direct", NOW, h)
    kept = json.loads(store.get(retry.RESULTS_KEY) or b"")
    assert kept[0] == got[-1] and len(kept) == 3


class RateLimitedTrackers(Web):
    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
        if tracking_vendor(url):
            self.requested.append(url)
            return Response(url, 429, {}, b"")
        return super().get(url, addresses=addresses)


def _rate_limited_issue(store: LocalStore) -> tuple[bytes, str]:
    raw = load("substack.eml")
    web = RateLimitedTrackers()

    def make(inv: Inventory) -> GuardedFetcher:
        return _guarded(web, inv.refused_urls())

    process_issue(raw, store, make, source=f"s3://bucket/{SOURCE}", pacer=HostPacer(0),
                  make_resolver=make)  # fmt: skip
    store.put(SOURCE, raw, "message/rfc822")
    sha = hashlib.sha256(raw).hexdigest()
    assert int((store.metadata(status_key(sha)) or {})["links_unresolved"]) > 0
    return raw, sha


def _resolver(web: Web) -> Any:
    def make(inv: Inventory) -> GuardedFetcher:
        return _guarded(web, inv.refused_urls())

    return make


def test_relink_follows_the_failed_redirects_again(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    raw, sha = _rate_limited_issue(store)
    unresolved = int((store.metadata(status_key(sha)) or {})["links_unresolved"])
    web = Web(trackers_land=True)
    got = relink_issue(raw, store, _resolver(web), now=NOW, pacer=HostPacer(0))
    assert (got.tried, got.resolved, got.rate_limited) == (unresolved, unresolved, False)
    meta = store.metadata(status_key(sha)) or {}
    assert (meta["links_unresolved"], meta["links_state"]) == ("0", "done")
    doc = json.loads(store.get(captures_key(sha)) or b"")
    assert "relinked_at" in doc
    for r in doc["links"]:
        if r["decision"] == "resolve":
            assert r["outcome"] == "ok" and store.exists(index_key(r["h"]))
    # only the failed ones were requested again
    assert all(tracking_vendor(u) or "landing" in u for u in web.requested)
    assert len([u for u in web.requested if tracking_vendor(u)]) == unresolved


def test_relink_stops_at_a_429_and_on_time(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    raw, sha = _rate_limited_issue(store)
    before = store.get(captures_key(sha))
    web = RateLimitedTrackers()
    got = relink_issue(raw, store, _resolver(web), now=NOW, pacer=HostPacer(0))
    assert (got.tried, got.rate_limited) == (1, True) and len(web.requested) == 1
    assert store.get(captures_key(sha)) == before
    got = relink_issue(raw, store, _resolver(web), now=NOW, pacer=HostPacer(0),
                       deadline=time.monotonic() - 1)  # fmt: skip
    assert (got.tried, got.out_of_time) == (0, True)


def test_a_message_request_relinks_then_retries_its_urls(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    raw, sha = _rate_limited_issue(store)
    web = Web(trackers_land=True)
    calls: list[float] = []

    def relink(raw_: bytes, deadline: float) -> Relinked:
        calls.append(deadline)
        assert raw_ == raw
        return relink_issue(raw_, store, _resolver(web), now=NOW, pacer=HostPacer(0),
                            deadline=deadline)  # fmt: skip

    key = _request(store, "dddddddd", {"scope": "message", "sha": sha})
    [got] = retry.apply_requests(store, [key], NOW, relink)
    assert len(calls) == 1 and not store.exists(key)
    assert got["relinked"]["resolved"] > 0
    # freshly queued URLs aren't retrying, so nothing to reschedule
    assert got["rescheduled"] == 0 and got["left"] > 0


def test_a_message_request_out_of_time_waits_for_the_next_run(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    _, sha = _rate_limited_issue(store)
    key = _request(store, "eeeeeeee", {"scope": "message", "sha": sha})
    got = retry.apply_requests(
        store, [key], NOW, lambda raw, d: Relinked(tried=1, out_of_time=True)
    )
    assert store.exists(key) and got[0]["relinked"] == {"tried": 1, "out_of_time": True}
    # an unknown message is done with, with a note
    other = _request(store, "ffffffff", {"scope": "message", "sha": "e" * 64})
    [got2] = retry.apply_requests(store, [other], NOW, None)
    assert got2["error"] == "message not processed yet" and not store.exists(other)
