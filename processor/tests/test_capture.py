from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from dea_capture.capture import (
    RETRY_AFTER,
    CaptureRecord,
    due_key,
    guess_kind,
    index_key,
    parse_due_key,
    queue,
    run,
    url_hash,
    wayback_url,
)
from dea_capture.classify import classify, tracking_vendor
from dea_capture.fetch import FetchError, HostPacer, TooLarge
from dea_capture.guard import GuardedFetcher, IPAddress, Response
from dea_capture.inventory import Inventory
from dea_capture.issue import process_issue, status_key
from dea_capture.links import _link_record, captures_key
from dea_capture.page import PageOutcome
from dea_capture.resolve import resolve
from dea_capture.store import LocalStore

from .conftest import EML_FIXTURES, FORBIDDEN_MARKERS, fake_resolver, load

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PDF = "https://docs.example.org/report.pdf"


@dataclass
class Web:
    """A scripted web: redirects, statuses, bodies. Unknown URLs get an HTML page."""

    redirects: dict[str, str] = field(default_factory=dict)
    status: dict[str, int] = field(default_factory=dict)
    bodies: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    huge: set[str] = field(default_factory=set)
    requested: list[str] = field(default_factory=list)
    # trackers not in ``redirects`` send the click to a landing page of their own
    trackers_land: bool = False
    # URLs that never answer plain HTTP, and the error they give
    down: dict[str, str] = field(default_factory=dict)

    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
        assert not any(m in url for m in FORBIDDEN_MARKERS), url
        self.requested.append(url)
        if url in self.huge:
            raise TooLarge(10)
        if url in self.down:
            raise FetchError(self.down[url])
        if url in self.redirects:
            return Response(url, 302, {"location": self.redirects[url]}, b"")
        if self.trackers_land and tracking_vendor(url):
            land = f"https://www.example.org/landing/{url_hash(url)[:12]}"
            return Response(url, 302, {"location": land}, b"")
        ctype, body = self.bodies.get(url, ("text/html", b"<html></html>"))
        return Response(url, self.status.get(url, 200), {"content-type": ctype}, body)


def _guarded(web: Web, refused: frozenset[str] = frozenset()) -> GuardedFetcher:
    return GuardedFetcher(web, resolve=fake_resolver, refused_urls=refused)


def _record(store: LocalStore, canonical: str) -> CaptureRecord:
    data = store.get(index_key(url_hash(canonical)))
    assert data is not None
    return CaptureRecord.load(data)


def _due_keys(root: Path) -> list[str]:
    due = root / "captures" / "due"
    return sorted(str(p.relative_to(root)) for p in due.rglob("*.json")) if due.exists() else []


# ── resolve ──────────────────────────────────────────────────────────────────


def test_resolve_follows_the_chain_to_the_landing_page() -> None:
    a, b = "https://click.example.com/r/1", "https://www.example.org/post?utm_source=x"
    web = Web(redirects={a: b})
    r = resolve(a, _guarded(web), HostPacer(0))
    assert (r.outcome, r.chain, r.final) == ("ok", [a, b], b)
    assert r.canonical == "https://www.example.org/post"
    assert (r.http_status, r.content_type) == (200, "text/html")


def test_resolve_stops_before_a_login_hop() -> None:
    a = "https://click.example.com/r/2"
    b = "https://www.example.org/post?token=abc"
    web = Web(redirects={a: b})
    r = resolve(a, _guarded(web), HostPacer(0))
    assert web.requested == [a]
    assert r.outcome == "ok" and r.stopped_at_auth
    assert r.canonical == "https://www.example.org/post"


def test_resolve_refuses_a_hop_onto_unsubscribe() -> None:
    a = "https://click.example.com/r/3"
    web = Web(redirects={a: "https://www.example.org/unsubscribe?e=1"})
    r = resolve(a, _guarded(web), HostPacer(0))
    assert r.outcome == "refused" and r.canonical is None
    assert web.requested == [a]


@pytest.mark.parametrize("status", [200, 403, 429])
def test_a_tracker_that_does_not_redirect_is_never_the_canonical(status: int) -> None:
    a = "https://substack.com/redirect/3f2a-uuid?j=eyJ1IjoxfQ.sig"
    web = Web(status={a: status})
    r = resolve(a, _guarded(web), HostPacer(0))
    assert (r.outcome, r.canonical, r.final, r.http_status) == ("failed", None, None, status)
    # nor is a tracker further down the chain
    b = "https://click.example.com/r/9"
    web = Web(redirects={a: b}, status={b: 429})
    r = resolve(a, _guarded(web), HostPacer(0))
    assert (r.outcome, r.canonical) == ("failed", None)


