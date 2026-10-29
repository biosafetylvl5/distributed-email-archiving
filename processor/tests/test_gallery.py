from __future__ import annotations

import json
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dea_capture import gallery, guard
from dea_capture.capture import (
    CaptureRecord,
    due_key,
    gallery_post,
    guess_kind,
    index_key,
    queue,
    run,
    url_hash,
)
from dea_capture.gallery import Haul, collect, gallerydl_args, scrub
from dea_capture.guard import GuardedFetcher
from dea_capture.media import MediaOutcome
from dea_capture.page import PageOutcome
from dea_capture.proxy import GuardProxy
from dea_capture.store import LocalStore

from .conftest import RecordingFetcher, fake_resolver

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
URL = "https://x.com/someone/status/1234567890"
POST = {"category": "twitter", "tweet_id": 1234567890, "content": "Hello", "author": {"name": "s"}}


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real = guard.check_destination

    def check(url: str, resolve: object = None, extra_blocked: Any = ()) -> Any:
        return real(url, fake_resolver, extra_blocked)

    monkeypatch.setattr(gallery, "check_destination", check)


# ── which URLs are posts ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "post"),
    [
        (URL, True),
        ("https://twitter.com/someone/status/1234567890/photo/1", True),
        ("https://x.com/someone", False),  # an account, not a post
        ("https://bsky.app/profile/someone.example.org/post/3kabc", True),
        ("https://bsky.app/profile/someone.example.org", False),
        ("https://www.instagram.com/p/AbC123/", True),
        ("https://www.instagram.com/someone/reel/AbC123/", True),
        ("https://www.instagram.com/someone/", False),
        ("https://www.facebook.com/someone/posts/pfbid0abc", True),
        ("https://www.facebook.com/someone", False),
        ("https://www.flickr.com/photos/someone/53000000000/", True),
        ("https://someone.tumblr.com/post/123456/a-title", True),
        ("https://imgur.com/a/AbC123", True),
        ("https://news.example.org/status/1", False),
    ],
)
def test_gallery_post(url: str, post: bool) -> None:
    assert gallery_post(url) is post
    assert (guess_kind(url, "text/html") == "gallery") is post


def test_gallerydl_args_keep_it_behind_the_proxy(tmp_path: Path) -> None:
    args = gallerydl_args(URL, tmp_path, 4321)
    assert args[args.index("--proxy") + 1] == "http://127.0.0.1:4321"
    assert "--config-ignore" in args
    assert args[args.index("--range") + 1] == f"1-{gallery.MAX_FILES}"
    assert args[args.index("-D") + 1] == str(tmp_path / "out")
    assert args[-1] == URL
    # no cookie or login cache kept between posts
    assert "cache.file=" in args


def test_scrub_drops_signed_query_strings() -> None:
    got = scrub({
        "display_url": "https://scontent.example.net/v/a.jpg?_nc_ht=x&oh=y&oe=z",
        "post_url": "https://www.example.com/photo/?fbid=1&set=a.2",
        "nested": [{"u": "https://cdn.example.net/b.mp4?X-Amz-Signature=s"}],
        "n": 3,
    })  # fmt: skip
    assert got == {
        "display_url": "https://scontent.example.net/v/a.jpg",
        "post_url": "https://www.example.com/photo/?fbid=1&set=a.2",
        "nested": [{"u": "https://cdn.example.net/b.mp4"}],
        "n": 3,
    }


# ── judging what gallery-dl did ─────────────────────────────────────────────


def fake_collector(**haul: Any) -> gallery.Collector:
    def collector(url: str, workdir: Path, limit: int | None = None) -> Haul:
        assert url == URL
        files: list[tuple[Path, dict[str, Any]]] = []
        for name in haul.pop("make", []):
            (workdir / name).write_bytes(b"x" * 10)
            files.append((workdir / name, {"url": "https://cdn.example.net/a.jpg?oh=1"}))
        return Haul(**{"exit_code": 0, "post": POST, "files": files, **haul})

    return collector


def test_collect_ok_names_files_in_order() -> None:
    kept: list[dict[str, bytes]] = []

    def keep(g: MediaOutcome) -> None:
        kept.append({n: p.read_bytes() for n, p in g.files.items()})

    got = collect(URL, fake_collector(make=["9_1.JPG", "9_2.mp4"]), keep)
    assert got.outcome == "ok"
    assert list(kept[0]) == ["files/001.jpg", "files/002.mp4"]
    assert got.info["post"]["content"] == "Hello"
    assert got.info["files"]["files/001.jpg"]["url"] == "https://cdn.example.net/a.jpg"


def test_collect_a_text_only_post() -> None:
    got = collect(URL, fake_collector(), lambda g: None)
    assert (got.outcome, got.files) == ("ok", {})


@pytest.mark.parametrize(
    ("haul", "outcome", "error"),
    [
        ({"exit_code": 64, "post": None}, "rerouted", "not a post gallery-dl knows"),
        ({"exit_code": 16, "post": None, "error": "Account credentials required"}, "rerouted",
         "wants a login: Account credentials required"),
        ({"exit_code": 4, "post": None, "error": "HTTP redirect to login page (<url>)"},
         "rerouted", "wants a login: HTTP redirect to login page (<url>)"),
        ({"exit_code": 4, "post": None, "error": "HttpError: '503 Service Unavailable'"},
         "failed", "HttpError: '503 Service Unavailable'"),
        ({"exit_code": None, "post": None}, "failed", "timed out"),
        ({"exit_code": None, "over_cap": True}, "deferred", "over the 3072 MiB cap"),
        ({"exit_code": 0, "post": None}, "failed", "gallery-dl exit 0"),
    ],
)  # fmt: skip
def test_collect_failures(haul: dict[str, Any], outcome: str, error: str) -> None:
    got = collect(URL, fake_collector(**haul), lambda g: pytest.fail("kept"))
    assert (got.outcome, got.error) == (outcome, error)


