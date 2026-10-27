from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import dea_capture.issue
from dea_capture.fetch import HostPacer, TooLarge
from dea_capture.guard import GuardedFetcher, IPAddress, Response
from dea_capture.inventory import Inventory, inventory
from dea_capture.issue import EXTRACT_VERSION, manifest_key, process_issue, status_key
from dea_capture.store import LocalStore, put_json

from .conftest import EML_FIXTURES, FORBIDDEN_MARKERS, fake_resolver, load


@dataclass
class FakeCDN:
    """Serves a small PNG for everything, with a few scripted exceptions."""

    missing: set[str] = field(default_factory=set)
    huge: set[str] = field(default_factory=set)
    redirects: dict[str, str] = field(default_factory=dict)
    # url -> (content type, body); everything else is a small PNG
    bodies: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)

    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
        assert not any(m in url for m in FORBIDDEN_MARKERS), url
        self.requested.append(url)
        if url in self.huge:
            raise TooLarge(10)
        if url in self.redirects:
            return Response(url, 301, {"location": self.redirects[url]}, b"")
        if url in self.missing:
            return Response(url, 404, {"content-type": "text/html"}, b"gone")
        if url in self.bodies:
            ctype, body = self.bodies[url]
            return Response(url, 200, {"content-type": ctype}, body)
        return Response(url, 200, {"content-type": "image/png"}, b"PNG:" + url.encode())


