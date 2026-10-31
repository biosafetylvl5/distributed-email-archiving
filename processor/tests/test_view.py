from __future__ import annotations

import hashlib
import json
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from dea_capture.classify import tracking_vendor
from dea_capture.extract import rewrite_css
from dea_capture.fetch import HostPacer
from dea_capture.guard import GuardedFetcher
from dea_capture.inventory import Inventory
from dea_capture.issue import manifest_key, process_issue
from dea_capture.store import LocalStore
from dea_capture.view import VIEW_VERSION, render, view_current, view_meta_key, view_prefix

from .conftest import EML_FIXTURES, FORBIDDEN_MARKERS
from .test_capture import Web, _guarded

A = "a" * 64
CSS = "c" * 64
FONT = "f" * 64
CID = "d" * 64

HOSTILE = """<html><head>
<meta http-equiv="refresh" content="0;url=https://evil.example.com/">
<base href="https://evil.example.com/">
<link rel="stylesheet" href="https://cdn.example.com/s.css">
<link rel="dns-prefetch" href="https://evil.example.com/">
<script>alert(1)</script>
<style>@import "https://cdn.example.com/s.css";
.a{background:url('https://cdn.example.com/a.png')}
.b{background:url(https://elsewhere.example.com/b.png)}
.c{background:url(data:image/gif;base64,R0lGOD)}</style>
</head><body onload="alert(2)">
<img src="https://cdn.example.com/a.png" onerror="x()"
 srcset="https://cdn.example.com/a.png 1x, https://elsewhere.example.com/b.png 2x">
<img src="https://track.example.com/open.gif?u=SECRET" width="1" height="1">
<img src="cid:logo@x">
<div style="background-image:url(https://cdn.example.com/a.png)">hi</div>
<iframe src="https://evil.example.com/"></iframe>
<form action="https://evil.example.com/"><input name="q"></form>
<svg><image href="https://evil.example.com/i.png"/></svg>
<a href="https://news.example.com/story?utm_source=x" ping="https://evil.example.com/">story</a>
<a href="https://list.example.com/unsubscribe?u=SECRET">unsubscribe</a>
<a href="mailto:reader@example.org">mail</a>
<a href="#top">top</a>
<!--[if mso]><v:rect><v:fill src="https://evil.example.com/bg.png"/></v:rect>
<a href="https://evil.example.com/c?u=SECRET">x</a><![endif]-->
<!--[if !mso]><!--><p>for everyone else</p><!--<![endif]-->
</body></html>"""


def _hostile() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Writer <writer@example.com>"
    msg["To"] = "reader@example.org"
    msg["Subject"] = "Hostile =?utf-8?q?caf=C3=A9?="
    msg.set_content("plain")
    msg.add_alternative(HOSTILE, subtype="html")
    html = msg.get_payload()[1]  # type: ignore[index]
    assert isinstance(html, EmailMessage)
    html.add_related(b"GIF89a", "image", "gif", cid="<logo@x>")
    return msg.as_bytes()


def _manifest() -> dict[str, Any]:
    def ok(url: str, digest: str, ext: str, via: str, **kw: Any) -> dict[str, Any]:
        return {"url": url, "via": via, "pixel": False, "status": "ok", "bytes": 10,
                "key": f"issues/x/assets/{digest}{ext}", **kw}  # fmt: skip

    return {
        "source": "s3://bucket/incoming/archive/2026-10-01/x.eml",
        "assets": [
            ok("https://cdn.example.com/a.png", A, ".png", "img"),
            ok("https://cdn.example.com/s.css", CSS, ".css", "stylesheet"),
            ok(
                "https://cdn.example.com/fonts/f.woff2",
                FONT,
                ".woff2",
                "css",
                found_in="https://cdn.example.com/s.css",
            ),
            ok("cid:logo@x", CID, ".gif", "cid"),
            {
                "url": "https://track.example.com/open.gif?u=SECRET",
                "via": "img",
                "pixel": True,
                "status": "refused",
            },
        ],
    }


