from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dea_capture.fetch import HostPacer, TooLarge
from dea_capture.guard import GuardedFetcher, IPAddress, Response
from dea_capture.inventory import Inventory, inventory
from dea_capture.issue import manifest_key, process_issue
from dea_capture.store import LocalStore

from .conftest import EML_FIXTURES, FORBIDDEN_MARKERS, fake_resolver, load


@dataclass
class FakeCDN:
    """Serves a small PNG for everything, with a few scripted exceptions."""

    missing: set[str] = field(default_factory=set)
    huge: set[str] = field(default_factory=set)
    redirects: dict[str, str] = field(default_factory=dict)
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
