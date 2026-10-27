"""Smoke test for capture-page, run inside the built image by CI.

1. The browser can't reach loopback: crawl a server on 127.0.0.1 (skipping
   crawl_page's own check, so only the proxy stands in the way) and expect
   the proxy to refuse it and the server to see no request.
2. A real page is captured through the proxy: https://example.com/ gives a
   WACZ and a screenshot, and the proxy opened tunnels for it.

    docker run --rm --read-only --tmpfs /tmp --user 1000 \\
      -v "$PWD/processor/deploy/smoke-page.py:/smoke.py:ro" IMAGE python3 /smoke.py
"""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
from pathlib import Path

from dea_capture import page
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
print("smoke ok")
sys.exit(0)
