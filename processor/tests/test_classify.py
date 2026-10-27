from __future__ import annotations

import base64
import json

import pytest

from dea_capture.classify import (
    Decision,
    LinkClass,
    canonicalize,
    classify,
    decode_redirect,
    tracking_vendor,
)


def _jwt(payload: object) -> str:
    def b(o: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    return f"{b({'alg': 'HS256'})}.{b(payload)}.c2ln"


POST = "https://example.substack.com/p/hello"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.example.org/a?utm_source=x&id=7&mc_cid=1&mc_eid=2&fbclid=z",
            "https://www.example.org/a?id=7",
        ),
        ("HTTPS://WWW.Example.ORG:443/About#team", "https://www.example.org/About"),
        ("http://example.org:80", "http://example.org/"),
        ("https://example.org:8443/x", "https://example.org:8443/x"),
        # `s` is a search query off Substack
        ("https://www.example.org/search?s=archives", "https://www.example.org/search?s=archives"),
        # ...and a share code on it
        (
            "https://example.substack.com/p/x?r=ab&s=r&action=share",
            "https://example.substack.com/p/x",
        ),
        # Substack markers make a custom domain count as Substack
        ("https://news.example.org/p/x?r=ab&isFreemail=true", "https://news.example.org/p/x"),
        ("https://example.org/r?token=t&access_token=a&keep=1", "https://example.org/r?keep=1"),
        (
            "https://open.substack.com/pub/Example/p/hello-world?r=ab&utm_campaign=post",
            "https://example.substack.com/p/hello-world",
        ),
        ("https://example.org/a?b=1&a=2", "https://example.org/a?b=1&a=2"),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("https://user:pw@example.org/p", "https://example.org/p"),
        ("https://mailchi.mp/example/oct?e=bb22", "https://mailchi.mp/example/oct"),
        (
            "https://x.us1.list-manage.com/vcard?u=1&id=2&e=3",
            "https://x.us1.list-manage.com/vcard?u=1&id=2",
        ),
        ("https://www.example.org/x?e=keep", "https://www.example.org/x?e=keep"),
    ],
)
def test_canonicalize(url: str, expected: str) -> None:
    assert canonicalize(url) == expected


def test_decode_substack_v2() -> None:
    url = f"https://substack.com/redirect/2/{_jwt({'e': POST, 'p': 1})}?j=x"
    assert decode_redirect(url) == POST
    two_part = _jwt({"e": POST}).split(".", 1)[1]  # payload.signature, as sent
    assert decode_redirect(f"https://substack.com/redirect/2/{two_part}") == POST


@pytest.mark.parametrize(
    "token",
    [
        "eyJub3QiOiJqc29u.bm90LWpzb24.c2ln",  # payload not JSON
        _jwt({"e": "javascript:alert(1)"}),
        _jwt({"e": 7}),
        _jwt(["e"]),
    ],
)
def test_decode_rejects_bad_payloads(token: str) -> None:
    assert decode_redirect(f"https://substack.com/redirect/2/{token}") is None


def test_decode_only_on_substack() -> None:
    assert decode_redirect(f"https://example.org/redirect/2/{_jwt({'e': POST})}") is None


@pytest.mark.parametrize(
    ("url", "vendor"),
    [
        ("https://substack.com/redirect/abc?j=1", "substack"),
        ("https://x.us1.list-manage.com/track/click?u=1", "mailchimp"),
        ("https://us1.list-manage.com/CvccQjEdTpH?e=1&c2id=2&m=3", "mailchimp"),
        ("https://us1.list-manage.com/d4AELPnCtuj", "mailchimp"),
        ("https://x.us1.list-manage.com/vcard?u=1", None),
        ("https://x.us1.list-manage.com/subscribe?u=1", None),
        ("https://email.mg1.substack.com/c/eJx", "mailgun"),
        ("https://x.activehosted.com/lt.php?x=1", "activecampaign"),
        ("https://x.lt.acemlna.com/Prod/link-tracker?a=1", "activecampaign"),
        ("https://u1.ct.sendgrid.net/ls/click?upn=1", "sendgrid"),
        ("https://click.example.com/abc", "heuristic"),
        ("https://www.example.com/click/abc", None),
        ("https://x.us1.list-manage.com/unsubscribe", None),
    ],
)
def test_tracking_vendor(url: str, vendor: str | None) -> None:
    assert tracking_vendor(url) == vendor


