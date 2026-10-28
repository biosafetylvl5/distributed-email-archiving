from __future__ import annotations

import hashlib
import json
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dea_capture import guard, media
from dea_capture.capture import CaptureRecord, due_key, index_key, queue, run, url_hash
from dea_capture.guard import GuardedFetcher
from dea_capture.media import (
    Grab,
    MediaOutcome,
    estimated_bytes,
    grab_media,
    judge_probe,
    store_media,
    ytdlp_args,
)
from dea_capture.proxy import GuardProxy
from dea_capture.store import LocalStore

from .conftest import RecordingFetcher, fake_resolver

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
URL = "https://www.youtube.com/watch?v=abc123"
INFO = {
    "id": "abc123", "title": "A talk", "duration": 61, "uploader": "Someone",
    "format": "137 - 1920x1080+140 - audio", "ext": "mp4", "live_status": "not_live",
    "requested_formats": [{"filesize": 10 << 20, "url": "https://rr1.example.net/v?sig=x"},
                          {"filesize_approx": 1 << 20, "url": "https://rr1.example.net/a"}],
    "formats": [{"url": "https://rr1.example.net/v?sig=x"}],
    "http_headers": {"User-Agent": "x"}, "url": "https://rr1.example.net/v?sig=x",
}  # fmt: skip


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real = guard.check_destination

    def check(url: str, resolve: object = None, extra_blocked: Any = ()) -> Any:
        return real(url, fake_resolver, extra_blocked)

    monkeypatch.setattr(media, "check_destination", check)


# ── deciding from the probe ─────────────────────────────────────────────────


def test_estimated_bytes() -> None:
    assert estimated_bytes(INFO) == 11 << 20
    assert estimated_bytes({"filesize_approx": 5}) == 5
    assert estimated_bytes({"requested_formats": [{"filesize": 5}, {}]}) is None


@pytest.mark.parametrize(
    ("info", "verdict"),
    [
        (INFO, None),
        ({"_type": "playlist", "entries": []}, "rerouted"),
        ({**INFO, "live_status": "is_live"}, "failed"),
        ({**INFO, "live_status": "is_upcoming"}, "failed"),
        ({**INFO, "requested_formats": [{"filesize": 4 << 30}]}, "deferred"),
        ({**INFO, "requested_formats": [{"url": "x"}]}, None),  # size unknown: try
    ],
)
def test_judge_probe(info: dict[str, Any], verdict: str | None) -> None:
    got = judge_probe(info)
    assert (got[0] if got else None) == verdict


def test_ytdlp_args_keep_it_behind_the_proxy(tmp_path: Path) -> None:
    for probe in (True, False):
        args = ytdlp_args(URL, tmp_path, 4321, probe=probe)
        assert args[args.index("--proxy") + 1] == "http://127.0.0.1:4321"
        assert {"--ignore-config", "--no-playlist"} <= set(args)
    probe_args = ytdlp_args(URL, tmp_path, 4321, probe=True)
    assert probe_args[-2:] == ["-J", URL]
    dl = ytdlp_args(URL, tmp_path, 4321, probe=False)
    # every protocol through yt-dlp's own downloader, so ffmpeg fetches nothing
    assert dl[dl.index("--downloader") + 1] == "native"
    assert dl[dl.index("--load-info-json") + 1] == str(tmp_path / "probe.json")
    assert dl[dl.index("--max-filesize") + 1] == str(media.MAX_BYTES)
    assert URL not in dl  # the download takes the probe's metadata, not the URL


# ── judging a grab ──────────────────────────────────────────────────────────


def fake_grabber(**grab: Any) -> media.Grabber:
    def grabber(url: str, workdir: Path) -> Grab:
        assert url == URL
        files = []
        for name in grab.pop("make", []):
            (workdir / name).write_bytes(b"x" * 10)
            files.append(workdir / name)
        return Grab(**{"info": INFO, "verdict": None, "files": files, **grab})

    return grabber