def test_resolve_reports_loops_and_long_chains() -> None:
    a, b = "https://click.example.com/r/a", "https://click.example.com/r/b"
    assert resolve(a, _guarded(Web(redirects={a: b, b: a})), HostPacer(0)).outcome == "failed"
    chain = {f"https://x.example.com/{i}": f"https://x.example.com/{i + 1}" for i in range(20)}
    r = resolve("https://x.example.com/0", _guarded(Web(redirects=chain)), HostPacer(0), max_hops=3)
    assert r.outcome == "failed" and "redirects" in (r.error or "")


TRACKER = "https://substack.com/redirect/3f2a-uuid?j=eyJ1IjoxfQ.sig"


def test_a_destination_that_wont_answer_plain_http_is_left_to_the_browser() -> None:
    b = "https://www.example.org/news/story/?utm_source=x"
    web = Web(redirects={TRACKER: b}, down={b: "203.0.113.5: ReadTimeoutError"})
    r = resolve(TRACKER, _guarded(web), HostPacer(0))
    assert (r.outcome, r.browser, r.final) == ("ok", True, b)
    assert r.canonical == "https://www.example.org/news/story/"
    assert r.error == "203.0.113.5: ReadTimeoutError"
    assert r.http_status is None


def test_a_cookie_check_loop_leaves_the_landing_page_to_the_browser() -> None:
    b = "https://www.example.org/post?utm_medium=email"
    check, again = "https://www.example.org/check", "https://www.example.org/check?x=1"
    web = Web(redirects={TRACKER: b, b: check, check: again, again: check})
    r = resolve(TRACKER, _guarded(web), HostPacer(0))
    assert (r.outcome, r.browser, r.error) == ("ok", True, "redirect loop")
    assert r.canonical == "https://www.example.org/post"


def test_a_tracker_that_wont_answer_is_never_left_to_the_browser() -> None:
    web = Web(down={TRACKER: "timeout"})
    r = resolve(TRACKER, _guarded(web), HostPacer(0))
    assert (r.outcome, r.browser, r.canonical) == ("failed", False, None)
    # nor one further down the chain
    b = "https://click.example.com/r/9"
    web = Web(redirects={TRACKER: b}, down={b: "timeout"})
    r = resolve(TRACKER, _guarded(web), HostPacer(0))
    assert (r.outcome, r.browser, r.canonical) == ("failed", False, None)


