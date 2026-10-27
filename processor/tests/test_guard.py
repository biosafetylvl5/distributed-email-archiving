from __future__ import annotations

import ipaddress

import pytest

from dea_capture.guard import (
    ForbiddenFetch,
    GuardedFetcher,
    check_destination,
    check_link,
    is_public,
)

from .conftest import PUBLIC_IP, RecordingFetcher, fake_resolver


def resolver_for(*addrs: str):  # type: ignore[no-untyped-def]
    return lambda host, port: list(addrs)


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.1", "172.16.5.4", "192.168.1.1", "127.0.0.1", "0.0.0.0", "169.254.169.254",
        "100.64.0.1", "198.18.0.1", "224.0.0.1", "255.255.255.255", "::1", "::",
        "fd00::1", "fe80::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
        "2002:7f00:1::", "64:ff9b::a00:1",
    ],
)  # fmt: skip
def test_non_public_addresses(addr: str) -> None:
    assert not is_public(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", [PUBLIC_IP, "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(addr: str) -> None:
    assert is_public(ipaddress.ip_address(addr))


def test_extra_blocked_ranges() -> None:
    cluster = [ipaddress.ip_network("93.184.0.0/16")]
    assert not is_public(ipaddress.ip_address(PUBLIC_IP), cluster)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://intranet.example.com/admin",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "https://user:pw@www.example.com/",
        "https:///nohost",
        "https://example.com:99999/",
    ],
)
def test_check_destination_refuses(url: str) -> None:
    with pytest.raises(ForbiddenFetch):
        check_destination(url, fake_resolver)


def test_any_private_answer_refuses() -> None:
    """DNS answering with one public and one private address is refused."""
    with pytest.raises(ForbiddenFetch, match="non-public"):
        check_destination("https://mixed.example.com/", resolver_for(PUBLIC_IP, "10.0.0.7"))


def test_names_are_resolved_not_parsed() -> None:
    """Decimal and other odd host spellings are caught by what they resolve to."""
    with pytest.raises(ForbiddenFetch):
        check_destination("http://2130706433/", resolver_for("127.0.0.1"))


def test_resolution_failure_refuses() -> None:
    def broken(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(ForbiddenFetch, match="cannot resolve"):
        check_destination("https://gone.example.com/", broken)


def test_returns_checked_addresses_for_pinning() -> None:
    addrs = check_destination("https://www.example.com/", fake_resolver)
    assert addrs == [ipaddress.ip_address(PUBLIC_IP)]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/unsubscribe?e=1",
        "https://example.org/events/1/rsvp",
        "https://example.org/a?token=t",
        "https://substack.com/app-link/post?post_id=1&token=t",
        "mailto:a@example.org",
        "#top",
    ],
)
def test_check_link_refuses(url: str) -> None:
    with pytest.raises(ForbiddenFetch):
        check_link(url)


def test_check_link_allows_content_and_trackers() -> None:
    check_link("https://www.example.org/news")
    check_link("https://x.us1.list-manage.com/track/click?u=1&id=2&e=3")


def test_embeds_skip_link_rules_but_not_network_rules(recording: RecordingFetcher) -> None:
    f = GuardedFetcher(recording, resolve=fake_resolver)
    pixel = "https://eotrx.substackcdn.com/open?token=pixel"
    f.get(pixel, "embed")  # a mail client loads it; so does the archive
    with pytest.raises(ForbiddenFetch):
        f.get(pixel, "link")
    with pytest.raises(ForbiddenFetch):
        f.get("http://intranet.example.com/logo.png", "embed")
    assert recording.requested == [pixel]


def test_unsubscribe_urls_are_refused_as_hops(recording: RecordingFetcher) -> None:
    lu = "https://example.org/l/abc"
    f = GuardedFetcher(recording, resolve=fake_resolver, refused_urls=frozenset({lu}))
    with pytest.raises(ForbiddenFetch):
        f.get(lu, "redirect-hop")
