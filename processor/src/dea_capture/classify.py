"""Classify links, unwrap known redirects offline, and canonicalize URLs.

Every link gets exactly one class and one decision:

- ``capture``: fetch ``fetch_url`` (the canonical URL) and archive it.
- ``resolve``: request the original href and follow its redirect chain, as a
  reader's click would. Each hop is checked again by ``guard`` before it is
  requested.
- ``skip``: never request it. ``reason`` says why.

Rules are ordered so that anything that could change the subscription, act
publicly, or log in is caught before anything that would fetch. When in doubt
a rule errs towards ``skip``: a skipped link is recorded and can be captured by
hand, a fetched unsubscribe link can't be undone.

Pure and offline.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


class LinkClass(StrEnum):
    CONTENT = "content"
    TRACKING_REDIRECT = "tracking-redirect"
    LOGIN_TOKEN = "login-token"
    ACTION = "action"
    UNSUBSCRIBE = "unsubscribe"
    NON_WEB = "non-web"
    INTERNAL_ANCHOR = "internal-anchor"


class Decision(StrEnum):
    CAPTURE = "capture"
    RESOLVE = "resolve"
    SKIP = "skip"


NEVER_FETCH = frozenset(
    {LinkClass.ACTION, LinkClass.UNSUBSCRIBE, LinkClass.NON_WEB, LinkClass.INTERNAL_ANCHOR}
)


@dataclass(frozen=True)
class Classified:
    href: str
    text: str
    link_class: LinkClass
    decision: Decision
    reason: str
    decoded: str | None  # redirect target recovered offline, if any
    canonical: str | None  # dedupe key; None for non-web links and opaque redirects
    fetch_url: str | None  # the first URL that may be requested; None when skipped


# ── parameters ──────────────────────────────────────────────────────────────

# identify the subscriber or log them in; a link carrying one is never
# requested as-is
AUTH_PARAMS = frozenset(
    {"token", "access_token", "auth_token", "login_token", "magic_token", "magic_link", "otp"}
)
# tracking and UI-state parameters, stripped everywhere
TRACKING_PARAMS = frozenset(
    {
        "mc_cid", "mc_eid", "fbclid", "gclid", "dclid", "msclkid", "_hsenc", "_hsmi",
        "mkt_tok", "ck_subscriber_id", "vero_id", "oly_enc_id", "oly_anon_id",
        "isfreemail", "submitlike", "triggershare", "showwelcomeonshare",
    }
)  # fmt: skip
# Substack's short names mean something else elsewhere (`s` is often search),
# so they are stripped only on Substack URLs
SUBSTACK_PARAMS = frozenset({"r", "s", "action", "comments", "play_audio"})
_SUBSTACK_MARKERS = frozenset({"isfreemail", "submitlike", "triggershare", "showwelcomeonshare"})
# Mailchimp's subscriber id; only personalises the hosted pages it's stripped from
MAILCHIMP_PARAMS = frozenset({"e"})
_MAILCHIMP_HOSTS = re.compile(r"(^|\.)(list-manage\.com|mailchi\.mp|forward-to-friend\.com)$")


def _is_substack(host: str, keys: Iterable[str]) -> bool:
    return (
        host == "substack.com"
        or host.endswith(".substack.com")
        or bool(_SUBSTACK_MARKERS & set(keys))
    )


def _strip(key: str, substack: bool, mailchimp: bool) -> bool:
    k = key.lower()
    return (
        k.startswith("utm_")
        or k in TRACKING_PARAMS
        or k in AUTH_PARAMS
        or (substack and k in SUBSTACK_PARAMS)
        or (mailchimp and k in MAILCHIMP_PARAMS)
    )


def query_keys(url: str) -> set[str]:
    return {k.lower() for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}


def has_auth_param(url: str) -> bool:
    return bool(AUTH_PARAMS & query_keys(url))


# ── canonicalization ────────────────────────────────────────────────────────

_OPEN_SUBSTACK = re.compile(r"^/pub/([a-z0-9-]+)/p/([^/]+)/?$", re.IGNORECASE)


def canonicalize(url: str) -> str:
    """The dedupe key for a web URL: tracking and auth parameters removed,
    scheme and host lowercased, default port, fragment and any credentials
    dropped, and known publisher aliases rewritten. Parameter order is
    otherwise preserved."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    bracketed = f"[{host}]" if ":" in host else host
    netloc = (
        bracketed
        if port is None or (scheme, port) in {("http", 80), ("https", 443)}
        else (f"{bracketed}:{port}")
    )
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    substack = _is_substack(host, (k.lower() for k, _ in pairs))
    mailchimp = bool(_MAILCHIMP_HOSTS.search(host))
    kept = [(k, v) for k, v in pairs if not _strip(k, substack, mailchimp)]
    path = parts.path or "/"
    if host == "open.substack.com" and (m := _OPEN_SUBSTACK.match(path)):
        netloc, path = f"{m.group(1).lower()}.substack.com", f"/p/{m.group(2)}"
    return urlunsplit((scheme, netloc, path, urlencode(kept, doseq=True), ""))