def test_rewrite_css() -> None:
    css = (
        "/* url(https://x.example.com/c.png) */ @import url('a.css');"
        ' @import "https://o.example.com/b.css";'
        ".x{background:url(i.png)} .y{src:url( 'data:font/woff2;base64,AA' )}"
        " .z{b:url(javascript:x)}"
    )
    seen: list[tuple[str | None, bool]] = []

    def to(url: str | None, imp: bool) -> str:
        seen.append((url, imp))
        return "LOCAL"

    out = rewrite_css(css, "https://cdn.example.com/css/main.css", to)
    assert seen == [
        ("https://cdn.example.com/css/a.css", True),
        ("https://o.example.com/b.css", True),
        ("https://cdn.example.com/css/i.png", False),
        (None, False),
    ]
    assert "x.example.com" not in out and "data:font/woff2" in out
    assert out.count('url("LOCAL")') == 4


def test_render_loads_nothing_from_elsewhere(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    css = b"@font-face{src:url(fonts/f.woff2)} .q{background:url(/nope.png)}"
    store.put(f"issues/x/assets/{CSS}.css", css, "text/css")
    captures = {"links": [{"href": "https://news.example.com/story?utm_source=x", "outcome": "ok",
                           "canonical": "https://news.example.com/story"}]}  # fmt: skip
    v = render(_hostile(), store, _manifest(), captures)
    out = v.html.decode()
    gone = [
        "<script",
        "<iframe",
        "<form",
        "<meta http-equiv",
        "<base",
        "onload",
        "onerror",
        "ping=",
        "dns-prefetch",
        "evil.example.com",
        "SECRET",
        "elsewhere.example.com",
    ]
    for g in gone:
        assert g not in out, g
    assert "<!--" not in out and "<p>for everyone else</p>" in out
    # every src, srcset, href and url() left is ours, an anchor, or a resolved link
    refs = re.findall(r'(?:src|srcset|href)="([^"]*)"', out) + re.findall(r'url\("([^"]*)"\)', out)
    for ref in refs:
        assert ref.startswith(("assets/", "css/", "#", "data:", "about:invalid",
                               "https://news.example.com/story")), ref  # fmt: skip
    assert f'src="assets/{A}.png"' in out and f'src="assets/{CID}.gif"' in out
    css_name = next(iter(v.css))
    assert f'href="css/{css_name}"' in out
    assert 'href="https://news.example.com/story" rel="noopener noreferrer" target="_blank"' in out
    assert 'title="Link removed: an unsubscribe link"' in out
    assert "srcset" not in out  # one candidate wasn't stored
    # the stylesheet points at the font next to it, nothing else
    font, nope = f'url("../assets/{FONT}.woff2")', 'url("about:invalid")'
    assert v.css == {css_name: f"@font-face{{src:{font}}} .q{{background:{nope}}}".encode()}
    assert (
        v.removed["pixels"] == 1
        and v.removed["links_removed"] == 2
        and v.removed["links_kept"] == 1
    )
    m = v.meta
    assert m["view_version"] == VIEW_VERSION and m["body"] == "html"
    assert m["headers"]["subject"] == "Hostile café"
    assert m["sizes"] == {"eml": len(_hostile()), "assets": 40, "asset_files": 4}
    assert {p["content_type"] for p in m["parts"]} == {"text/plain", "text/html", "image/gif"}


def test_text_only_messages_are_escaped(tmp_path: Path) -> None:
    msg = EmailMessage()
    msg["Subject"] = "plain"
    msg["To"] = "reader@example.org"
    msg.set_content(
        "<b>not bold</b> https://substack.com/redirect/abc?j=SECRET "
        "https://example.com/article?token=SECRET&lang=en."
    )
    v = render(msg.as_bytes(), LocalStore(tmp_path), {"assets": []}, None)
    out = v.html.decode()
    assert "&lt;b&gt;not bold&lt;/b&gt;" in out and "<a " not in out
    assert "SECRET" not in out
    assert "https://example.com/article?lang=en." in out
    assert "to" not in v.meta["headers"]
    assert "reader@example.org" not in json.dumps(v.meta)


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_the_issue_stage_renders_the_view(path: Path, tmp_path: Path) -> None:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    store = LocalStore(tmp_path)
    web = Web(trackers_land=True)

    def make(inv: Inventory) -> GuardedFetcher:
        return _guarded(web, inv.refused_urls())

    result = process_issue(raw, store, make, source="s3://bucket/incoming/archive/x.eml",
                           pacer=HostPacer(0), make_resolver=make)  # fmt: skip
    assert result.counts["view"] == 1
    out = (store.get(view_prefix(sha) + "index.html") or b"").decode()
    for m in FORBIDDEN_MARKERS:
        assert m not in out, m
    for href in re.findall(r'href="(https?://[^"]*)"', out):
        assert not tracking_vendor(href.replace("&amp;", "&")), href
    for ref in re.findall(r'(?:src|background|poster)="([^"]*)"', out):
        assert ref.startswith(("assets/", "data:")), ref
        if ref.startswith("assets/"):
            assert store.exists(f"issues/{sha}/{ref}")
    assert view_current(store, sha)

    # an issue done before views existed gets one when it's seen again
    store.delete(view_meta_key(sha))
    again = process_issue(raw, store, make, source="s3://bucket/incoming/archive/x.eml",
                          pacer=HostPacer(0), make_resolver=make)  # fmt: skip
    assert (again.skipped, again.counts) == (False, {"view": 1})
    requested = len(web.requested)
    third = process_issue(raw, store, make, source="s3://bucket/incoming/archive/x.eml",
                          pacer=HostPacer(0), make_resolver=make)  # fmt: skip
    assert third.skipped and len(web.requested) == requested
    assert json.loads(store.get(manifest_key(sha)) or b"")["sha256"] == sha


def test_srcset_candidates_with_shared_prefix(tmp_path: Path) -> None:
    msg = EmailMessage()
    url = "https://example.com/a.png"
    msg.set_content(f'<img srcset="{url} 1x, {url}?v=2 2x">', subtype="html")
    manifest = {
        "assets": [
            {"url": u, "status": "ok", "via": "img", "key": f"issues/x/assets/{name}.png"}
            for u, name in [(url, A), (url + "?v=2", CID)]
        ]
    }
    out = render(msg.as_bytes(), LocalStore(tmp_path), manifest, None).html.decode()
    assert f'srcset="assets/{A}.png 1x, assets/{CID}.png 2x"' in out


def test_identical_stylesheets_with_different_bases(tmp_path: Path) -> None:
    from bs4 import BeautifulSoup

    msg = EmailMessage()
    urls = [f"https://example.com/{base}/site.css" for base in ("one", "two")]
    msg.set_content(
        "".join(f'<link rel="stylesheet" href="{url}">' for url in urls), subtype="html"
    )
    css_key = f"issues/x/assets/{CSS}.css"
    store = LocalStore(tmp_path)
    store.put(css_key, b".logo{background:url(logo.png)}", "text/css")
    assets: list[dict[str, Any]] = []
    for url, digest in zip(urls, [A, CID], strict=True):
        assets.extend(
            [
                {"url": url, "status": "ok", "via": "stylesheet", "key": css_key},
                {
                    "url": url.replace("site.css", "logo.png"),
                    "status": "ok",
                    "via": "css",
                    "key": f"issues/x/assets/{digest}.png",
                },
            ]
        )
    v = render(msg.as_bytes(), store, {"assets": assets}, None)
    soup = BeautifulSoup(v.html, "html.parser")
    names = [str(link.get("href")).removeprefix("css/") for link in soup.find_all("link")]
    assert len(set(names)) == 2
    for name, digest in zip(names, [A, CID], strict=True):
        assert v.css[name] == f'.logo{{background:url("../assets/{digest}.png")}}'.encode()