def test_grab_media_ok_keeps_the_files_while_they_exist() -> None:
    kept: list[dict[str, bytes]] = []

    def keep(g: MediaOutcome) -> None:
        kept.append({n: p.read_bytes() for n, p in g.files.items()})

    got = grab_media(URL, fake_grabber(make=["media.mp4", "thumbnail.webp"]), keep)
    assert got.outcome == "ok"
    assert kept == [{"media.mp4": b"x" * 10, "thumbnail.webp": b"x" * 10}]
    # the post's metadata, not how it was fetched
    assert got.info["title"] == "A talk"
    assert not {"formats", "requested_formats", "http_headers", "url"} & set(got.info)


@pytest.mark.parametrize(
    ("grab", "outcome", "error"),
    [
        ({"verdict": ("rerouted", "a channel")}, "rerouted", "a channel"),
        ({"info": None, "error": "Video unavailable", "exit_code": 1}, "failed",
         "Video unavailable"),
        ({"exit_code": 1, "error": "HTTP Error 403"}, "failed", "HTTP Error 403"),
        ({"exit_code": None}, "failed", "yt-dlp timed out, no file"),
        ({"make": ["thumbnail.jpg"]}, "failed", "yt-dlp exit 0, no file"),
    ],
)  # fmt: skip
def test_grab_media_failures(grab: dict[str, Any], outcome: str, error: str) -> None:
    got = grab_media(URL, fake_grabber(**grab), lambda g: pytest.fail("kept"))
    assert (got.outcome, got.error) == (outcome, error)


def test_grab_media_defers_an_oversize_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "MAX_BYTES", 5)
    got = grab_media(URL, fake_grabber(make=["media.mp4"]), lambda g: pytest.fail("kept"))
    assert got.outcome == "deferred"


def test_grab_media_refuses_a_private_host() -> None:
    got = grab_media("http://10.0.0.5/v.mp4", fake_grabber(), lambda g: pytest.fail("kept"))
    assert got.outcome == "refused"


# ── running yt-dlp (a fake one) ─────────────────────────────────────────────

FAKE_YTDLP = """
import json, os, subprocess, sys, time
args = sys.argv[1:]
cwd = os.getcwd()
open(os.path.join(cwd, "env-" + ("probe" if "-J" in args else "dl") + ".json"), "w").write(
    json.dumps(dict(os.environ)))
if "-J" in args:
    print(args[-1], file=sys.stderr)  # its log names the URL
    if os.environ.get("FAKE_YTDLP_FAIL"):
        print("ERROR: [youtube] abc123: Video unavailable " + args[-1], file=sys.stderr)
        sys.exit(1)
    print(os.environ["FAKE_YTDLP_INFO"])
    sys.exit(0)
if os.environ.get("FAKE_YTDLP_HANG"):
    time.sleep(60)
leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(os.path.join(cwd, "leftover.pid"), "w").write(str(leftover.pid))
out = [a.split(":", 1)[-1] for i, a in enumerate(args) if args[i - 1] == "-P"][0]
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "media.mp4"), "wb").write(b"\\x00\\x00\\x00\\x18ftypmp42")
open(os.path.join(out, "thumbnail.jpg"), "wb").write(b"\\xff\\xd8\\xff")
"""


