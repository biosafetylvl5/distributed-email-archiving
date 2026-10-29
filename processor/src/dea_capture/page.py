"""capture-page: one page as a browser renders it, with browsertrix-crawler.

The crawler loads the canonical URL alone (no other pages, no links followed)
in its own browser, runs its scroll and autoplay behaviours, and writes a
WACZ. Its browser and its own downloads go through ``GuardProxy``, so they
reach public addresses only. From the WACZ this keeps the page's entry
(status, title), the text as it stood after the behaviours ran (or at load,
when that didn't change), and the viewport screenshot.

Stored in the attempt's folder under ``captures/<h[:2]>/<h>/<stamp>/``:
``page.wacz``, ``screenshot.png`` and ``text.txt`` when there are any, and
``capture.json``.

The crawler's own log names every URL the page loaded. It is kept in the
temporary directory and never printed; when a crawl fails, its error lines,
with every URL cut down to its host, become the attempt's error.
"""

from __future__ import annotations

import contextlib
import ctypes
import gzip
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
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
    final_text: str | None = None  # after the behaviours; None if unchanged from load
    stats: ProxyStats = field(default_factory=ProxyStats)
    log_errors: str | None = None  # see log_errors()


Crawler = Callable[[str, Path], Crawl]


def crawl_args(url: str, workdir: Path, proxy_port: int, user_agent_suffix: str) -> list[str]:
    return [
        CRAWL, "--url", url, "--scopeType", "page", "--limit", "1", "--workers", "1",
        "--headless", "--generateWACZ", "--screenshot", "view", "--text", "to-pages,final-to-warc",
        "--collection", COLLECTION, "--cwd", str(workdir), "--saveState", "never",
        "--pageLoadTimeout", str(PAGE_LOAD_TIMEOUT), "--behaviorTimeout", str(BEHAVIOR_TIMEOUT),
        "--timeLimit", str(TIME_LIMIT), "--sizeLimit", str(SIZE_LIMIT),
        "--proxyServer", f"http://127.0.0.1:{proxy_port}",
        # without this the browser reaches localhost directly
        "--extraChromeArgs=--proxy-bypass-list=<-loopback>",
        "--userAgentSuffix", user_agent_suffix,
    ]  # fmt: skip


def _crawler_env(workdir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SECRET_ENV)}
    # The image detaches the crawler's redis and browser into sessions of
    # their own, so they outlive it; a later crawl would then find the old
    # redis on its fixed port, state and all. Keep them in our process group,
    # where killpg ends them. Redis also saves to, and reloads from, its cwd,
    # which is the temp dir: make that this crawl's directory.
    env["DETACHED_CHILD_PROC"] = "0"
    env["TMPDIR"] = str(workdir)
    return env


def _become_subreaper() -> None:
    """Adopt the crawler's orphans, so they're reaped here, not left as
    zombies under PID 1 (courier, which doesn't reap them)."""
    if sys.platform == "linux":
        with contextlib.suppress(OSError, AttributeError):
            ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _end_group(pgid: int, grace: float = 10.0) -> None:
    """TERM, then KILL, everything in the crawler's process group, until none is left."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            _reap()
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)


_URL = re.compile(r"""https?://([^/\s"'<>?#]+)[^\s"'<>]*""")
_ADDRESS = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# what failed and why, from a log line's details
_DETAIL_KEYS = ("type", "msg", "reason", "status", "loadState", "errorText", "errorMessage")
# the crawler says this whenever its one page failed: no news
_GENERIC = ("Crawl failed, no pages crawled successfully",)
LOG_ERRORS_MAX = 300


def _mask(text: str) -> str:
    """Every URL down to its host, and no mail addresses."""
    return _ADDRESS.sub("<address>", _URL.sub(r"<\1>", text))


