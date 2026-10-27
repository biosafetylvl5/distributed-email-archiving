"""capture-page: one page as a browser renders it, with browsertrix-crawler.

The crawler loads the canonical URL alone (no other pages, no links followed)
in its own browser, runs its scroll and autoplay behaviours, and writes a
WACZ. Its browser and its own downloads go through ``GuardProxy``, so they
reach public addresses only. From the WACZ this keeps the page's entry
(status, title, extracted text) and the viewport screenshot.

Stored in the attempt's folder under ``captures/<h[:2]>/<h>/<stamp>/``:
``page.wacz``, ``screenshot.png`` and ``text.txt`` when there are any, and
``capture.json``.

The crawler's own log names every URL the page loaded. It is kept in the
temporary directory and never printed.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .guard import ForbiddenFetch, IPNetwork, check_destination, check_link
from .proxy import GuardProxy, ProxyStats
from .store import Store, put_json

CRAWL = "crawl"
PAGE_LOAD_TIMEOUT = 60
BEHAVIOR_TIMEOUT = 60
# the crawler stops itself at these; we kill it a while after
TIME_LIMIT = 240
SIZE_LIMIT = 200 * 1024 * 1024
KILL_AFTER = TIME_LIMIT + 180
LIMIT_EXITS = {14: "size limit", 15: "time limit"}
COLLECTION = "page"

# environment the crawler and its browser never see
_SECRET_ENV = ("AWS_", "COURIER_")


@dataclass
class Crawl:
    exit_code: int | None  # None: killed after KILL_AFTER seconds
    wacz: Path | None
    page: dict[str, Any] | None  # the URL's entry in the WACZ's pages.jsonl
    screenshot: bytes | None
    stats: ProxyStats = field(default_factory=ProxyStats)


Crawler = Callable[[str, Path], Crawl]


def crawl_args(url: str, workdir: Path, proxy_port: int, user_agent_suffix: str) -> list[str]:
    return [
        CRAWL, "--url", url, "--scopeType", "page", "--limit", "1", "--workers", "1",
        "--headless", "--generateWACZ", "--screenshot", "view", "--text", "to-pages",
        "--collection", COLLECTION, "--cwd", str(workdir), "--saveState", "never",
        "--pageLoadTimeout", str(PAGE_LOAD_TIMEOUT), "--behaviorTimeout", str(BEHAVIOR_TIMEOUT),
        "--timeLimit", str(TIME_LIMIT), "--sizeLimit", str(SIZE_LIMIT),
        "--proxyServer", f"http://127.0.0.1:{proxy_port}",
        # without this the browser reaches localhost directly
        "--extraChromeArgs=--proxy-bypass-list=<-loopback>",
        "--userAgentSuffix", user_agent_suffix,
    ]  # fmt: skip


def _crawler_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_SECRET_ENV)}


def run_crawler(url: str, workdir: Path, proxy: GuardProxy, user_agent_suffix: str) -> Crawl:
    """Run browsertrix-crawler on ``url`` behind ``proxy`` (started here)."""
    port = proxy.start()
    try:
        with (workdir / "crawl.log").open("wb") as log:
            proc = subprocess.Popen(
                crawl_args(url, workdir, port, user_agent_suffix),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                cwd=workdir, env=_crawler_env(), start_new_session=True,
            )  # fmt: skip
            try:
                code: int | None = proc.wait(KILL_AFTER)
            except subprocess.TimeoutExpired:
                code = None
            # the crawler leaves its browser and redis behind if it dies
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, sig)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(10)
                    if sig == signal.SIGTERM:
                        break
    finally:
        proxy.stop()
    wacz = workdir / "collections" / COLLECTION / f"{COLLECTION}.wacz"
    page, shot = read_wacz(wacz, url) if wacz.is_file() else (None, None)
    return Crawl(code, wacz if wacz.is_file() else None, page, shot, proxy.stats)


def _warc_records(data: bytes) -> Iterator[tuple[dict[str, str], bytes]]:
    raw = gzip.decompress(data)
    pos = 0
    while (start := raw.find(b"WARC/1.", pos)) >= 0:
        end = raw.find(b"\r\n\r\n", start)
        if end < 0:
            return
        headers: dict[str, str] = {}
        for line in raw[start:end].split(b"\r\n")[1:]:
            name, _, value = line.decode("utf-8", "replace").partition(":")
            headers[name.strip().lower()] = value.strip()
        try:
            n = int(headers.get("content-length", ""))
        except ValueError:
            return
        yield headers, raw[end + 4 : end + 4 + n]
        pos = end + 4 + n


def read_wacz(path: Path, url: str) -> tuple[dict[str, Any] | None, bytes | None]:
    """The page's pages.jsonl entry and its viewport screenshot, if there are any."""
    page: dict[str, Any] | None = None
    shot: bytes | None = None
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "pages/pages.jsonl" in names:
            for line in z.read("pages/pages.jsonl").splitlines():
                with contextlib.suppress(ValueError):
                    entry = json.loads(line)
                    is_page = isinstance(entry, dict) and "url" in entry and "format" not in entry
                    if is_page and (page is None or entry.get("url") == url):
                        page = entry
        for name in names:
            if shot is None and name.startswith("archive/") and "screenshot" in name:
                for headers, block in _warc_records(z.read(name)):
                    if headers.get("warc-target-uri", "").startswith("urn:view:"):
                        shot = block
                        break
    return page, shot