@pytest.mark.parametrize(
    ("href", "text", "cls"),
    [
        ("#top", "Back to top", LinkClass.INTERNAL_ANCHOR),
        ("mailto:a@example.org", "", LinkClass.NON_WEB),
        ("tel:+15555550100", "", LinkClass.NON_WEB),
        ("javascript:alert(1)", "", LinkClass.NON_WEB),
        ("data:text/html,hi", "", LinkClass.NON_WEB),
        ("https:///nohost", "", LinkClass.NON_WEB),
        ("https://x.us1.list-manage.com/unsubscribe?u=1", "", LinkClass.UNSUBSCRIBE),
        ("https://x.us1.list-manage.com/profile?u=1", "", LinkClass.UNSUBSCRIBE),
        ("https://x.substack.com/action/disable_email?token=t", "", LinkClass.UNSUBSCRIBE),
        ("https://x.activehosted.com/proc.php?s=1&act=unsub", "Click here", LinkClass.UNSUBSCRIBE),
        ("https://example.org/email-preferences", "", LinkClass.UNSUBSCRIBE),
        ("https://example.org/opt-out?id=1", "", LinkClass.UNSUBSCRIBE),
        ("https://x.substack.com/account?utm_source=email", "", LinkClass.UNSUBSCRIBE),
        ("https://example.org/settings/email", "", LinkClass.UNSUBSCRIBE),
        ("https://x.activehosted.com/proc.php?s=1&act=prefs", "", LinkClass.UNSUBSCRIBE),
        ("https://example.org/x", "Unsubscribe", LinkClass.UNSUBSCRIBE),
        ("https://example.org/x", "Manage your email preferences", LinkClass.UNSUBSCRIBE),
        ("https://example.org/x", "update your preferences", LinkClass.UNSUBSCRIBE),
        ("https://example.org/x", "Opt out", LinkClass.UNSUBSCRIBE),
        # anchor text beats an opaque tracker: we can't see where it goes
        ("https://substack.com/redirect/abc?j=1", "Unsubscribe", LinkClass.UNSUBSCRIBE),
        ("https://x.list-manage.com/subscribe/confirm?u=1", "", LinkClass.ACTION),
        ("https://example.org/events/4/rsvp", "", LinkClass.ACTION),
        ("https://example.org/poll?answer=yes", "", LinkClass.ACTION),
        ("https://example.org/verify?code=1", "", LinkClass.ACTION),
        ("https://example.org/do?action=confirm", "", LinkClass.ACTION),
        ("https://example.org/x", "Confirm", LinkClass.ACTION),
        ("https://example.org/x", "RSVP now", LinkClass.ACTION),
        # a long anchor that merely mentions a verb is content
        (
            "https://example.org/news/1",
            "Officials confirm the new schedule after weeks of debate",
            LinkClass.CONTENT,
        ),
        ("https://example.org/x", "Like", LinkClass.CONTENT),
        ("https://example.org/a?token=t", "", LinkClass.LOGIN_TOKEN),
        ("https://substack.com/app-link/post?post_id=1&submitLike=true&token=t", "Like",
         LinkClass.LOGIN_TOKEN),
        ("https://substack.com/redirect/abc?j=1", "Read more", LinkClass.TRACKING_REDIRECT),
        ("https://www.example.org/news", "News", LinkClass.CONTENT),
        ("https://example.substack.com/p/x?action=share", "Share", LinkClass.CONTENT),
    ],
)  # fmt: skip
def test_classes(href: str, text: str, cls: LinkClass) -> None:
    assert classify(href, text).link_class == cls


def test_skip_has_no_fetch_url() -> None:
    c = classify("https://example.org/unsubscribe?e=1")
    assert (c.decision, c.fetch_url, c.canonical) == (Decision.SKIP, None, None)


def test_list_unsubscribe_header_wins() -> None:
    lu = "https://example.org/l/abc123"
    assert classify(lu, "Read more", [lu]).link_class == LinkClass.UNSUBSCRIBE
    assert classify(lu, "Read more").link_class == LinkClass.CONTENT


def test_login_token_captures_canonical_without_token() -> None:
    c = classify("https://substack.com/app-link/post?publication_id=1&post_id=2&token=t&r=x")
    assert c.decision == Decision.CAPTURE
    assert (
        c.fetch_url
        == c.canonical
        == "https://substack.com/app-link/post?publication_id=1&post_id=2"
    )


def test_decoded_redirect_resolves_and_dedupes() -> None:
    href = f"https://substack.com/redirect/2/{_jwt({'e': POST + '?utm_source=substack'})}?j=1"
    c = classify(href, "Hello")
    assert c.link_class == LinkClass.TRACKING_REDIRECT
    assert c.decision == Decision.RESOLVE
    assert c.fetch_url == href  # the click itself, as a reader would make it
    assert c.decoded == POST + "?utm_source=substack"
    assert c.canonical == POST


def test_decoded_redirect_to_unsubscribe_is_skipped() -> None:
    target = "https://example.substack.com/action/disable_email?token=t"
    c = classify(f"https://substack.com/redirect/2/{_jwt({'e': target})}", "Stop these")
    assert c.link_class == LinkClass.UNSUBSCRIBE
    assert c.reason.startswith("redirect target")
    assert c.decision == Decision.SKIP


def test_decoded_redirect_to_login_link_skips_the_click() -> None:
    target = POST + "?token=t&r=x"
    c = classify(f"https://substack.com/redirect/2/{_jwt({'e': target})}", "Continue")
    assert c.link_class == LinkClass.LOGIN_TOKEN
    assert c.fetch_url == POST


def test_opaque_redirect_has_no_canonical() -> None:
    c = classify("https://x.us1.list-manage.com/track/click?u=1&id=2&e=3", "Report")
    assert c.decision == Decision.RESOLVE
    assert c.canonical is None
    assert c.decoded is None


def test_unsubscribe_like_slug_errs_towards_skip() -> None:
    """Known false positive, accepted: a skipped link can be captured by hand."""
    c = classify("https://www.example.com/unsubscribe-from-the-news-cycle", "A post")
    assert c.link_class == LinkClass.UNSUBSCRIBE
