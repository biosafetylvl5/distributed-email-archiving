"""Smoke test for capture-page, capture-media and capture-gallery, run inside
the built image by CI.

1. The browser can't reach loopback: crawl a server on 127.0.0.1 (skipping
   crawl_page's own check, so only the proxy stands in the way) and expect
   the proxy to refuse it and the server to see no request.
2. A real page is captured through the proxy: https://example.com/ gives a
   WACZ and a screenshot, and the proxy opened tunnels for it.
3. capture-media's tools run (yt-dlp, ffmpeg, node), and yt-dlp can't reach
   loopback either. Then a short public video is grabbed; CI's addresses are
   often refused by video sites, so that part only reports what happened.
4. gallery-dl runs and can't reach loopback.

    docker run --rm --read-only --tmpfs /tmp --user 1000 \\
      -v "$PWD/processor/deploy/smoke-page.py:/smoke.py:ro" IMAGE python3 /smoke.py
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from dea_capture import gallery, media, page
from dea_capture.proxy import GuardProxy

hits: list[str] = []


def log_tail(workdir: Path) -> None:
    """The crawler's warnings and errors, and its last lines (smoke URLs only)."""
    try:
        lines = (workdir / "crawl.log").read_text(errors="replace").splitlines()
    except OSError as exc:
        print("no crawl.log:", exc)
        return
    keep = [ln for ln in lines if '"logLevel":"error"' in ln or '"logLevel":"fatal"' in ln
            or '"logLevel":"warn"' in ln]  # fmt: skip
    for ln in [*keep[-15:], "--- last lines", *lines[-10:]]:
        print("  crawl.log:", ln[:400])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>local</html>")


local = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=local.serve_forever, daemon=True).start()
local_url = f"http://127.0.0.1:{local.server_address[1]}/"

with tempfile.TemporaryDirectory() as tmp:
    c = page.run_crawler(local_url, Path(tmp), GuardProxy(), "dea-capture-smoke")
    loopback = {"exit": c.exit_code, "hits": len(hits), "refused": dict(c.stats.refused)}
    print("loopback", json.dumps(loopback))
    if hits or not c.stats.refused.get("127.0.0.1"):
        log_tail(Path(tmp))
assert not hits, "the browser reached loopback"
assert c.stats.refused.get("127.0.0.1"), "the proxy didn't see the loopback request"


def crawler(url: str, workdir: Path) -> page.Crawl:
    c = page.run_crawler(url, workdir, GuardProxy(), "dea-capture-smoke")
    seen = {"exit": c.exit_code, "proxy": c.stats.to_json(), "page": bool(c.page),
            "wacz": bool(c.wacz)}  # fmt: skip
    print("example.com crawl", json.dumps(seen))
    if not c.page:
        log_tail(workdir)
    return c


got = page.crawl_page("https://example.com/", crawler)
summary = {
    "outcome": got.outcome, "status": got.http_status, "error": got.error,
    "files": {k: len(v) for k, v in got.files.items()}, "info": got.info,
}  # fmt: skip
print("example.com", json.dumps(summary))
assert got.outcome == "ok", got.error
assert got.files["page.wacz"][:2] == b"PK"
assert got.files["screenshot.png"][:8] == b"\x89PNG\r\n\x1a\n"
assert got.info["proxy"]["tunnels"] > 0

for tool in ([sys.executable, "-m", "yt_dlp", "--version"], [sys.executable, "-m", "gallery_dl",
             "--version"], ["ffmpeg", "-version"], ["node", "--version"]):  # fmt: skip
    out = subprocess.run(tool, capture_output=True, text=True, check=True).stdout
    print(tool[-2] if tool[0] == sys.executable else tool[0], out.splitlines()[0][:80])

before = len(hits)
with tempfile.TemporaryDirectory() as tmp:
    proxy = GuardProxy()
    g = media.run_ytdlp(local_url + "clip.mp4", Path(tmp), proxy)
    print("media loopback", json.dumps({"info": g.info is not None, "hits": len(hits) - before,
                                        "refused": dict(proxy.stats.refused)}))  # fmt: skip
assert len(hits) == before, "yt-dlp reached loopback"
assert g.info is None and proxy.stats.refused.get("127.0.0.1"), "the proxy didn't refuse yt-dlp"

kept: dict[str, int] = {}


def grabber(url: str, workdir: Path, limit: int | None) -> media.Grab:
    return media.run_ytdlp(url, workdir, GuardProxy(), limit)


def keep(m: media.MediaOutcome) -> None:
    kept.update({name: path.stat().st_size for name, path in m.files.items()})


video = media.grab_media("https://www.youtube.com/watch?v=jNQXAC9IVRw", grabber, keep)
print("youtube (informational)", json.dumps({"outcome": video.outcome, "error": video.error,
                                             "files": kept, "format": video.info.get("format"),
                                             **video.details}))  # fmt: skip
before = len(hits)
with tempfile.TemporaryDirectory() as tmp:
    proxy = GuardProxy()
    # an image URL, which gallery-dl's directlink extractor would fetch
    h = gallery.run_gallerydl(local_url + "image.jpg", Path(tmp), proxy)
    print("gallery loopback", json.dumps({"exit": h.exit_code, "hits": len(hits) - before,
                                          "refused": dict(proxy.stats.refused)}))  # fmt: skip
assert len(hits) == before, "gallery-dl reached loopback"
assert not h.files and proxy.stats.refused.get("127.0.0.1"), "the proxy didn't refuse gallery-dl"

print("smoke ok")
sys.exit(0)