def _crawler_version() -> str:
    try:
        meta = json.loads(Path("/app/package.json").read_text())
        return f"browsertrix-crawler {meta['version']}"
    except (OSError, ValueError, KeyError):
        return "browsertrix-crawler"


def _file(data: bytes) -> dict[str, Any]:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@dataclass
class PageOutcome:
    """What capture.run records for one attempt."""

    outcome: str  # ok, failed or refused
    http_status: int | None = None
    error: str | None = None
    files: Mapping[str, bytes] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)


def crawl_page(
    canonical: str, crawler: Crawler, extra_blocked: Sequence[IPNetwork] = ()
) -> PageOutcome:
    """Crawl ``canonical`` into a temporary directory and judge the result."""
    try:
        check_link(canonical)
        check_destination(canonical, extra_blocked=extra_blocked)
    except ForbiddenFetch as exc:
        return PageOutcome("refused", error=str(exc))
    with tempfile.TemporaryDirectory(prefix="dea-page-") as tmp:
        c = crawler(canonical, Path(tmp))
        host = (urlsplit(canonical).hostname or "").lower()
        status = c.page.get("status") if c.page else None
        status = status if isinstance(status, int) else None
        if c.page is None or c.wacz is None:
            if host in c.stats.refused:
                return PageOutcome("refused", error="the proxy refused the page's host")
            why = "crawl timed out" if c.exit_code is None else f"crawl exit {c.exit_code}"
            return PageOutcome("failed", error=f"{why}, no page recorded")
        if status is not None and not 200 <= status < 300:
            return PageOutcome("failed", status, f"HTTP {status}")
        if not c.page.get("loadState"):
            return PageOutcome("failed", status, "page didn't load")
        files: dict[str, bytes] = {"page.wacz": c.wacz.read_bytes()}
        if c.screenshot:
            files["screenshot.png"] = c.screenshot
        text = c.page.get("text")
        if isinstance(text, str) and text.strip():
            files["text.txt"] = text.encode()
        info = {
            "title": c.page.get("title"),
            "mime": c.page.get("mime"),
            "load_state": c.page.get("loadState"),
            "crawler_exit": c.exit_code,
            "limit_hit": LIMIT_EXITS.get(c.exit_code or 0),
            "proxy": c.stats.to_json(),
            "crawler": _crawler_version(),
        }
        return PageOutcome("ok", status, files=files, info=info)


_TYPES = {".wacz": "application/wacz", ".png": "image/png", ".txt": "text/plain; charset=utf-8"}


def store_page(
    store: Store, folder: str, canonical: str, got: PageOutcome, fetched_at: str
) -> None:
    for name, data in got.files.items():
        store.put(folder + name, data, _TYPES[Path(name).suffix])
    put_json(
        store,
        folder + "capture.json",
        {
            "canonical": canonical,
            "via": "browsertrix",
            "http_status": got.http_status,
            **got.info,
            "files": {name: _file(data) for name, data in got.files.items()},
            "fetched_at": fetched_at,
            "tool": f"dea-capture {__version__}",
        },
    )