# ── redirect unwrapping ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Redirect:
    vendor: str
    host: re.Pattern[str]
    path: re.Pattern[str]


# click-tracking wrappers: requesting one registers a click for the subscriber
_REDIRECTS = (
    _Redirect("substack", re.compile(r"^(.+\.)?substack\.com$"), re.compile(r"^/redirect/")),
    _Redirect("mailchimp", re.compile(r"(^|\.)list-manage\.com$"), re.compile(r"^/track/click")),
    # newer short links, us<n>.list-manage.com/<code>?c2id=…; the code always has
    # a capital or digit, which keeps /vcard, /subscribe and friends out
    _Redirect(
        "mailchimp", re.compile(r"(^|\.)list-manage\.com$"),
        re.compile(r"^/(?=[^/]*[A-Z0-9])[A-Za-z0-9]{6,}/?$"),
    ),
    _Redirect("mailgun", re.compile(r"^email\.[a-z0-9.-]+$"), re.compile(r"^/c/")),
    _Redirect("activecampaign", re.compile(r".*"), re.compile(r"/lt\.php$|^/Prod/link-tracker")),
    _Redirect("sendgrid", re.compile(r"(^|\.)ct\.sendgrid\.net$"), re.compile(r"^/ls/click")),
    _Redirect(
        "heuristic", re.compile(r"^(click|clicks|trk|track|tracking)\.[a-z0-9.-]+$"),
        re.compile(r"."),
    ),
)  # fmt: skip

# payload.signature in practice; header.payload.signature also accepted
_SUBSTACK_V2 = re.compile(r"^/redirect/2/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){1,2})(?:/|$)")


