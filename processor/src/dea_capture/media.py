"""capture-media: a video or audio post, with yt-dlp.

yt-dlp runs twice, both times behind ``GuardProxy``, so it reaches public
addresses only. The first run reads the post's metadata (``-J``) and nothing
else; from it:

- a channel or playlist is not one post: it goes to capture-page instead;
- a live or upcoming stream is retried later, once it's a recording;
- a post whose chosen formats are estimated at more than ``MAX_BYTES`` is
  deferred without downloading.

The second run downloads the formats the first one chose (best up to 1080p, MP4 if it can),
using yt-dlp's own downloader for every protocol, so ffmpeg only merges files
on disk and never fetches anything itself.

Stored in the attempt's folder under ``captures/<h[:2]>/<h>/<stamp>/``:
``media.<ext>``, ``thumbnail.<ext>`` when there is one, ``info.json`` (the
post's metadata, without its signed format URLs or request headers), and
``capture.json``. The files stream from disk to the store.

yt-dlp's log names the post's URLs. It is kept in the temporary directory and
never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .guard import ForbiddenFetch, IPNetwork, check_destination, check_link
from .page import _become_subreaper, _end_group
from .proxy import GuardProxy, ProxyStats
from .store import Store, put_json

MAX_BYTES = 3 * 1024**3
# best up to 1080p, preferring MP4 and M4A streams, which merge into a file
# browsers play
FORMAT_SORT = "res:1080,ext"
PROBE_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 90 * 60
_SECRET_ENV = ("AWS_", "COURIER_")

# what info.json keeps of yt-dlp's metadata: the post, not how to fetch it
INFO_KEEP = (
    "id", "title", "fulltitle", "description", "uploader", "uploader_id", "uploader_url",
    "channel", "channel_id", "channel_url", "creator", "creators", "artist", "track",
    "album", "upload_date", "release_date", "timestamp", "release_timestamp", "duration",
    "view_count", "like_count", "comment_count", "repost_count", "tags", "categories",
    "chapters", "license", "age_limit", "live_status", "was_live", "webpage_url",
    "extractor", "extractor_key", "format_id", "format", "format_note", "ext", "width",
    "height", "fps", "vcodec", "acodec", "abr", "vbr", "language",
)  # fmt: skip
_LIVE = {"is_live", "is_upcoming", "post_live"}
_COLLECTIONS = {"playlist", "multi_video"}

Outcome = Literal["ok", "failed", "refused", "deferred", "rerouted"]


@dataclass
class Grab:
    """What yt-dlp did with one URL."""

    info: dict[str, Any] | None  # the probe's metadata; None if it failed
    verdict: tuple[Outcome, str] | None  # set when the probe decided against downloading
    files: list[Path] = field(default_factory=list)  # downloaded, in the work directory
    exit_code: int | None = 0  # of the last run; None: killed at its timeout
    error: str | None = None  # yt-dlp's last ERROR line
    stats: ProxyStats = field(default_factory=ProxyStats)


Grabber = Callable[[str, Path], Grab]


def estimated_bytes(info: dict[str, Any]) -> int | None:
    """The chosen formats' size, as the site reports or yt-dlp estimates it."""
    formats = info.get("requested_formats") or [info]
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
    return sum(sizes) if all(isinstance(s, int) for s in sizes) else None


def judge_probe(info: dict[str, Any]) -> tuple[Outcome, str] | None:
    """Why not to download this post, or None to go ahead."""
    if info.get("_type") in _COLLECTIONS:
        return "rerouted", "a channel or playlist, not one post"
    if info.get("live_status") in _LIVE or info.get("is_live"):
        return "failed", f"stream is {info.get('live_status') or 'live'}"
    size = estimated_bytes(info)
    if size is not None and size > MAX_BYTES:
        return "deferred", f"about {size // 2**20} MiB, over the {MAX_BYTES // 2**20} MiB cap"
    return None


def ytdlp_args(url: str, workdir: Path, proxy_port: int, *, probe: bool) -> list[str]:
    common = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
        "--proxy", f"http://127.0.0.1:{proxy_port}", "--js-runtimes", "node",
        "--cache-dir", str(workdir / "cache"), "--socket-timeout", "60",
        "-S", FORMAT_SORT, "--no-progress", "--newline",
    ]  # fmt: skip
    if probe:
        return [*common, "--flat-playlist", "-J", url]
    return [
        *common, "--load-info-json", str(workdir / "probe.json"),
        # yt-dlp fetches every protocol itself; ffmpeg only merges
        "--downloader", "native", "--max-filesize", str(MAX_BYTES),
        "--retries", "5", "--fragment-retries", "10",
        "--merge-output-format", "mp4/mkv", "--write-thumbnail", "--no-mtime",
        "-P", str(workdir / "out"), "-P", f"temp:{workdir / 'part'}",
        "-o", "media.%(ext)s", "-o", "thumbnail:thumbnail.%(ext)s",
    ]  # fmt: skip


def _env(workdir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SECRET_ENV)}
    env["HOME"] = env["TMPDIR"] = str(workdir)
    return env