def test_a_file_left_to_the_browser_is_queued_as_a_page(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    pdf = "https://www.example.org/bill.pdf"
    web = Web(redirects={TRACKER: pdf}, down={pdf: "SSLError"})
    rec = _link_record(classify(TRACKER), store, _guarded(web), HostPacer(0), T0, {})
    assert (rec.outcome, rec.browser, rec.kind, rec.queued) == ("ok", True, "page", "new")
    assert _record(store, pdf).kind == "page"


# ── queueing ─────────────────────────────────────────────────────────────────


def test_due_keys_round_trip() -> None:
    h = url_hash(PDF)
    key = due_key("direct", T0, h)
    assert key == f"captures/due/direct/20261001T120000Z-{h}.json"
    assert parse_due_key(key) == ("direct", T0, h)
    with pytest.raises(ValueError):
        parse_due_key("captures/due/direct/nope.json")


def test_kind_is_guessed_from_url_and_type() -> None:
    assert guess_kind(PDF) == "direct"
    assert guess_kind("https://www.youtube.com/watch?v=x") == "media"
    assert guess_kind("https://cdn.example.com/a.mp3") == "media"
    assert guess_kind("https://www.example.org/post") == "page"
    assert guess_kind("https://www.example.org/dl", "application/pdf") == "direct"
    assert guess_kind("https://www.example.org/a.pdf", "text/html; charset=utf-8") == "page"


def test_a_url_is_queued_once_across_issues(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert queue(store, PDF, "direct", T0) == "new"
    assert queue(store, PDF, "direct", T0 + timedelta(minutes=5)) == "queued"
    assert _due_keys(tmp_path) == [due_key("direct", T0, url_hash(PDF))]
    assert store.metadata(index_key(url_hash(PDF))) == {
        "status": "queued", "kind": "direct", "attempts": "0",
        "updated_at": "2026-10-01T12:00:00+00:00",
    }  # fmt: skip


def test_a_lost_due_key_is_put_back(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    queue(store, PDF, "direct", T0)
    store.delete(due_key("direct", T0, url_hash(PDF)))
    queue(store, PDF, "direct", T0)
    assert _due_keys(tmp_path) == [due_key("direct", T0, url_hash(PDF))]


# ── capture-direct ───────────────────────────────────────────────────────────


def cdx_api(url: str, queued: str = "20261001120000") -> str:
    return (
        "https://web.archive.org/cdx/search/cdx?url="
        + quote(url, safe="")
        + f"&output=json&fl=timestamp%2Coriginal&filter=statuscode%3A200&closest={queued}"
        "&sort=closest&limit=1"
    )


def cdx_answer(ts: str, url: str) -> bytes:
    return json.dumps([["timestamp", "original"], [ts, url]]).encode()


def _queued(tmp_path: Path, url: str = PDF) -> tuple[LocalStore, str]:
    store = LocalStore(tmp_path)
    queue(store, url, "direct", T0)
    return store, due_key("direct", T0, url_hash(url))


def test_direct_capture_stores_body_and_headers(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)
    web = Web(bodies={PDF: ("application/pdf", b"%PDF-1.7 hello")})
    result = run(store, key, _guarded(web), now=T0, pacer=HostPacer(0))
    assert (result.outcome, result.status) == ("ok", "ok")
    rec = _record(store, PDF)
    assert rec.status == "ok" and rec.due is None and rec.capture
    assert store.get(rec.capture + "original.pdf") == b"%PDF-1.7 hello"
    meta = json.loads(store.get(rec.capture + "capture.json") or b"")
    assert meta["sha256"] == hashlib.sha256(b"%PDF-1.7 hello").hexdigest()
    assert meta["headers"]["content-type"] == "application/pdf"
    assert _due_keys(tmp_path) == []
    # the due key, run again by a restarted poller, is a no-op
    store.put(key, b"{}", "application/json")
    assert run(store, key, _guarded(web), now=T0, pacer=HostPacer(0)).outcome == "stale"
    assert web.requested == [PDF]


@pytest.mark.parametrize("kind", ["direct", "page"])
def test_a_queued_tracker_url_is_refused_unrequested(tmp_path: Path, kind: str) -> None:
    tracker = "https://substack.com/redirect/3f2a-uuid?j=eyJ1IjoxfQ.sig"
    store = LocalStore(tmp_path)
    queue(store, tracker, kind, T0)  # type: ignore[arg-type]
    web, wayback = Web(), Web()

    def crawler(url: str) -> PageOutcome:
        raise AssertionError(url)

    key = due_key(kind, T0, url_hash(tracker))  # type: ignore[arg-type]
    result = run(store, key, _guarded(web), now=T0, pacer=HostPacer(0),
                 wayback_fetcher=_guarded(wayback), page_crawler=crawler)  # fmt: skip
    assert (result.outcome, result.status) == ("refused", "refused")
    assert (web.requested, wayback.requested) == ([], [])
    assert _due_keys(tmp_path) == []


def test_failures_retry_on_the_schedule_then_fall_back_to_wayback(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)
    web = Web(status={PDF: 404})
    now = T0
    for i, delay in enumerate(RETRY_AFTER):
        result = run(store, key, _guarded(web), now=now, pacer=HostPacer(0))
        assert (result.outcome, result.status, result.attempts) == ("failed", "retrying", i + 1)
        now += delay
        [key] = _due_keys(tmp_path)
        assert parse_due_key(key)[1] == now

    ts = "20260915000000"
    raw = wayback_url(ts, PDF, "id_")
    assert raw == f"https://web.archive.org/web/{ts}id_/{PDF}"
    wayback = Web(bodies={raw: ("application/pdf", b"%PDF old"),
                          cdx_api(PDF): ("application/json", cdx_answer(ts, PDF))})  # fmt: skip
    result = run(
        store, key, _guarded(web), now=now, pacer=HostPacer(0), wayback_fetcher=_guarded(wayback)
    )
    assert (result.status, result.attempts) == ("failed", len(RETRY_AFTER) + 1)
    rec = _record(store, PDF)
    assert rec.wayback is not None
    assert rec.wayback == {"timestamp": ts, "capture": rec.wayback["capture"], "via": "raw"}
    assert store.get(rec.wayback["capture"] + "original.pdf") == b"%PDF old"
    assert _due_keys(tmp_path) == []
    assert (store.metadata(index_key(rec.h)) or {})["wayback"] == ts


def test_a_failing_file_gets_its_snapshot_after_two_failures(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)
    web = Web(status={PDF: 500})
    ts = "20260915000000"
    wayback = Web(bodies={wayback_url(ts, PDF, "id_"): ("application/pdf", b"%PDF old"),
                          cdx_api(PDF): ("application/json", cdx_answer(ts, PDF))})  # fmt: skip
    kw = {"pacer": HostPacer(0), "wayback_fetcher": _guarded(wayback)}
    run(store, key, _guarded(web), now=T0, **kw)  # type: ignore[arg-type]
    assert wayback.requested == [] and _record(store, PDF).wayback is None
    [key] = _due_keys(tmp_path)
    run(store, key, _guarded(web), now=T0 + RETRY_AFTER[0], **kw)  # type: ignore[arg-type]
    rec = _record(store, PDF)
    assert rec.status == "retrying" and rec.wayback is not None
    assert len(wayback.requested) == 2
    # later failures keep retrying, without asking the Wayback Machine again
    [key] = _due_keys(tmp_path)
    run(store, key, _guarded(web), now=T0 + RETRY_AFTER[0] + RETRY_AFTER[1], **kw)  # type: ignore[arg-type]
    assert len(wayback.requested) == 2 and _record(store, PDF).status == "retrying"


def test_a_file_the_site_refuses_gets_its_snapshot_at_once(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)
    wayback = Web(bodies={cdx_api(PDF): ("application/json", b"[]")})  # no snapshot
    run(store, key, _guarded(Web(status={PDF: 403})), now=T0, pacer=HostPacer(0),
        wayback_fetcher=_guarded(wayback))  # fmt: skip
    assert wayback.requested == [cdx_api(PDF)]
    assert _record(store, PDF).wayback is None


def test_html_is_handed_to_the_page_stage(tmp_path: Path) -> None:
    store, key = _queued(tmp_path, "https://www.example.org/file.pdf")
    result = run(store, key, _guarded(Web()), now=T0, pacer=HostPacer(0))
    assert result.outcome == "rerouted"
    rec = _record(store, "https://www.example.org/file.pdf")
    assert (rec.kind, rec.status) == ("page", "queued")
    assert _due_keys(tmp_path) == [due_key("page", T0, rec.h)]


def test_a_link_that_lands_on_a_video_page_goes_to_the_media_stage(tmp_path: Path) -> None:
    link = "https://www.example.org/watch.pdf"
    video = "https://www.youtube.com/watch?v=abc"
    store, key = _queued(tmp_path, link)
    result = run(store, key, _guarded(Web(redirects={link: video})), now=T0, pacer=HostPacer(0))
    assert result.outcome == "rerouted"
    rec = _record(store, link)
    assert (rec.kind, rec.status, rec.capture) == ("media", "queued", None)
    assert _due_keys(tmp_path) == [due_key("media", T0, rec.h)]


def test_stored_headers_leave_out_cookies(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)

    class Cookied(Web):
        def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
            r = super().get(url, addresses=addresses)
            return Response(r.url, r.status, {**r.headers, "Set-Cookie": "sid=1"}, r.body)

    web = Cookied(bodies={PDF: ("application/pdf", b"%PDF-1.7")})
    run(store, key, _guarded(web), now=T0, pacer=HostPacer(0))
    rec = _record(store, PDF)
    meta = json.loads(store.get(f"{rec.capture}capture.json") or b"")
    assert "content-type" in meta["headers"]
    assert not [k for k in meta["headers"] if k.lower() == "set-cookie"]


def test_too_large_is_deferred_and_refused_is_final(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)
    run(store, key, _guarded(Web(huge={PDF})), now=T0, pacer=HostPacer(0))
    assert _record(store, PDF).status == "deferred"

    login = "https://www.example.org/x.pdf"
    queue(store, login, "direct", T0)
    web = Web(redirects={login: "https://www.example.org/account/delete"})
    run(store, due_key("direct", T0, url_hash(login)), _guarded(web), now=T0, pacer=HostPacer(0))
    assert _record(store, login).status == "refused"
    assert _due_keys(tmp_path) == []


def test_a_capture_that_dies_leaves_a_lease(tmp_path: Path) -> None:
    store, key = _queued(tmp_path)

    class Crash(Web):
        def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(store, key, _guarded(Crash()), now=T0, pacer=HostPacer(0))
    [lease] = _due_keys(tmp_path)
    assert parse_due_key(lease)[1] == T0 + timedelta(hours=1)
    assert _record(store, PDF).due == lease
    web = Web(bodies={PDF: ("application/pdf", b"%PDF")})
    assert run(store, lease, _guarded(web), now=T0, pacer=HostPacer(0)).status == "ok"


# ── the issue stage's link half ──────────────────────────────────────────────


def _issue(raw: bytes, store: LocalStore, web: Web, **kw: object) -> object:
    def make(inv: Inventory) -> GuardedFetcher:
        return _guarded(web, inv.refused_urls())

    return process_issue(
        raw, store, make, source="s3://bucket/incoming/archive/x.eml",
        pacer=HostPacer(0), **kw,  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_links_are_resolved_and_queued(path: Path, tmp_path: Path) -> None:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    store = LocalStore(tmp_path)
    web = Web(trackers_land=True)
    result = _issue(raw, store, web, make_resolver=lambda inv: _guarded(web, inv.refused_urls()))
    doc = json.loads(store.get(captures_key(sha)) or b"")
    assert doc["links"], "fixture should have links to capture"
    hashes = {r["h"] for r in doc["links"] if r["h"]}
    assert hashes
    for h in hashes:
        assert store.exists(index_key(h))
    # a tracker that answers without redirecting is unresolved, not queued
    assert not [r for r in doc["links"] if r["canonical"] and tracking_vendor(r["canonical"])]
    status = store.metadata(status_key(sha)) or {}
    assert status["links_state"] in {"done", "partial"}
    assert int(status["captures_queued"]) == len(hashes)
    # nothing that identifies the reader is in a shared object
    for h in hashes:
        rec = json.loads(store.get(index_key(h)) or b"")
        assert not any(m in rec["canonical"] for m in FORBIDDEN_MARKERS)
    assert "http" not in result.line()  # type: ignore[attr-defined]


def test_done_issues_get_their_links_resolved_once(tmp_path: Path) -> None:
    raw = load("generic.eml")
    sha = hashlib.sha256(raw).hexdigest()
    store = LocalStore(tmp_path)
    web = Web()
    _issue(raw, store, web)
    assert not store.exists(captures_key(sha))
    assert (store.metadata(status_key(sha)) or {})["links_state"] == "pending"
    embeds_requested = len(web.requested)

    resolver = Web()

    def make_resolver(inv: Inventory) -> GuardedFetcher:
        return _guarded(resolver, inv.refused_urls())

    result = _issue(raw, store, web, make_resolver=make_resolver)
    assert not result.skipped  # type: ignore[attr-defined]
    assert len(web.requested) == embeds_requested
    assert store.exists(captures_key(sha))
    assert (store.metadata(status_key(sha)) or {})["links_state"] in {"done", "partial"}
    before = len(resolver.requested)
    assert _issue(raw, store, web, make_resolver=make_resolver).skipped  # type: ignore[attr-defined]
    assert len(resolver.requested) == before


# ── the due poller ───────────────────────────────────────────────────────────


def test_due_poller_waits_for_the_time_in_the_key() -> None:
    pytest.importorskip("courier")
    from dea_capture.courier_plugin import S3DuePoller, is_due

    h = url_hash(PDF)
    assert is_due(due_key("direct", T0, h), now=T0)
    assert not is_due(due_key("direct", T0 + timedelta(seconds=1), h), now=T0)
    assert not is_due("captures/due/direct/junk.json", now=T0)
    assert S3DuePoller.name == "s3_due_poller"


def test_resolve_never_requests_a_hop_with_the_readers_address() -> None:
    a = "https://click.example.com/r/4"
    b = "https://substack.com/signup?freeSignupEmail=reader%40example.com&r=abc"
    web = Web(redirects={a: b})
    r = resolve(a, _guarded(web), HostPacer(0))
    assert web.requested == [a]
    assert r.stopped_at_auth
    assert r.canonical == "https://substack.com/signup"
