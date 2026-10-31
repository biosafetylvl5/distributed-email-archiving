"""The safety net: whatever the classifier decides, nothing that could act as
the subscriber or reach the local network gets requested."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from dea_capture.capture import index_key
from dea_capture.classify import Decision, LinkClass, classify
from dea_capture.extract import extract_links, parse
from dea_capture.fetch import HostPacer
from dea_capture.guard import ForbiddenFetch, GuardedFetcher, Response, check_link
from dea_capture.inventory import Inventory, inventory
from dea_capture.links import process_links
from dea_capture.resolve import _to_browser, resolve
from dea_capture.store import LocalStore

from .conftest import EML_FIXTURES, FORBIDDEN_MARKERS, RecordingFetcher, fake_resolver
from .test_fetch import Scripted


def _guarded(recording: RecordingFetcher, refused: frozenset[str]) -> GuardedFetcher:
    return GuardedFetcher(recording, resolve=fake_resolver, refused_urls=refused)


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_dry_run_requests_nothing_forbidden(path: Path, recording: RecordingFetcher) -> None:
    inv = inventory(path.read_bytes())
    fetcher = _guarded(recording, inv.refused_urls())
    for embed in inv.embeds:
        fetcher.get(embed.url, "embed")
    refused = []
    for link in inv.to_fetch():
        assert link.fetch_url is not None
        try:
            fetcher.get(link.fetch_url, "link")
        except ForbiddenFetch:
            refused.append(link.fetch_url)
    assert recording.requested, "fixture should fetch something"
    # only network-level refusals are expected here; the classifier already
    # skipped everything that acts as the subscriber
    for url in refused:
        assert not any(m in url for m in FORBIDDEN_MARKERS)


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_guard_refuses_every_marked_href_directly(path: Path, recording: RecordingFetcher) -> None:
    """Even if a caller ignored the classifier and asked for the raw href."""
    msg = parse(path.read_bytes())
    fetcher = _guarded(recording, inventory(path.read_bytes()).refused_urls())
    marked = [
        link.href for link in extract_links(msg) if any(m in link.href for m in FORBIDDEN_MARKERS)
    ]
    marked += [u for u in msg.list_unsubscribe]
    assert marked
    for href in marked:
        with pytest.raises(ForbiddenFetch):
            fetcher.get(href, "link")
        with pytest.raises(ForbiddenFetch):
            fetcher.get(href, "redirect-hop")
    assert recording.requested == []


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_marked_links_are_skipped_or_stripped(path: Path) -> None:
    for link in inventory(path.read_bytes()).links:
        if not any(m in link.href for m in FORBIDDEN_MARKERS):
            continue
        if link.decision == Decision.SKIP:
            assert link.fetch_url is None
            continue
        # login links are captured logged out, never with their token
        assert link.link_class == LinkClass.LOGIN_TOKEN, link
        assert link.fetch_url is not None
        assert not any(m in link.fetch_url for m in FORBIDDEN_MARKERS), link


@pytest.mark.parametrize("canonical_conflict", [False, True])
def test_refused_href_is_never_queued(
    tmp_path: Path, recording: RecordingFetcher, canonical_conflict: bool
) -> None:
    refused = "https://example.org/article"
    content = refused + "?utm_source=news" if canonical_conflict else refused
    inv = Inventory([], [], [classify(content, "Read"), classify(refused, "Unsubscribe")], ())
    store = LocalStore(tmp_path)
    doc = process_links(
        "a" * 64, inv, store, _guarded(recording, inv.refused_urls()), pacer=HostPacer(0)
    )
    assert doc["links"] == []
    assert not list(tmp_path.glob("captures/**/*"))
    assert recording.requested == []


@pytest.mark.parametrize(
    "landing",
    [
        "https://example.org/post?return=https%3A%2F%2Fexample.org%2FSECRET_reader%40example.com",
        "https://EXAMPLE.SUBSTACK.COM./p/post?token=SECRET&r=SECRET",
        "https://example.org/news/SECRET_reader%40example.com",
    ],
)
def test_resolved_mailboxes_never_reach_shared_capture_records(
    tmp_path: Path, landing: str
) -> None:
    href = "https://click.example.org/story"
    inner = Scripted({href: Response(href, 302, {"location": landing}, b"")})
    inv = Inventory([], [], [classify(href)], ())
    store = LocalStore(tmp_path)
    doc = process_links(
        "a" * 64, inv, store, GuardedFetcher(inner, resolve=fake_resolver), pacer=HostPacer(0)
    )
    assert inner.requested == [href]
    rec = doc["links"][0]
    # Tokenized hrefs/chains remain issue-private; every shared record is checked in full.
    assert rec["chain"] == [landing]
    safe_fields = {k: rec[k] for k in ("canonical", "h", "error", "kind", "queued")}
    assert "SECRET" not in json.dumps(safe_fields)
    assert "reader" not in json.dumps(safe_fields)
    if rec["h"]:
        shared = store.get(index_key(rec["h"]))
        assert shared is not None
        assert b"SECRET" not in shared and b"reader" not in shared
    else:
        assert rec["outcome"] == "refused"


def test_trailing_dot_trackers_are_not_browser_destinations() -> None:
    href = "https://click.example.org/story"
    tracker = "https://SUBSTACK.COM./redirect/opaque?j=SECRET"
    assert _to_browser([href, tracker], tracker, "timeout") is None
    inner = Scripted(
        {
            href: Response(href, 302, {"location": tracker}, b""),
            tracker: Response(tracker, 200, {}, b""),
        }
    )
    got = resolve(href, GuardedFetcher(inner, resolve=fake_resolver), HostPacer(0))
    assert got.outcome == "failed"
    assert got.canonical is None


def test_classification_safe_fields_exclude_private_mailboxes() -> None:
    href = "https://example.org/post?token=SECRET&return=SECRET_reader%40example.com"
    c = classify(href)
    safe = {k: v for k, v in asdict(c).items() if k not in {"href", "text", "decoded"}}
    assert "SECRET" not in json.dumps(safe)
    assert "reader" not in json.dumps(safe)
    assert c.href == href  # the original belongs only in the private inventory


def test_every_fixture_has_skips_and_fetches() -> None:
    for path in EML_FIXTURES:
        decisions = {c.decision for c in inventory(path.read_bytes()).links}
        assert Decision.SKIP in decisions, path.name
        assert decisions & {Decision.CAPTURE, Decision.RESOLVE}, path.name


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_url_rules_alone_catch_marked_hrefs(path: Path) -> None:
    """Without the per-message refusal list, as for a redirect hop that lands
    on one of these: the URL rules on their own must still refuse them."""
    msg = parse(path.read_bytes())
    for link in extract_links(msg):
        if any(m in link.href for m in FORBIDDEN_MARKERS):
            with pytest.raises(ForbiddenFetch):
                check_link(link.href, msg.list_unsubscribe)