def _run(
    args: list[str], workdir: Path, log: str, timeout: float, stdout: Path | None
) -> int | None:
    with (workdir / log).open("wb") as err, (stdout or Path(os.devnull)).open("wb") as out:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=workdir,
            env=_env(workdir), start_new_session=True,
        )  # fmt: skip
        try:
            code: int | None = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            code = None
        _end_group(proc.pid)
    return code


_ERROR = re.compile(r"^ERROR: (.*)$", re.M)
_URL = re.compile(r"https?://\S+")


def _last_error(workdir: Path, log: str) -> str | None:
    try:
        found = _ERROR.findall((workdir / log).read_text(errors="replace"))
    except OSError:
        return None
    return _URL.sub("<url>", found[-1])[:300] if found else None


def run_ytdlp(url: str, workdir: Path, proxy: GuardProxy) -> Grab:
    """Probe ``url`` with yt-dlp behind ``proxy``, then download it unless told not to."""
    _become_subreaper()
    port = proxy.start()
    try:
        probe = workdir / "probe.json"
        code = _run(ytdlp_args(url, workdir, port, probe=True), workdir, "probe.log",
                    PROBE_TIMEOUT, probe)  # fmt: skip
        try:
            info = json.loads(probe.read_text())
        except (OSError, ValueError):
            info = None
        if not isinstance(info, dict):
            return Grab(None, None, exit_code=code, error=_last_error(workdir, "probe.log"),
                        stats=proxy.stats)  # fmt: skip
        verdict = judge_probe(info)
        if verdict:
            return Grab(info, verdict, exit_code=code, stats=proxy.stats)
        code = _run(ytdlp_args(url, workdir, port, probe=False), workdir, "download.log",
                    DOWNLOAD_TIMEOUT, None)  # fmt: skip
    finally:
        proxy.stop()
    out = workdir / "out"
    files = sorted(p for p in out.iterdir() if p.is_file()) if out.is_dir() else []
    return Grab(info, None, files, code, _last_error(workdir, "download.log"), proxy.stats)


@dataclass
class MediaOutcome:
    outcome: Outcome
    error: str | None = None
    files: dict[str, Path] = field(default_factory=dict)  # stored name -> file
    info: dict[str, Any] = field(default_factory=dict)  # for info.json
    details: dict[str, Any] = field(default_factory=dict)  # for capture.json


def _ytdlp_version() -> str:
    try:
        return f"yt-dlp {metadata.version('yt-dlp')}"
    except metadata.PackageNotFoundError:
        return "yt-dlp (unknown)"


def grab_media(
    canonical: str, grabber: Grabber, keep: Callable[[MediaOutcome], None],
    extra_blocked: Sequence[IPNetwork] = (),
) -> MediaOutcome:  # fmt: skip
    """Grab ``canonical`` into a temporary directory and judge the result. ``keep``
    is called with a good outcome while its files still exist."""
    try:
        check_link(canonical)
        check_destination(canonical, extra_blocked=extra_blocked)
    except ForbiddenFetch as exc:
        return MediaOutcome("refused", str(exc))
    with tempfile.TemporaryDirectory(prefix="dea-media-") as tmp:
        g = grabber(canonical, Path(tmp))
        if g.verdict:
            return MediaOutcome(*g.verdict)
        if g.info is None:
            return MediaOutcome("failed", g.error or f"yt-dlp exit {g.exit_code}, no metadata")
        media = [p for p in g.files if p.stem == "media"]
        if g.exit_code != 0 or len(media) != 1:
            why = "timed out" if g.exit_code is None else f"exit {g.exit_code}"
            return MediaOutcome("failed", g.error or f"yt-dlp {why}, no file")
        if media[0].stat().st_size > MAX_BYTES:
            return MediaOutcome("deferred", f"over the {MAX_BYTES // 2**20} MiB cap")
        files = {f"media{media[0].suffix}": media[0]}
        thumbs = [p for p in g.files if p.stem == "thumbnail"]
        if thumbs:
            files[f"thumbnail{thumbs[0].suffix}"] = thumbs[0]
        got = MediaOutcome(
            "ok", files=files, info={k: g.info[k] for k in INFO_KEEP if k in g.info},
            details={"proxy": g.stats.to_json(), "downloader": _ytdlp_version()},
        )  # fmt: skip
        keep(got)
        return got


_TYPES = {
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".opus": "audio/ogg", ".ogg": "audio/ogg",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}  # fmt: skip


def _describe(path: Path) -> dict[str, Any]:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": h.hexdigest()}


def store_media(
    store: Store, folder: str, canonical: str, got: MediaOutcome, fetched_at: str
) -> None:
    for name, path in got.files.items():
        store.put_file(folder + name, path, _TYPES.get(path.suffix, "application/octet-stream"))
    put_json(store, folder + "info.json", got.info)
    put_json(
        store,
        folder + "capture.json",
        {
            "canonical": canonical,
            "via": "yt-dlp",
            "title": got.info.get("title"),
            "duration": got.info.get("duration"),
            "format": got.info.get("format"),
            **got.details,
            "files": {name: _describe(path) for name, path in got.files.items()},
            "fetched_at": fetched_at,
            "tool": f"dea-capture {__version__}",
        },
    )