@pytest.fixture
def fake_ytdlp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pkg = tmp_path / "lib" / "yt_dlp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(textwrap.dedent(FAKE_YTDLP))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "lib"))
    monkeypatch.setenv("FAKE_YTDLP_INFO", json.dumps(INFO))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_run_ytdlp(fake_ytdlp: Path, capfd: pytest.CaptureFixture[str]) -> None:
    g = media.run_ytdlp(URL, fake_ytdlp, GuardProxy(verify=None))
    assert (g.exit_code, g.verdict, g.error) == (0, None, None)
    assert g.info is not None and g.info["title"] == "A talk"
    assert [p.name for p in g.files] == ["media.mp4", "thumbnail.jpg"]
    for run_ in ("probe", "dl"):
        env = json.loads((fake_ytdlp / f"env-{run_}.json").read_text())
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert env["HOME"] == str(fake_ytdlp)
    assert URL not in capfd.readouterr().out
    leftover = int((fake_ytdlp / "leftover.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(leftover, 0)


def test_run_ytdlp_stops_at_the_probe(fake_ytdlp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_YTDLP_INFO", json.dumps({"_type": "playlist", "entries": []}))
    g = media.run_ytdlp(URL, fake_ytdlp, GuardProxy(verify=None))
    assert g.verdict is not None and g.verdict[0] == "rerouted"
    assert not (fake_ytdlp / "env-dl.json").exists()


def test_run_ytdlp_reports_the_error_without_urls(
    fake_ytdlp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_YTDLP_FAIL", "1")
    g = media.run_ytdlp(URL, fake_ytdlp, GuardProxy(verify=None))
    assert g.info is None
    assert g.error == "[youtube] abc123: Video unavailable <url>"


def test_run_ytdlp_kills_a_hung_download(fake_ytdlp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_YTDLP_HANG", "1")
    monkeypatch.setattr(media, "DOWNLOAD_TIMEOUT", 1)
    g = media.run_ytdlp(URL, fake_ytdlp, GuardProxy(verify=None))
    assert g.exit_code is None and g.files == []


# ── capture.run with media keys ─────────────────────────────────────────────


def _media_store(tmp_path: Path) -> tuple[LocalStore, str]:
    store = LocalStore(tmp_path / "store")
    assert queue(store, URL, "media", T0) == "new"
    return store, due_key("media", T0, url_hash(URL))


def _guarded() -> GuardedFetcher:
    return GuardedFetcher(RecordingFetcher(), resolve=fake_resolver)


def _grabber(grab: media.Grabber) -> Any:
    return lambda url, keep: grab_media(url, grab, keep)


def test_run_media_ok(tmp_path: Path) -> None:
    store, key = _media_store(tmp_path)
    grab = fake_grabber(make=["media.mp4", "thumbnail.jpg"])
    result = run(store, key, _guarded(), now=T0, media_grabber=_grabber(grab))
    assert (result.outcome, result.status) == ("ok", "ok")
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert rec.capture
    assert store.get(rec.capture + "media.mp4") == b"x" * 10
    assert store.get(rec.capture + "thumbnail.jpg") == b"x" * 10
    info = json.loads(store.get(rec.capture + "info.json") or b"")
    assert info["title"] == "A talk" and "formats" not in info
    meta = json.loads(store.get(rec.capture + "capture.json") or b"")
    assert meta["via"] == "yt-dlp"
    assert meta["files"]["media.mp4"]["bytes"] == 10
    assert "rr1.example.net" not in json.dumps(meta) + json.dumps(info)


def test_run_media_sends_a_channel_to_the_page_stage(tmp_path: Path) -> None:
    store, key = _media_store(tmp_path)
    grab = fake_grabber(verdict=("rerouted", "a channel or playlist, not one post"))
    result = run(store, key, _guarded(), now=T0, media_grabber=_grabber(grab))
    assert result.outcome == "rerouted"
    rec = CaptureRecord.load(store.get(index_key(url_hash(URL))) or b"")
    assert (rec.kind, rec.status, rec.due) == ("page", "queued", due_key("page", T0, rec.h))


def test_run_media_defers_and_retries(tmp_path: Path) -> None:
    store, key = _media_store(tmp_path)
    grab = fake_grabber(verdict=("deferred", "too big"))
    assert run(store, key, _guarded(), now=T0, media_grabber=_grabber(grab)).status == "deferred"
    store, key = _media_store(tmp_path / "2")
    grab = fake_grabber(verdict=("failed", "stream is is_live"))
    assert run(store, key, _guarded(), now=T0, media_grabber=_grabber(grab)).status == "retrying"


def test_store_media_streams_files(tmp_path: Path) -> None:
    f = tmp_path / "media.webm"
    f.write_bytes(b"webm")
    store = LocalStore(tmp_path / "s")
    got = MediaOutcome("ok", files={"media.webm": f}, info={"title": "t"})
    store_media(store, "x/", URL, got, "2026-10-01T12:00:00+00:00")
    assert store.get("x/media.webm") == b"webm"
    meta = json.loads(store.get("x/capture.json") or b"")
    assert meta["files"]["media.webm"] == {
        "bytes": 4,
        "sha256": hashlib.sha256(b"webm").hexdigest(),
    }
