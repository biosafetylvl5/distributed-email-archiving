from __future__ import annotations

import gzip
import io
import json
import os
import stat
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dea_capture import guard, page
from dea_capture.capture import (
    RETRY_AFTER,
    CaptureRecord,
    index_key,
    parse_due_key,
    queue,
    run,
    url_hash,
)
from dea_capture.guard import GuardedFetcher
from dea_capture.page import Crawl, PageOutcome, crawl_args, crawl_page, read_wacz
from dea_capture.proxy import GuardProxy, ProxyStats
from dea_capture.store import LocalStore

from .conftest import RecordingFetcher, fake_resolver

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
URL = "https://news.example.org/2026/10/story"
PNG = b"\x89PNG\r\n\x1a\nfake"


def _warc(records: list[tuple[str, str, bytes]]) -> bytes:
    """Gzip-per-record WARC, as the crawler writes it."""
    out = io.BytesIO()
    for kind, target, block in records:
        head = (
            f"WARC/1.1\r\nWARC-Type: {kind}\r\nWARC-Target-URI: {target}\r\n"
            f"Content-Type: image/png\r\nContent-Length: {len(block)}\r\n\r\n"
        ).encode()
        out.write(gzip.compress(head + block + b"\r\n\r\n"))
    return out.getvalue()