def _b64url_json(segment: str) -> object:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def tracking_vendor(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for r in _REDIRECTS:
        if r.host.search(host) and r.path.search(parts.path):
            return r.vendor
    return None


def decode_redirect(url: str) -> str | None:
    """The target of a redirect that carries it in the URL, or None.

    Substack's ``/redirect/2/<token>`` keeps the target in the token
    payload's ``e`` claim. The signature isn't checked: the result is only used to classify
    and deduplicate, never as proof of where the redirect goes.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "substack.com" or host.endswith(".substack.com"):
        m = _SUBSTACK_V2.match(parts.path)
        if m:
            segments = m.group(1).split(".")
            try:
                payload = _b64url_json(segments[1] if len(segments) == 3 else segments[0])
            except (ValueError, binascii.Error, UnicodeDecodeError):
                return None
            target = payload.get("e") if isinstance(payload, dict) else None
            if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
                return target
    return None


# ── state-changing links ────────────────────────────────────────────────────

_UNSUB_URL = re.compile(
    r"unsub|opt[-_]?out|disable[-_]?email|email[-_]?pref|manage[-_]?(your[-_]?)?subscription"
    r"|subscription[-_]?(center|centre|pref|settings)|/preferences?\b|update[-_]?profile"
    r"|list-manage\.com/profile|[?&](act|action)=(unsub|pref)|/(account|settings)(/|$|\?)",
    re.IGNORECASE,
)
_UNSUB_TEXT = re.compile(
    r"\b(unsubscribe|unsub|opt[- ]?out"
    r"|(manage|update|change|edit)( your)?( email)? (subscriptions?|preferences|settings)"
    r"|email (preferences|settings)|subscription (preferences|settings|cent(er|re))"
    r"|stop (receiving|getting) (these )?emails)\b",
    re.IGNORECASE,
)
_ACTION_SEGMENTS = frozenset(
    {
        "rsvp", "vote", "votes", "poll", "polls", "confirm", "confirmation", "verify",
        "verification", "activate", "activation", "optin", "opt-in", "double-opt-in",
    }
)  # fmt: skip
_ACTION_PARAMS = frozenset({"rsvp", "vote", "answer", "rating", "confirm", "verify"})
_ACTION_VALUES = re.compile(
    r"^(confirm|verify|vote|like|rsvp|subscribe|delete|activate)", re.IGNORECASE
)
_ACTION_TEXT = re.compile(r"\b(confirm|verify|rsvp|vote)\b", re.IGNORECASE)
_SHORT_TEXT_WORDS = 6


def _unsubscribe_reason(url: str) -> str | None:
    if _UNSUB_URL.search(unquote(url)):
        return "url looks like unsubscribe or preferences"
    return None


def _action_reason(url: str) -> str | None:
    parts = urlsplit(url)
    segments = {s.lower() for s in unquote(parts.path).split("/") if s}
    if hit := sorted(segments & _ACTION_SEGMENTS):
        return f"path segment '{hit[0]}'"
    host = (parts.hostname or "").lower()
    pairs = [(k.lower(), v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    if hit := sorted({k for k, _ in pairs} & _ACTION_PARAMS):
        return f"parameter '{hit[0]}'"
    substack = _is_substack(host, (k for k, _ in pairs))
    for k, v in pairs:
        # Substack's action=share etc. is UI state, stripped by canonicalize
        if k in {"act", "action"} and not substack and _ACTION_VALUES.match(v):
            return f"parameter {k}={v}"
    return None


def _state_change(url: str, text: str) -> tuple[LinkClass, str] | None:
    if reason := _unsubscribe_reason(url):
        return LinkClass.UNSUBSCRIBE, reason
    if reason := _action_reason(url):
        return LinkClass.ACTION, reason
    if text and _UNSUB_TEXT.search(text):
        return LinkClass.UNSUBSCRIBE, "anchor text"
    if text and len(text.split()) <= _SHORT_TEXT_WORDS and _ACTION_TEXT.search(text):
        return LinkClass.ACTION, "anchor text"
    return None


# ── classification ──────────────────────────────────────────────────────────


def _skip(
    href: str, text: str, cls: LinkClass, reason: str, decoded: str | None = None
) -> Classified:
    return Classified(href, text, cls, Decision.SKIP, reason, decoded, None, None)


def classify(href: str, text: str = "", unsubscribe_urls: Iterable[str] = ()) -> Classified:
    """Classify one link. ``unsubscribe_urls`` are the message's
    ``List-Unsubscribe`` URIs, which are never fetched whatever they look like."""
    href = href.strip()
    text = " ".join(text.split())
    if href.startswith("#"):
        return _skip(href, text, LinkClass.INTERNAL_ANCHOR, "fragment within the message")
    scheme = urlsplit(href).scheme.lower()
    if scheme not in {"http", "https"}:
        return _skip(href, text, LinkClass.NON_WEB, f"scheme '{scheme or 'none'}'")
    if not urlsplit(href).hostname:
        return _skip(href, text, LinkClass.NON_WEB, "no host")
    if href in set(unsubscribe_urls):
        return _skip(href, text, LinkClass.UNSUBSCRIBE, "List-Unsubscribe header")

    decoded = decode_redirect(href)
    if hit := _state_change(href, text):
        return _skip(href, text, hit[0], hit[1], decoded)
    if decoded and (hit := _state_change(decoded, "")):
        return _skip(href, text, hit[0], f"redirect target: {hit[1]}", decoded)

    vendor = tracking_vendor(href)
    if vendor:
        if decoded and has_auth_param(decoded):
            # the click would land logged in; go straight to the logged-out page
            canonical = canonicalize(decoded)
            return Classified(
                href, text, LinkClass.LOGIN_TOKEN, Decision.CAPTURE,
                f"{vendor} redirect to a login-token URL", decoded, canonical, canonical,
            )  # fmt: skip
        reason = f"{vendor} redirect" + (", target decoded" if decoded else ", opaque")
        target = canonicalize(decoded) if decoded else None
        return Classified(
            href, text, LinkClass.TRACKING_REDIRECT, Decision.RESOLVE, reason, decoded,
            target, href,
        )  # fmt: skip

    canonical = canonicalize(href)
    if has_auth_param(href):
        return Classified(
            href, text, LinkClass.LOGIN_TOKEN, Decision.CAPTURE,
            "auth parameter stripped", None, canonical, canonical,
        )  # fmt: skip
    return Classified(href, text, LinkClass.CONTENT, Decision.CAPTURE, "", None, canonical,
                      canonical)  # fmt: skip
