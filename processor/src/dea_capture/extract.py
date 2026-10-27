"""Pull bodies, inline parts, embeds and links out of a raw ``.eml``.

Pure and offline: no network access, and nothing here decides what may be
fetched. That is ``classify`` and ``guard``.
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

CSS_URL_RE = re.compile(r"url\(\s*['\"]?(https?://[^'\")\s]+)['\"]?\s*\)", re.IGNORECASE)
# bare URLs in text/plain; stops at whitespace and common closing punctuation
TEXT_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?"

# open-tracking pixels that don't declare 1x1 dimensions
_PIXEL_HOSTS = ("eotrx.substackcdn.com",)
_PIXEL_PATHS = re.compile(r"/track/open|/open\.php|/wf/open|/e/o/|^/o/|/lto?\.php", re.IGNORECASE)
_TINY = re.compile(r"^\s*[01](px)?\s*$")
_STYLE_TINY = re.compile(r"(?:^|;)\s*(?:width|height)\s*:\s*[01]px", re.IGNORECASE)


@dataclass(frozen=True)
class CidPart:
    cid: str
    content_type: str
    data: bytes
    filename: str | None


@dataclass(frozen=True)
class Embed:
    url: str
    via: str  # img | source | background | style | stylesheet
    pixel: bool = False


@dataclass(frozen=True)
class Link:
    href: str
    text: str
    via: str  # html | text
    position: int


@dataclass
class Message:
    message: EmailMessage
    html: str | None
    text: str | None
    cid_parts: dict[str, CidPart] = field(default_factory=dict)
    list_unsubscribe: tuple[str, ...] = ()


def _body(msg: EmailMessage, kind: str) -> str | None:
    part = msg.get_body(preferencelist=(kind,))
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else None


def parse_list_unsubscribe(value: str | None) -> tuple[str, ...]:
    """RFC 2369: a comma-separated list of ``<uri>``. Mailto and http both kept."""
    if not value:
        return ()
    return tuple(m.strip() for m in re.findall(r"<([^>]+)>", value))


def parse(raw: bytes) -> Message:
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    cid_parts: dict[str, CidPart] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        cid = part.get("Content-ID")
        if not cid:
            continue
        cid = str(cid).strip().strip("<>")
        payload = part.get_payload(decode=True)
        cid_parts[cid] = CidPart(
            cid=cid,
            content_type=part.get_content_type(),
            data=payload if isinstance(payload, bytes) else b"",
            filename=part.get_filename(),
        )
    lu = msg.get("List-Unsubscribe")
    return Message(
        message=msg,
        html=_body(msg, "html"),
        text=_body(msg, "plain"),
        cid_parts=cid_parts,
        list_unsubscribe=parse_list_unsubscribe(str(lu) if lu is not None else None),
    )


def _is_http(url: str | None) -> bool:
    return bool(url) and str(url).strip().lower().startswith(("http://", "https://"))


def _attr(el: Tag, name: str) -> str:
    value = el.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def is_tracking_pixel(url: str, el: Tag | None = None) -> bool:
    if el is not None:
        w, h = _attr(el, "width"), _attr(el, "height")
        if w and h and _TINY.match(w) and _TINY.match(h):
            return True
        if len(_STYLE_TINY.findall(_attr(el, "style"))) >= 2:
            return True
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in _PIXEL_HOSTS:
        return True
    return bool(_PIXEL_PATHS.search(parts.path))


def extract_embeds(html: str | None) -> list[Embed]:
    """Everything the HTML loads to render, in document order, deduplicated.

    Tracking pixels are included and flagged: they are part of what a reader's
    mail client loads.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found: list[Embed] = []

    def add(url: str | None, via: str, el: Tag | None = None) -> None:
        if url and _is_http(url):
            url = url.strip()
            found.append(Embed(url, via, is_tracking_pixel(url, el if via == "img" else None)))

    for el in soup.find_all(["img", "source"]):
        assert isinstance(el, Tag)
        add(_attr(el, "src"), el.name, el)
        for candidate in _attr(el, "srcset").split(","):
            bits = candidate.strip().split()
            if bits:
                add(bits[0], el.name)
    for el in soup.find_all(background=True):
        assert isinstance(el, Tag)
        add(_attr(el, "background"), "background")
    for el in soup.find_all(style=True):
        assert isinstance(el, Tag)
        for m in CSS_URL_RE.finditer(_attr(el, "style")):
            add(m.group(1), "style")
    for el in soup.find_all("style"):
        for m in CSS_URL_RE.finditer(el.get_text() or ""):
            add(m.group(1), "style")
    for el in soup.find_all("link"):
        assert isinstance(el, Tag)
        if "stylesheet" in [r.lower() for r in el.get_attribute_list("rel") if r]:
            add(_attr(el, "href"), "stylesheet")

    seen: set[str] = set()
    out: list[Embed] = []
    for e in found:
        if e.url not in seen:
            seen.add(e.url)
            out.append(e)
    return out


def _anchor_text(el: Tag) -> str:
    text = " ".join(el.get_text(" ").split())
    if text:
        return text
    for img in el.find_all("img"):
        assert isinstance(img, Tag)
        alt = " ".join(_attr(img, "alt").split())
        if alt:
            return f"[image: {alt}]"
    return _attr(el, "title") or _attr(el, "aria-label")


def extract_links(msg: Message) -> list[Link]:
    """Every link a reader could follow: ``<a>``/``<area>`` hrefs in the HTML
    part, plus bare URLs in the text part that the HTML doesn't already have.

    Every href is kept, whatever its scheme, so the inventory is complete.
    """
    links: list[Link] = []
    seen: set[str] = set()
    if msg.html:
        soup = BeautifulSoup(msg.html, "html.parser")
        for el in soup.find_all(["a", "area"]):
            assert isinstance(el, Tag)
            href = _attr(el, "href").strip()
            if not href:
                continue
            links.append(Link(href, _anchor_text(el), "html", len(links)))
            seen.add(href)
    if msg.text:
        for m in TEXT_URL_RE.finditer(msg.text):
            url = m.group(0).rstrip(_TRAILING_PUNCT)
            if url not in seen:
                seen.add(url)
                links.append(Link(url, "", "text", len(links)))
    return links