def test_collect_refuses_a_private_host() -> None:
    got = collect("http://10.0.0.5/p/1", fake_collector(), lambda g: pytest.fail("kept"))
    assert got.outcome == "refused"


# ── running gallery-dl (a fake one) ─────────────────────────────────────────

FAKE_GALLERYDL = """
import json, os, subprocess, sys, time
args = sys.argv[1:]
cwd = os.getcwd()
open(os.path.join(cwd, "env.json"), "w").write(json.dumps(dict(os.environ)))
print("[twitter][debug] " + args[-1], file=sys.stderr)  # its log names the URL
out = args[args.index("-D") + 1]
leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(os.path.join(cwd, "leftover.pid"), "w").write(str(leftover.pid))
if os.environ.get("FAKE_GDL_FAIL"):
    print("[twitter][error] HttpError: '404 Not Found' for " + args[-1], file=sys.stderr)
    sys.exit(4)
open(os.path.join(out, "info.json"), "w").write(json.dumps({"content": "Hello"}))
for name in ("123_10.jpg", "123_2.jpg"):
    open(os.path.join(out, name), "wb").write(b"\\xff\\xd8\\xff")
    open(os.path.join(out, name + ".json"), "w").write(json.dumps({"name": name}))
if os.environ.get("FAKE_GDL_BIG"):
    open(os.path.join(out, "big.mp4"), "wb").write(b"x" * 100)
    time.sleep(60)
"""


@pytest.fixture
def fake_gallerydl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pkg = tmp_path / "lib" / "gallery_dl"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(textwrap.dedent(FAKE_GALLERYDL))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "lib"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_run_gallerydl(fake_gallerydl: Path, capfd: pytest.CaptureFixture[str]) -> None:
    h = gallery.run_gallerydl(URL, fake_gallerydl, GuardProxy(verify=None))
    assert (h.exit_code, h.error, h.over_cap) == (0, None, False)
    assert h.post == {"content": "Hello"}
    # in the post's order, each with its metadata
    assert [(p.name, m["name"]) for p, m in h.files] == [("123_2.jpg", "123_2.jpg"),
                                                          ("123_10.jpg", "123_10.jpg")]  # fmt: skip
    env = json.loads((fake_gallerydl / "env.json").read_text())
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] == str(fake_gallerydl)
    assert URL not in capfd.readouterr().out
    leftover = int((fake_gallerydl / "leftover.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(leftover, 0)


def test_run_gallerydl_reports_the_error_without_urls(
    fake_gallerydl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GDL_FAIL", "1")
    h = gallery.run_gallerydl(URL, fake_gallerydl, GuardProxy(verify=None))
    assert h.exit_code == 4
    assert h.error == "HttpError: '404 Not Found' for <url>"


def test_run_gallerydl_stops_over_the_cap(
    fake_gallerydl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GDL_BIG", "1")
    monkeypatch.setattr(gallery, "MAX_BYTES", 50)
    h = gallery.run_gallerydl(URL, fake_gallerydl, GuardProxy(verify=None))
    assert h.exit_code is None and h.over_cap


# ── capture.run with gallery keys ───────────────────────────────────────────


def _guarded() -> GuardedFetcher:
    return GuardedFetcher(RecordingFetcher(), resolve=fake_resolver)


def _collector(c: gallery.Collector) -> Any:
    return lambda url, keep, limit: collect(url, c, keep, limit=limit)


def _rec(store: LocalStore) -> CaptureRecord:
    return CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")


def test_run_gallery_ok(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "store")
    assert queue(store, URL, "gallery", T0) == "new"
    key = due_key("gallery", T0, url_hash(URL))
    c = _collector(fake_collector(make=["1_1.jpg"]))
    result = run(store, key, _guarded(), now=T0, gallery_collector=c)
    assert (result.outcome, result.status) == ("ok", "ok")
    rec = _rec(store)
    assert rec.capture
    assert store.get(rec.capture + "files/001.jpg") == b"x" * 10
    info = json.loads(store.get(rec.capture + "info.json") or b"")
    assert info["post"]["content"] == "Hello"
    meta = json.loads(store.get(rec.capture + "capture.json") or b"")
    assert (meta["via"], meta["site"]) == ("gallery-dl", "twitter")
    assert meta["files"]["files/001.jpg"]["bytes"] == 10


def test_a_post_queued_as_a_page_moves_to_gallery_and_not_back(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "store")
    assert queue(store, URL, "page", T0) == "new"
    key = due_key("page", T0, url_hash(URL))

    def crawler(url: str) -> PageOutcome:
        return PageOutcome("ok", http_status=200, files={"page.wacz": b"PK"})

    # the page stage hands it to the gallery stage, without crawling
    result = run(store, key, _guarded(), now=T0, page_crawler=lambda u: pytest.fail("crawled"))
    assert result.outcome == "rerouted"
    rec = _rec(store)
    assert (rec.kind, rec.due) == ("gallery", due_key("gallery", T0, rec.h))
    # which finds a login wall, and hands it back
    c = _collector(fake_collector(exit_code=16, post=None))
    assert rec.due
    assert run(store, rec.due, _guarded(), now=T0, gallery_collector=c).outcome == "rerouted"
    rec = _rec(store)
    assert (rec.kind, rec.status) == ("page", "queued")
    # and this time the page stage crawls it
    assert rec.due
    result = run(store, rec.due, _guarded(), now=T0, page_crawler=crawler)
    assert result.outcome == "ok"