def make_wacz(
    path: Path, pages: list[dict[str, Any]], shot: bytes | None = PNG, final: str | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        lines = [{"format": "json-pages-1.0", "id": "pages"}, *pages]
        z.writestr("pages/pages.jsonl", "\n".join(json.dumps(x) for x in lines) + "\n")
        z.writestr("archive/rec-1.warc.gz", _warc([("response", URL, b"<html>")]))
        if shot is not None:
            records = [("resource", f"urn:thumbnail:{URL}", b"thumb"),
                       ("resource", f"urn:view:{URL}", shot)]  # fmt: skip
            z.writestr("archive/screenshots-1.warc.gz", _warc(records))
        if final is not None:
            z.writestr("archive/text-1.warc.gz",
                       _warc([("resource", f"urn:textFinal:{URL}", final.encode())]))  # fmt: skip
    return path


def _page(**kw: Any) -> dict[str, Any]:
    return {"url": URL, "title": "Story", "loadState": 4, "status": 200, "mime": "text/html",
            "seed": True, "text": "The story text.", **kw}  # fmt: skip


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """crawl_page checks the URL first; resolve with the fake DNS."""
    real = guard.check_destination

    def check(url: str, resolve: object = None, extra_blocked: Any = ()) -> Any:
        return real(url, fake_resolver, extra_blocked)

    monkeypatch.setattr(page, "check_destination", check)


def test_crawl_args_keep_the_browser_behind_the_proxy(tmp_path: Path) -> None:
    args = crawl_args(URL, tmp_path, 4321)
    assert args[:3] == ["crawl", "--url", URL]
    assert args[args.index("--proxyServer") + 1] == "http://127.0.0.1:4321"
    assert "--extraChromeArgs=--proxy-bypass-list=<-loopback>" in args
    assert args[args.index("--scopeType") + 1] == "page"
    assert args[args.index("--limit") + 1] == "1"
    assert args[args.index("--cwd") + 1] == str(tmp_path)
    # the crawler's plain Chrome user agent: bot protection blocks anything that names us
    assert not any(a.startswith("--userAgent") for a in args)


def test_read_wacz(tmp_path: Path) -> None:
    other = _page(url="https://news.example.org/other", seed=False)
    w = make_wacz(tmp_path / "x.wacz", [other, _page()])
    got = read_wacz(w, URL)
    assert got.page is not None and got.page["title"] == "Story"
    assert got.screenshot == PNG
    assert got.final_text is None
    empty = read_wacz(make_wacz(tmp_path / "y.wacz", [], shot=None), URL)
    assert (empty.page, empty.screenshot) == (None, None)
    assert read_wacz(make_wacz(tmp_path / "z.wacz", [_page()], final="Grown text."),
                     URL).final_text == "Grown text."  # fmt: skip


def fake_crawler(tmp_path: Path, **crawl: Any) -> page.Crawler:
    def crawler(url: str, workdir: Path) -> Crawl:
        assert url == URL
        return Crawl(**{"exit_code": 0, "wacz": None, "page": None, "screenshot": None, **crawl})

    return crawler


def test_crawl_page_ok(tmp_path: Path) -> None:
    w = make_wacz(tmp_path / "x.wacz", [_page()])
    got = crawl_page(URL, fake_crawler(tmp_path, wacz=w, page=_page(), screenshot=PNG,
                                       exit_code=15))  # fmt: skip
    assert got.outcome == "ok"
    assert got.http_status == 200
    assert set(got.files) == {"page.wacz", "screenshot.png", "text.txt"}
    assert got.info["limit_hit"] == "time limit"
    assert got.info["title"] == "Story"
    assert (got.files["text.txt"], got.info["text_from"]) == (b"The story text.", "load")


def test_crawl_page_prefers_the_final_text(tmp_path: Path) -> None:
    w = make_wacz(tmp_path / "x.wacz", [_page()])
    got = crawl_page(URL, fake_crawler(tmp_path, wacz=w, page=_page(text=""),
                                       final_text="Loaded later."))  # fmt: skip
    assert (got.files["text.txt"], got.info["text_from"]) == (b"Loaded later.", "final")


@pytest.mark.parametrize(
    ("crawl", "outcome", "error"),
    [
        ({"page": _page(status=404)}, "failed", "HTTP 404"),
        ({"page": _page(loadState=0, status=None)}, "failed", "page didn't load"),
        ({"exit_code": 1}, "failed", "crawl exit 1, no page recorded"),
        ({"exit_code": None}, "failed", "crawl timed out, no page recorded"),
        (
            {"exit_code": 9, "log_errors": "Page Load Failed (msg=net::ERR_TIMED_OUT)"},
            "failed",
            "crawl exit 9, no page recorded: Page Load Failed (msg=net::ERR_TIMED_OUT)",
        ),
    ],
)
def test_crawl_page_failures(
    tmp_path: Path, crawl: dict[str, Any], outcome: str, error: str
) -> None:
    if "page" in crawl:
        crawl["wacz"] = make_wacz(tmp_path / "x.wacz", [crawl["page"]])
    got = crawl_page(URL, fake_crawler(tmp_path, **crawl))
    assert (got.outcome, got.error) == (outcome, error)
    assert not got.files


def test_crawl_page_refused(tmp_path: Path) -> None:
    def never(url: str, workdir: Path) -> Crawl:
        raise AssertionError("crawled a refused URL")

    assert crawl_page("https://intranet.example.com/", never).outcome == "refused"
    assert crawl_page("https://news.example.org/?token=abc", never).outcome == "refused"
    # the proxy refused the page's own host (its DNS changed under us)
    stats = ProxyStats()
    stats.refused["news.example.org"] += 1
    got = crawl_page(URL, fake_crawler(tmp_path, exit_code=1, stats=stats))
    assert got.outcome == "refused"


def test_log_errors(tmp_path: Path) -> None:
    log = tmp_path / "crawl.log"
    lines: list[Any] = [
        {"logLevel": "info", "context": "general", "message": "Seeds", "details": {"url": URL}},
        {"logLevel": "warn", "context": "pageStatus", "message": "Page Load Failed",
         "details": {"msg": "net::ERR_CONNECTION_RESET at https://cdn.example.net/a?t=1",
                     "loadState": 0, "page": URL, "workerid": 0}},
        {"logLevel": "warn", "context": "pageStatus", "message": "Page Load Failed",
         "details": {"msg": "net::ERR_CONNECTION_RESET at https://cdn.example.net/a?t=1",
                     "loadState": 0, "page": URL}},
        {"logLevel": "error", "context": "general",
         "message": "Crawl failed, no pages crawled successfully"},
        {"logLevel": "error", "context": "browser", "message": "Mail reader@example.com said no"},
    ]  # fmt: skip
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    got = page.log_errors(log)
    assert got == (
        "Page Load Failed (msg=net::ERR_CONNECTION_RESET at <cdn.example.net>, loadState=0); "
        "Mail <address> said no"
    )
    assert page.log_errors(tmp_path / "missing.log") is None
    log.write_text(json.dumps({"logLevel": "error", "message": "x" * 1000}))
    assert len(page.log_errors(log) or "") == page.LOG_ERRORS_MAX


# -- run_crawler, with a stand-in `crawl` on PATH

FAKE_CRAWL = r"""import json, os, sys, time, zipfile
args = sys.argv[1:]
cwd = args[args.index("--cwd") + 1]
open(os.path.join(cwd, "env.json"), "w").write(json.dumps(dict(os.environ)))
if os.environ.get("FAKE_CRAWL_HANG"):
    time.sleep(60)
# like the crawler's redis: a child left running after the crawler exits
import subprocess
leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(os.path.join(cwd, "leftover.pid"), "w").write(str(leftover.pid))
coll = os.path.join(cwd, "collections", "page")
os.makedirs(coll)
with zipfile.ZipFile(os.path.join(coll, "page.wacz"), "w") as z:
    z.writestr("pages/pages.jsonl", json.dumps({"url": args[args.index("--url") + 1],
               "status": 200, "loadState": 4}) + "\n")
print("crawler log line with " + args[args.index("--url") + 1])
"""


@pytest.fixture
def fake_crawl_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "crawl.py"
    script.write_text(FAKE_CRAWL)
    exe = bindir / "crawl"
    # a wrapper, not a shebang: the interpreter's path may contain spaces
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    return tmp_path


def test_run_crawler(fake_crawl_bin: Path, capfd: pytest.CaptureFixture[str]) -> None:
    work = fake_crawl_bin / "work"
    work.mkdir()
    c = page.run_crawler(URL, work, GuardProxy(verify=None))
    assert c.exit_code == 0
    assert c.page is not None and c.page["status"] == 200
    assert c.wacz is not None and c.wacz.is_file()
    # no credentials for the crawler, and its log (URLs) never reaches stdout
    env = json.loads((work / "env.json").read_text())
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # its redis and browser stay killable, and keep their files in `work`
    assert env["DETACHED_CHILD_PROC"] == "0"
    assert env["TMPDIR"] == str(work)
    assert URL not in capfd.readouterr().out
    assert URL in (work / "crawl.log").read_text()
    # the child it left behind was ended with it
    leftover = int((work / "leftover.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(leftover, 0)


def test_run_crawler_kills_a_hung_crawl(
    fake_crawl_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRAWL_HANG", "1")
    monkeypatch.setattr(page, "KILL_AFTER", 1)
    work = fake_crawl_bin / "work"
    work.mkdir()
    c = page.run_crawler(URL, work, GuardProxy(verify=None))
    assert c.exit_code is None
    assert c.page is None and c.wacz is None


# -- capture.run with page keys


def _page_store(tmp_path: Path) -> tuple[LocalStore, str]:
    store = LocalStore(tmp_path / "store")
    assert queue(store, URL, "page", T0) == "new"
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.due is not None
    return store, rec.due


def _no_direct() -> GuardedFetcher:
    return GuardedFetcher(RecordingFetcher(), resolve=fake_resolver)


def test_run_page_ok(tmp_path: Path) -> None:
    store, due = _page_store(tmp_path)
    got = PageOutcome("ok", 200, files={"page.wacz": b"wacz", "text.txt": b"text"},
                      info={"title": "Story", "proxy": {"tunnels": 3}})  # fmt: skip
    seen: list[str] = []

    def crawler(url: str) -> PageOutcome:
        seen.append(url)
        return got

    r = run(store, due, _no_direct(), now=T0, page_crawler=crawler)
    assert (r.outcome, r.status) == ("ok", "ok")
    assert seen == [URL]
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.capture is not None and rec.due is None
    assert store.get(rec.capture + "page.wacz") == b"wacz"
    meta = json.loads(store.get(rec.capture + "capture.json") or b"")
    assert meta["via"] == "browsertrix"
    assert meta["files"]["page.wacz"]["bytes"] == 4
    assert meta["title"] == "Story"
    assert not list((tmp_path / "store" / "captures" / "due").rglob("*.json"))


def test_run_page_failure_retries(tmp_path: Path) -> None:
    store, due = _page_store(tmp_path)
    r = run(store, due, _no_direct(), now=T0,
            page_crawler=lambda url: PageOutcome("failed", 503, "HTTP 503"))  # fmt: skip
    assert (r.outcome, r.status) == ("failed", "retrying")
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.due is not None
    kind, when, _ = parse_due_key(rec.due)
    assert (kind, when) == ("page", T0 + RETRY_AFTER[0])
    assert store.exists(rec.due)


def test_run_page_refused_is_final(tmp_path: Path) -> None:
    store, due = _page_store(tmp_path)
    r = run(store, due, _no_direct(), now=T0,
            page_crawler=lambda url: PageOutcome("refused", error="private"))  # fmt: skip
    assert r.status == "refused"


WAYBACK_TS = "20260930000000"


def test_a_page_the_site_refuses_gets_its_snapshot_crawled_at_once(tmp_path: Path) -> None:
    from dea_capture.capture import wayback_url

    from .test_capture import Web, _guarded, cdx_answer, cdx_api

    store, due = _page_store(tmp_path)
    wayback = Web(bodies={cdx_api(URL): ("application/json", cdx_answer(WAYBACK_TS, URL))})
    replay = wayback_url(WAYBACK_TS, URL, "if_")
    seen: list[str] = []

    def crawler(url: str) -> PageOutcome:
        seen.append(url)
        if url == URL:
            return PageOutcome("failed", 403, "HTTP 403")
        return PageOutcome("ok", 200, files={"page.wacz": b"old wacz"}, info={"title": "Old"})

    r = run(store, due, _no_direct(), now=T0, page_crawler=crawler,
            wayback_fetcher=_guarded(wayback))  # fmt: skip
    assert (r.outcome, r.status) == ("failed", "retrying")
    assert seen == [URL, replay]
    assert wayback.requested == [cdx_api(URL)]
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.wayback is not None and rec.wayback["via"] == "crawl"
    assert rec.capture is None and rec.due is not None
    folder = rec.wayback["capture"]
    assert store.get(folder + "page.wacz") == b"old wacz"
    meta = json.loads(store.get(folder + "capture.json") or b"")
    assert (meta["canonical"], meta["via"], meta["snapshot"]) == (
        URL,
        f"wayback:{WAYBACK_TS}",
        replay,
    )


def test_a_page_whose_snapshot_will_not_crawl_gets_its_html(tmp_path: Path) -> None:
    from dea_capture.capture import wayback_url

    from .test_capture import Web, _guarded, cdx_answer, cdx_api

    store, due = _page_store(tmp_path)
    raw = wayback_url(WAYBACK_TS, URL, "id_")
    wayback = Web(bodies={cdx_api(URL): ("application/json", cdx_answer(WAYBACK_TS, URL)),
                          raw: ("text/html", b"<html>old</html>")})  # fmt: skip
    failing = lambda url: PageOutcome("failed", error="crawl exit 9, no page recorded")  # noqa: E731
    run(store, due, _no_direct(), now=T0, page_crawler=failing, wayback_fetcher=_guarded(wayback))
    assert wayback.requested == []  # one failure, no status: wait for the retry
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.due is not None
    run(store, rec.due, _no_direct(), now=T0 + RETRY_AFTER[0], page_crawler=failing,
        wayback_fetcher=_guarded(wayback))  # fmt: skip
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.wayback is not None and rec.wayback["via"] == "raw"
    assert store.get(rec.wayback["capture"] + "original.html") == b"<html>old</html>"


def test_page_keys_need_a_crawler(tmp_path: Path) -> None:
    store, due = _page_store(tmp_path)
    with pytest.raises(NotImplementedError):
        run(store, due, _no_direct(), now=T0 + timedelta(seconds=1))
