"""capture-gallery: one social media post's images, videos and text, with gallery-dl.

gallery-dl runs once, behind ``GuardProxy``, with no account and no cookies.
It saves the post's files at their original resolution, each file's metadata,
and the post's own (``info.json``: its text, author, date and counts). A post
whose site wants a login, or that gallery-dl has no extractor for, goes to
capture-page instead. At most ``MAX_FILES`` files and ``MAX_BYTES`` in all are
kept; a post that grows past that while downloading is deferred.

Stored in the attempt's folder under ``captures/<h[:2]>/<h>/<stamp>/``:
``files/001.<ext>``... in the post's order, ``info.json`` (``post``: the
post's metadata; ``files``: each file's, by stored name) and ``capture.json``.
Signed CDN URLs in the metadata lose their query strings.

gallery-dl's log names the post's URLs. It is kept in the temporary directory
and never printed.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from . import __version__
from .guard import ForbiddenFetch, IPNetwork, check_destination, check_link
from .media import _TYPES, MediaOutcome, _describe, _last_error, _run
from .page import _become_subreaper
from .proxy import GuardProxy, ProxyStats
from .store import Store, put_json

MAX_FILES = 100
MAX_BYTES = 3 * 1024**3
TIMEOUT = 30 * 60

# gallery-dl's exit status is a bitmask of what went wrong
EXIT_LOGIN = 16  # the site wants an account
EXIT_NO_EXTRACTOR = 64

_ERROR = re.compile(r"^\[[^\]]*\]\[error\] (.*)$", re.M)
_LOGIN = re.compile(r"login|log in|sign in|account", re.I)


@dataclass
class Haul:
    """What gallery-dl did with one URL."""

    exit_code: int | None  # None: killed, at its timeout or over the size cap
    post: dict[str, Any] | None  # the post's metadata (its info.json)
    files: list[tuple[Path, dict[str, Any]]] = field(default_factory=list)  # file, its metadata
    over_cap: bool = False
    error: str | None = None  # gallery-dl's last error line
    stats: ProxyStats = field(default_factory=ProxyStats)


Collector = Callable[[str, Path], Haul]


def gallerydl_args(url: str, workdir: Path, proxy_port: int) -> list[str]:
    return [
        sys.executable, "-m", "gallery_dl", "--config-ignore",
        "--proxy", f"http://127.0.0.1:{proxy_port}",
        "-D", str(workdir / "out"), "--write-metadata", "--write-info-json",
        "--range", f"1-{MAX_FILES}", "--no-mtime", "--no-part",
        "--http-timeout", "60", "--retries", "3", "--sleep-request", "1",
        "-o", "cache.file=", "-o", "extractor.twitter.text-tweets=true",
        # videos gallery-dl hands to yt-dlp are fetched by yt-dlp, never ffmpeg
        "-o", 'downloader.ytdl.raw-options={"external_downloader": {"default": "native"}}',
        url,
    ]  # fmt: skip


def _natural(path: Path) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path.name)]


def _load(path: Path) -> dict[str, Any] | None:
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def run_gallerydl(url: str, workdir: Path, proxy: GuardProxy) -> Haul:
    """Download ``url``'s post with gallery-dl behind ``proxy``."""
    _become_subreaper()
    out = workdir / "out"
    out.mkdir()
    port = proxy.start()
    try:
        code = _run(gallerydl_args(url, workdir, port), workdir, "gallery-dl.log", TIMEOUT,
                    None, stop=lambda: _size(out) > MAX_BYTES)  # fmt: skip
    finally:
        proxy.stop()
    files = sorted((p for p in out.iterdir() if p.is_file() and p.suffix != ".json"), key=_natural)
    return Haul(
        code, _load(out / "info.json"),
        [(p, _load(p.with_name(p.name + ".json")) or {}) for p in files],
        over_cap=_size(out) > MAX_BYTES,
        error=_last_error(workdir, "gallery-dl.log", _ERROR), stats=proxy.stats,
    )  # fmt: skip


# query parameters that sign or identify one request for a file
_SIGNED = re.compile(
    r"^(oh|oe|_nc_.*|sig|signature|token|expires?|x-amz-.*|policy|key-pair-id|hmac)$", re.I
)


def scrub(value: Any) -> Any:
    """``value`` with signed URLs' query strings removed, all the way down."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")) and "?" in value:
        parts = urlsplit(value)
        if any(_SIGNED.match(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
            return urlunsplit(parts._replace(query=""))
    return value


def _gallerydl_version() -> str:
    try:
        return f"gallery-dl {metadata.version('gallery-dl')}"
    except metadata.PackageNotFoundError:
        return "gallery-dl (unknown)"


def collect(
    canonical: str, collector: Collector, keep: Callable[[MediaOutcome], None],
    extra_blocked: Sequence[IPNetwork] = (),
) -> MediaOutcome:  # fmt: skip
    """Collect ``canonical``'s post into a temporary directory and judge the
    result. ``keep`` is called with a good outcome while its files still exist."""
    try:
        check_link(canonical)
        check_destination(canonical, extra_blocked=extra_blocked)
    except ForbiddenFetch as exc:
        return MediaOutcome("refused", str(exc))
    with tempfile.TemporaryDirectory(prefix="dea-gallery-") as tmp:
        h = collector(canonical, Path(tmp))
        why = h.error or ("timed out" if h.exit_code is None else f"gallery-dl exit {h.exit_code}")
        if h.over_cap:
            return MediaOutcome("deferred", f"over {MAX_BYTES // 2**20} MiB")
        code = h.exit_code or 0
        if code & EXIT_NO_EXTRACTOR:
            return MediaOutcome("rerouted", "not a post gallery-dl knows")
        if code & EXIT_LOGIN or (h.error and _LOGIN.search(h.error)):
            return MediaOutcome("rerouted", f"wants a login: {why}")
        if h.exit_code != 0 or (h.post is None and not h.files):
            return MediaOutcome("failed", why)
        names = {f"files/{i:03}{p.suffix.lower()}": (p, m) for i, (p, m) in enumerate(h.files, 1)}
        got = MediaOutcome(
            "ok", files={n: p for n, (p, _) in names.items()},
            info={"post": scrub(h.post or {}),
                  "files": {n: scrub(m) for n, (_, m) in names.items()}},
            details={"proxy": h.stats.to_json(), "downloader": _gallerydl_version()},
        )  # fmt: skip
        keep(got)
        return got


def store_gallery(
    store: Store, folder: str, canonical: str, got: MediaOutcome, fetched_at: str
) -> None:
    for name, path in got.files.items():
        ctype = _TYPES.get(path.suffix.lower(), "application/octet-stream")
        store.put_file(folder + name, path, ctype)
    put_json(store, folder + "info.json", got.info)
    post = got.info.get("post", {})
    put_json(
        store,
        folder + "capture.json",
        {
            "canonical": canonical,
            "via": "gallery-dl",
            "site": post.get("category"),
            **got.details,
            "files": {name: _describe(path) for name, path in got.files.items()},
            "fetched_at": fetched_at,
            "tool": f"dea-capture {__version__}",
        },
    )