def _run(raw: bytes, store: LocalStore, cdn: FakeCDN, **kw: object) -> object:
    def make(inv: Inventory) -> GuardedFetcher:
        return GuardedFetcher(cdn, resolve=fake_resolver, refused_urls=inv.refused_urls())

    return process_issue(
        raw, store, make, source="s3://bucket/incoming/archive/x.eml",
        pacer=HostPacer(0), **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def _manifest(store: LocalStore, raw: bytes) -> dict:  # type: ignore[type-arg]
    sha = hashlib.sha256(raw).hexdigest()
    data = store.get(manifest_key(sha))
    assert data is not None
    return json.loads(data)  # type: ignore[no-any-return]


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_every_embed_is_fetched_and_stored(path: Path, tmp_path: Path) -> None:
    raw = path.read_bytes()
    store, cdn = LocalStore(tmp_path), FakeCDN()
    _run(raw, store, cdn)
    m = _manifest(store, raw)
    web = [a for a in m["assets"] if a["via"] != "cid"]
    assert {a["url"] for a in web} == set(cdn.requested)
    for a in m["assets"]:
        assert a["status"] == "ok", a
        body = store.get(a["key"])
        assert body is not None and hashlib.sha256(body).hexdigest() == a["sha256"]
    # pixels are fetched like any other embed, and flagged
    pixels = inventory(raw).summary()["tracking_pixels"]
    assert sum(a["pixel"] for a in web) == pixels
    assert pixels or path.stem == "generic"
    links = store.get(f"issues/{m['sha256']}/links.json")
    assert links is not None and json.loads(links)["source"].startswith("s3://")


def test_outcomes_are_recorded_not_raised(tmp_path: Path) -> None:
    raw = load("mailchimp.eml")
    logo = "https://mcusercontent.com/abc/images/logo.png"
    pixel = "https://example.us1.list-manage.com/track/open.php?u=aa11&id=cc33&e=bb22"
    cdn = FakeCDN(missing={logo}, redirects={pixel: "http://intranet.example.com/p.gif"})
    store = LocalStore(tmp_path)
    _run(raw, store, cdn)
    by_url = {a["url"]: a for a in _manifest(store, raw)["assets"]}
    assert (by_url[logo]["status"], by_url[logo]["error"]) == ("failed", "HTTP 404")
    assert by_url[pixel]["status"] == "refused"
    assert "non-public" in by_url[pixel]["error"]
    assert "http://intranet.example.com/p.gif" not in cdn.requested


def test_oversized_is_deferred(tmp_path: Path) -> None:
    raw = load("mailchimp.eml")
    logo = "https://mcusercontent.com/abc/images/logo.png"
    store = LocalStore(tmp_path)
    _run(raw, store, FakeCDN(huge={logo}))
    by_url = {a["url"]: a for a in _manifest(store, raw)["assets"]}
    assert by_url[logo]["status"] == "deferred:size"


def test_redirected_embed_keeps_its_chain(tmp_path: Path) -> None:
    raw = load("mailchimp.eml")
    logo = "https://mcusercontent.com/abc/images/logo.png"
    moved = "https://cdn.example.com/logo.png"
    store = LocalStore(tmp_path)
    _run(raw, store, FakeCDN(redirects={logo: moved}))
    by_url = {a["url"]: a for a in _manifest(store, raw)["assets"]}
    assert (by_url[logo]["status"], by_url[logo]["chain"]) == ("ok", [moved])


def test_done_issues_are_skipped(tmp_path: Path) -> None:
    raw = load("generic.eml")
    store = LocalStore(tmp_path)
    first = FakeCDN()
    _run(raw, store, first)
    again = FakeCDN()
    result = _run(raw, store, again)
    assert again.requested == []
    assert "skipped" in result.line()  # type: ignore[attr-defined]
    forced = FakeCDN()
    _run(raw, store, forced, force=True)
    assert forced.requested == first.requested


def test_cid_parts_are_stored_from_the_message(tmp_path: Path) -> None:
    raw = load("generic.eml")
    store = LocalStore(tmp_path)
    _run(raw, store, FakeCDN())
    (cid,) = [a for a in _manifest(store, raw)["assets"] if a["via"] == "cid"]
    assert cid["url"] == "cid:logo@example.com"
    assert cid["key"].endswith(".png")
    body = store.get(cid["key"])
    assert body is not None and body.startswith(b"\x89PNG")


def test_stdout_line_has_no_urls(tmp_path: Path) -> None:
    result = _run(load("substack.eml"), LocalStore(tmp_path), FakeCDN())
    line = result.line()  # type: ignore[attr-defined]
    assert line.startswith("ISSUE ") and "http" not in line


def test_older_manifest_is_redone_without_refetching(tmp_path: Path) -> None:
    raw = load("substack.eml")
    store = LocalStore(tmp_path)
    _run(raw, store, FakeCDN())
    m = _manifest(store, raw)
    big = "https://substackcdn.com/image/fetch/w_1200,c_limit,f_auto,q_auto:good/photo.jpg"
    # what an EXTRACT_VERSION 1 run left: the srcset URL cut at its first comma
    m["extract_version"] = 1
    for a in m["assets"]:
        if a["url"] == big:
            a.update(url="https://substackcdn.com/image/fetch/w_1200", status="failed")
    put_json(store, manifest_key(m["sha256"]), m)

    again = FakeCDN()
    result = _run(raw, store, again)
    assert again.requested == [big]
    assert "reused=" in result.line()  # type: ignore[attr-defined]
    m2 = _manifest(store, raw)
    assert m2["extract_version"] == EXTRACT_VERSION
    assert {a["url"] for a in m2["assets"]} == {a["url"] for a in _manifest_fresh(raw, tmp_path)}
    assert all(a["status"] == "ok" for a in m2["assets"])
    # and now it is done
    third = FakeCDN()
    _run(raw, store, third)
    assert third.requested == []


def _manifest_fresh(raw: bytes, tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    store = LocalStore(tmp_path / "fresh")
    _run(raw, store, FakeCDN())
    return _manifest(store, raw)["assets"]  # type: ignore[no-any-return]


LETTER_CSS = "https://www.example.com/css/letter.css"
FONTS_CSS = "https://fonts.example.com/css?family=Serif"


def _css_cdn(**kw: object) -> FakeCDN:
    return FakeCDN(
        bodies={
            LETTER_CSS: (
                "text/css",
                b"@import 'nested.css'; .hero{background:url(../img/hero.jpg)}"
                b" .x{background:url(data:image/png;base64,AA)}"
                b" .dup{background:url(https://www.example.com/img/banner.jpg)}",
            ),
            "https://www.example.com/css/nested.css": (
                "text/css",
                b"@import url(deeper.css); .n{background:url(n.png)}",
            ),
            "https://www.example.com/css/deeper.css": (
                "text/css; charset=utf-8",
                b"@import 'deepest.css'; .d{background:url(d.png)}",
            ),
            "https://www.example.com/css/deepest.css": ("text/css", b".z{background:url(z.png)}"),
            FONTS_CSS: ("text/css", b"@font-face{src:url(/s/serif-latin.woff2)}"),
        },
        **kw,  # type: ignore[arg-type]
    )


def test_stylesheets_are_followed(tmp_path: Path) -> None:
    raw = load("generic.eml")
    store, cdn = LocalStore(tmp_path), _css_cdn()
    result = _run(raw, store, cdn)
    by_url = {a["url"]: a for a in _manifest(store, raw)["assets"]}
    hero = by_url["https://www.example.com/img/hero.jpg"]
    assert (hero["via"], hero["found_in"], hero["status"]) == ("css", LETTER_CSS, "ok")
    assert by_url["https://www.example.com/css/nested.css"]["via"] == "css-import"
    assert by_url["https://fonts.example.com/s/serif-latin.woff2"]["found_in"] == FONTS_CSS
    for depth in ("n.png", "deeper.css", "d.png", "deepest.css"):
        assert by_url[f"https://www.example.com/css/{depth}"]["status"] == "ok"
    # three stylesheets deep and no further: deepest.css is stored, not read
    assert "https://www.example.com/css/z.png" not in by_url
    # nothing twice, no data: URIs
    assert cdn.requested.count("https://www.example.com/img/banner.jpg") == 1
    assert not any(u.startswith("data:") for u in by_url)
    assert "from_css=" in result.line()  # type: ignore[attr-defined]


def test_stylesheet_assets_have_a_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dea_capture.issue, "MAX_CSS_ASSETS", 2)
    raw = load("generic.eml")
    store = LocalStore(tmp_path)
    _run(raw, store, _css_cdn())
    css = [a for a in _manifest(store, raw)["assets"] if a["found_in"]]
    assert [a["status"] for a in css][:2] == ["ok", "ok"]
    assert {a["status"] for a in css[2:]} == {"deferred:budget"}


def test_stylesheet_reused_from_an_older_manifest_is_still_read(tmp_path: Path) -> None:
    raw = load("generic.eml")
    store = LocalStore(tmp_path)
    _run(raw, store, _css_cdn())
    m = _manifest(store, raw)
    m["extract_version"] = EXTRACT_VERSION - 1
    m["assets"] = [a for a in m["assets"] if not a["found_in"]]
    put_json(store, manifest_key(m["sha256"]), m)
    again = _css_cdn()
    _run(raw, store, again)
    # the stylesheets come from the store; only what they refer to is fetched
    assert LETTER_CSS not in again.requested
    assert "https://www.example.com/img/hero.jpg" in again.requested


def test_status_record_has_counts_and_no_urls(tmp_path: Path) -> None:
    raw = load("generic.eml")
    store = LocalStore(tmp_path)
    _run(raw, store, FakeCDN(missing={"https://www.example.com/img/footer.png"}))
    sha = hashlib.sha256(raw).hexdigest()
    meta = store.metadata(status_key(sha))
    assert meta is not None
    assert meta["state"] == "partial"
    assert meta["assets_failed"] == "1"
    assert meta["source"] == "s3://bucket/incoming/archive/x.eml"
    assert int(meta["links_skip"]) > 0
    assert int(meta["links"]) == sum(
        int(meta[f"links_{d}"]) for d in ("capture", "resolve", "skip")
    )
    assert all(isinstance(v, str) for v in meta.values())
    assert not any("http" in v for k, v in meta.items() if k != "source")
    body = json.loads(store.get(status_key(sha)) or b"")
    assert body == {"sha256": sha, **meta}


def test_skipped_issue_gets_a_missing_status(tmp_path: Path) -> None:
    raw = load("generic.eml")
    store = LocalStore(tmp_path)
    _run(raw, store, FakeCDN())
    sha = hashlib.sha256(raw).hexdigest()
    before = store.metadata(status_key(sha))
    (tmp_path / status_key(sha)).unlink()
    again = FakeCDN()
    result = _run(raw, store, again)
    assert result.skipped and again.requested == []  # type: ignore[attr-defined]
    assert store.metadata(status_key(sha)) == before
    assert store.metadata(status_key(sha))["state"] == "done"  # type: ignore[index]