def log_errors(log: Path) -> str | None:
    """The distinct warnings and errors in a crawler log (one JSON object a
    line), URLs masked, cut to ``LOG_ERRORS_MAX`` characters; None if none."""
    found: list[str] = []
    try:
        lines = log.read_text("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("logLevel") not in {"warn", "error", "fatal"}:
            continue
        message = str(entry.get("message", "")).strip()
        if not message or message.startswith(_GENERIC):
            continue
        details = entry.get("details")
        extra = []
        if isinstance(details, dict):
            extra = [f"{k}={_mask(str(details[k]))}" for k in _DETAIL_KEYS
                     if details.get(k) not in (None, "")]  # fmt: skip
        text = _mask(message) + (f" ({', '.join(extra)})" if extra else "")
        if text not in found:
            found.append(text)
    if not found:
        return None
    joined = "; ".join(found)
    return joined if len(joined) <= LOG_ERRORS_MAX else joined[: LOG_ERRORS_MAX - 1] + "…"


def run_crawler(url: str, workdir: Path, proxy: GuardProxy, user_agent_suffix: str) -> Crawl:
    """Run browsertrix-crawler on ``url`` behind ``proxy`` (started here)."""
    _become_subreaper()
    port = proxy.start()
    try:
        with (workdir / "crawl.log").open("wb") as log:
            proc = subprocess.Popen(
                crawl_args(url, workdir, port, user_agent_suffix),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                cwd=workdir, env=_crawler_env(workdir), start_new_session=True,
            )  # fmt: skip
            try:
                code: int | None = proc.wait(KILL_AFTER)
            except subprocess.TimeoutExpired:
                code = None
            # the crawler leaves its redis and browser running when it exits
            _end_group(proc.pid)
    finally:
        proxy.stop()
    wacz = workdir / "collections" / COLLECTION / f"{COLLECTION}.wacz"
    errors = log_errors(workdir / "crawl.log")
    if not wacz.is_file():
        return Crawl(code, None, None, None, stats=proxy.stats, log_errors=errors)
    got = read_wacz(wacz, url)
    return Crawl(code, wacz, got.page, got.screenshot, final_text=got.final_text,
                 stats=proxy.stats, log_errors=errors)  # fmt: skip


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


@dataclass
class WaczParts:
    page: dict[str, Any] | None = None  # the URL's pages.jsonl entry
    screenshot: bytes | None = None
    final_text: str | None = None


def read_wacz(path: Path, url: str) -> WaczParts:
    """What we keep beside the WACZ, from inside it."""
    page: dict[str, Any] | None = None
    shot: bytes | None = None
    final: str | None = None
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "pages/pages.jsonl" in names:
            for line in z.read("pages/pages.jsonl").splitlines():
                with contextlib.suppress(ValueError):
                    entry = json.loads(line)
                    is_page = isinstance(entry, dict) and "url" in entry and "format" not in entry
                    if is_page and (page is None or entry.get("url") == url):
                        page = entry
        # the crawler writes its screenshot and text records to WARCs of their own
        for name in names:
            if not name.startswith("archive/") or not ("screenshot" in name or "text" in name):
                continue
            for headers, block in _warc_records(z.read(name)):
                target = headers.get("warc-target-uri", "")
                if shot is None and target.startswith("urn:view:"):
                    shot = block
                elif target.startswith("urn:textFinal:"):
                    final = block.decode("utf-8", "replace")
    return WaczParts(page, shot, final)


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


def _why(error: str, c: Crawl) -> str:
    return f"{error}: {c.log_errors}" if c.log_errors else error


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
            return PageOutcome("failed", error=_why(f"{why}, no page recorded", c))
        if status is not None and not 200 <= status < 300:
            return PageOutcome("failed", status, f"HTTP {status}")
        if not c.page.get("loadState"):
            return PageOutcome("failed", status, _why("page didn't load", c))
        files: dict[str, bytes] = {"page.wacz": c.wacz.read_bytes()}
        if c.screenshot:
            files["screenshot.png"] = c.screenshot
        text = c.final_text if c.final_text and c.final_text.strip() else c.page.get("text")
        if isinstance(text, str) and text.strip():
            files["text.txt"] = text.encode()
        info = {
            "title": c.page.get("title"),
            "mime": c.page.get("mime"),
            "load_state": c.page.get("loadState"),
            "text_from": "final" if c.final_text and c.final_text.strip() else "load",
            "crawler_exit": c.exit_code,
            "limit_hit": LIMIT_EXITS.get(c.exit_code or 0),
            "proxy": c.stats.to_json(),
            "crawler": _crawler_version(),
        }
        return PageOutcome("ok", status, files=files, info=info)


_TYPES = {".wacz": "application/wacz", ".png": "image/png", ".txt": "text/plain; charset=utf-8"}


def store_page(
    store: Store, folder: str, canonical: str, got: PageOutcome, fetched_at: str, *,
    via: str = "browsertrix", snapshot: str | None = None,
) -> None:  # fmt: skip
    """``via`` and ``snapshot``: where the page came from, if not ``canonical``
    itself (``wayback:<timestamp>`` and the replay URL that was crawled)."""
    for name, data in got.files.items():
        store.put(folder + name, data, _TYPES[Path(name).suffix])
    put_json(
        store,
        folder + "capture.json",
        {
            "canonical": canonical,
            "via": via,
            **({"snapshot": snapshot} if snapshot else {}),
            "http_status": got.http_status,
            **got.info,
            "files": {name: _file(data) for name, data in got.files.items()},
            "fetched_at": fetched_at,
            "tool": f"dea-capture {__version__}",
        },
    )
