"""Subscriptions' capture size limits."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dea_capture import gallery, guard, media
from dea_capture.capture import (
    LIMITS_KEY,
    CaptureRecord,
    index_key,
    load_limits,
    queue,
    run,
    size_limit,
    url_hash,
)
from dea_capture.guard import GuardedFetcher
from dea_capture.links import captures_key
from dea_capture.media import Grab, MediaOutcome, grab_media, judge_probe
from dea_capture.page import PageOutcome
from dea_capture.store import LocalStore, put_json

from .conftest import EML_FIXTURES, RecordingFetcher, fake_resolver
from .test_capture import Web, _issue
from .test_capture import _guarded as _web_guarded

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
URL = "https://news.example.com/story"
MiB = 2**20


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real = guard.check_destination

    def check(url: str, resolve: object = None, extra_blocked: Any = ()) -> Any:
        return real(url, fake_resolver, extra_blocked)

    monkeypatch.setattr(media, "check_destination", check)
    monkeypatch.setattr(gallery, "check_destination", check)


def _limits(store: LocalStore, **by_sub: int) -> None:
    put_json(store, LIMITS_KEY, {"limits": {k.replace("__", "/"): v for k, v in by_sub.items()}})


def _rec(store: LocalStore, url: str = URL) -> CaptureRecord:
    return CaptureRecord.load(store.get(index_key(url_hash(url))) or b"")


def _guarded() -> GuardedFetcher:
    return GuardedFetcher(RecordingFetcher(), resolve=fake_resolver)


def test_the_most_permissive_limit_applies() -> None:
    limits = {"ana/a": 10, "ana/b": 30}
    assert size_limit(["ana/a"], limits) == 10
    assert size_limit(["ana/a", "ana/b"], limits) == 30
    assert size_limit(["ana/a", "ana/c"], limits) is None  # c has none
    assert size_limit(None, limits) is None  # a message with no subscription linked it
    assert size_limit([], limits) is None


def test_load_limits_skips_junk(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert load_limits(store) == {}
    put_json(store, LIMITS_KEY, {"limits": {"a/b": 5, "a/c": "5", "a/d": 0, "a/e": True}})
    assert load_limits(store) == {"a/b": 5}
    store.put(LIMITS_KEY, b"not json", "application/json")
    assert load_limits(store) == {}


def test_queue_notes_each_subscription(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert queue(store, URL, "page", T0, "ana/a") == "new"
    assert queue(store, URL, "page", T0, "ana/b") == "queued"
    assert queue(store, URL, "page", T0, "ana/a") == "queued"
    assert _rec(store).subs == ["ana/a", "ana/b"]
    queue(store, URL, "page", T0, None)
    assert _rec(store).subs is None  # for good: no limit applies now
    queue(store, URL, "page", T0, "ana/c")
    assert _rec(store).subs is None


def _crawler(size: int) -> Any:
    return lambda url: PageOutcome("ok", 200, files={"page.wacz": b"w" * size})


def test_a_page_over_the_limit_is_deferred(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    _limits(store, ana__a=2 * MiB)
    queue(store, URL, "page", T0, "ana/a")
    r = run(store, _rec(store).due or "", _guarded(), now=T0, page_crawler=_crawler(3 * MiB))
    assert (r.outcome, r.status) == ("deferred", "deferred")
    rec = _rec(store)
    assert rec.attempts[-1].limit == 2 * MiB
    assert rec.attempts[-1].error == "3 MiB, over the subscription's 2 MiB limit"
    assert rec.capture is None and not list((tmp_path / "captures").glob("??/*/*/page.wacz"))

    # the same subscription linking it again changes nothing
    assert queue(store, URL, "page", T0, "ana/a") == "deferred"
    # one with a higher limit queues it again, and it fits now
    _limits(store, ana__a=2 * MiB, ana__b=4 * MiB)
    later = T0 + timedelta(hours=1)
    assert queue(store, URL, "page", later, "ana/b") == "queued"
    rec = _rec(store)
    assert rec.status == "queued" and rec.due and store.exists(rec.due)
    r = run(store, rec.due, _guarded(), now=later, page_crawler=_crawler(3 * MiB))
    assert r.status == "ok"


def test_a_lower_limit_doesnt_requeue(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    _limits(store, ana__a=2 * MiB, ana__b=1 * MiB)
    queue(store, URL, "page", T0, "ana/a")
    run(store, _rec(store).due or "", _guarded(), now=T0, page_crawler=_crawler(3 * MiB))
    assert queue(store, URL, "page", T0, "ana/b") == "deferred"
    assert _rec(store).subs == ["ana/a", "ana/b"]


def test_no_limit_without_a_subscription(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    _limits(store, ana__a=1)
    queue(store, URL, "page", T0)
    r = run(store, _rec(store).due or "", _guarded(), now=T0, page_crawler=_crawler(3 * MiB))
    assert r.status == "ok"


def test_a_direct_file_over_the_limit_is_deferred(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    url = "https://files.example.com/report.pdf"
    web = Web(bodies={url: ("application/pdf", b"%" * (2 * MiB))})
    _limits(store, ana__a=MiB)
    queue(store, url, "direct", T0, "ana/a")
    r = run(store, _rec(store, url).due or "", _web_guarded(web), now=T0)
    assert r.status == "deferred"
    assert _rec(store, url).attempts[-1].limit == MiB


# ── media and gallery ───────────────────────────────────────────────────────

INFO: dict[str, Any] = {"id": "x", "title": "t", "requested_formats": [{"filesize": 50 * MiB}]}


def test_judge_probe_with_a_limit() -> None:
    assert judge_probe(INFO) is None
    assert judge_probe(INFO, 100 * MiB) is None
    assert judge_probe(INFO, 10 * MiB) == (
        "deferred", "about 50 MiB, over the subscription's 10 MiB limit")  # fmt: skip
    # a limit above the stage's own cap doesn't raise it
    size = media.MAX_BYTES + 1
    big = {**INFO, "requested_formats": [{"filesize": size}]}
    why = f"about {size // MiB} MiB, over the {media.MAX_BYTES // MiB} MiB cap"
    assert judge_probe(big, 2 * media.MAX_BYTES) == ("deferred", why)


def test_ytdlp_downloads_no_more_than_the_limit(tmp_path: Path) -> None:
    args = media.ytdlp_args(URL, tmp_path, 1, probe=False, max_bytes=5 * MiB)
    assert args[args.index("--max-filesize") + 1] == str(5 * MiB)


def test_grab_media_says_whose_limit_deferred_it(tmp_path: Path) -> None:
    seen: list[int | None] = []

    def grabber(url: str, workdir: Path, limit: int | None) -> Grab:
        seen.append(limit)
        return Grab(INFO, judge_probe(INFO, limit))

    got = grab_media(URL, grabber, lambda g: None, limit=10 * MiB)
    assert (got.outcome, got.limited, seen) == ("deferred", True, [10 * MiB])
    store = LocalStore(tmp_path)
    _limits(store, ana__a=10 * MiB)
    queue(store, URL, "media", T0, "ana/a")

    def grab(url: str, keep: Any, limit: int | None) -> MediaOutcome:
        return grab_media(url, grabber, keep, limit=limit)

    assert run(store, _rec(store).due or "", _guarded(), now=T0, media_grabber=grab).status == (
        "deferred")  # fmt: skip
    assert _rec(store).attempts[-1].limit == 10 * MiB


def test_the_stage_cap_isnt_the_subscriptions(tmp_path: Path) -> None:
    def grabber(url: str, workdir: Path, limit: int | None) -> Grab:
        return Grab(INFO, ("deferred", "over the cap"))

    assert not grab_media(URL, grabber, lambda g: None).limited
    assert not grab_media(URL, grabber, lambda g: None, limit=2 * media.MAX_BYTES).limited


def test_gallery_stops_at_the_limit(tmp_path: Path) -> None:
    def collector(url: str, workdir: Path, limit: int | None) -> gallery.Haul:
        return gallery.Haul(None, None, over_cap=True)

    got = gallery.collect(URL, collector, lambda g: None, limit=MiB)
    assert (got.outcome, got.error, got.limited) == (
        "deferred", "over the subscription's 1 MiB limit", True)  # fmt: skip


# ── the issue stage ─────────────────────────────────────────────────────────


def test_the_issue_stage_queues_for_the_messages_subscription(tmp_path: Path) -> None:
    raw = EML_FIXTURES[0].read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    store = LocalStore(tmp_path)
    web = Web(trackers_land=True)
    _issue(raw, store, web, make_resolver=lambda inv: _web_guarded(web, inv.refused_urls()),
           subscription="ana/a")  # fmt: skip
    doc = json.loads(store.get(captures_key(sha)) or b"")
    assert doc["subscription"] == "ana/a"
    hashes = {r["h"] for r in doc["links"] if r["h"]}
    assert hashes
    for h in hashes:
        assert CaptureRecord.load(store.get(index_key(h)) or b"").subs == ["ana/a"]
