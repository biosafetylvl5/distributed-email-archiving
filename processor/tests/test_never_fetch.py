"""The safety net: whatever the classifier decides, nothing that could act as
the subscriber or reach the local network gets requested."""

from __future__ import annotations

from pathlib import Path

import pytest

from dea_capture.classify import Decision, LinkClass
from dea_capture.extract import extract_links, parse
from dea_capture.guard import ForbiddenFetch, GuardedFetcher, check_link
from dea_capture.inventory import inventory

from .conftest import EML_FIXTURES, FORBIDDEN_MARKERS, RecordingFetcher, fake_resolver


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
