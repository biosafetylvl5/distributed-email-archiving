"""Pull bodies, inline parts, embeds and links out of a raw ``.eml``.

Pure and offline: no network access, and nothing here decides what may be
fetched. That is ``classify`` and ``guard``.
"""

from __future__ import annotations

import email
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# @import "x.css"; @import 'x.css'; @import url(x.css) -- the url() form is
# matched first so its URL is reported once, as an import
_CSS_IMPORT = re.compile(
    r"""@import\s+(?:url\(\s*(?P<q1>['"]?)(?P<u1>[^'")]*?)(?P=q1)\s*\)|(?P<q2>['"])(?P<u2>.*?)(?P=q2))""",
    re.IGNORECASE | re.DOTALL,
)
_CSS_URL = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<u>[^'")]*?)(?P=q)\s*\)""", re.IGNORECASE)
_ICON_RELS = frozenset({"icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed",
                        "mask-icon"})  # fmt: skip
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
    # img | source | background | style | stylesheet | poster | input | icon;
    # the issue stage adds css (an asset a stylesheet refers to)
    via: str
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


def parse_srcset(value: str) -> list[str]:
    """The URLs in a ``srcset``, as the HTML spec parses it.

    A URL runs to the next whitespace, so commas inside it are kept: image
    CDNs put them in paths (``/image/fetch/w_424,c_limit,f_auto/...``). Only
    commas at the end of a URL, or after its descriptors, separate candidates.
    """
    urls: list[str] = []
    pos, n = 0, len(value)
    while pos < n:
        while pos < n and (value[pos].isspace() or value[pos] == ","):
            pos += 1
        start = pos
        while pos < n and not value[pos].isspace():
            pos += 1
        url = value[start:pos]
        if not url:
            break
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            # skip the descriptors, up to the comma that ends this candidate
            depth = 0
            while pos < n and not (value[pos] == "," and depth == 0):
                depth += {"(": 1, ")": -1}.get(value[pos], 0)
                pos += 1
        if url:
            urls.append(url)
    return urls


def css_urls(css: str, base: str | None = None) -> tuple[list[str], list[str]]:
    """``(imports, assets)``: the stylesheets ``css`` imports and the other
    files it refers to (fonts, images), as absolute http(s) URLs.

    Relative URLs are resolved against ``base``, the stylesheet's own URL. In
    a message's HTML there is no base, so only absolute URLs count.
    """
    css = _CSS_COMMENT.sub("", css)
    imports: list[str] = []
    spans: list[tuple[int, int]] = []

    def absolute(raw: str) -> str | None:
        raw = raw.strip()
        if not raw or raw.lower().startswith("data:"):
            return None
        url = urljoin(base, raw) if base else raw
        return url if _is_http(url) else None

    for m in _CSS_IMPORT.finditer(css):
        spans.append(m.span())
        url = absolute(m.group("u1") if m.group("u1") is not None else m.group("u2") or "")
        if url:
            imports.append(url)
    assets: list[str] = []
    for m in _CSS_URL.finditer(css):
        if any(a <= m.start() < b for a, b in spans):
            continue
        url = absolute(m.group("u"))
        if url:
            assets.append(url)
    return imports, assets


def rewrite_css(css: str, base: str | None, to: Callable[[str | None, bool], str]) -> str:
    """``css`` with each ``@import`` and ``url()`` replaced by ``to(url, is_import)``,
    where ``url`` is absolute as in ``css_urls`` (None when it isn't http(s)).
    ``data:`` URLs are kept as they are. Comments are dropped."""

    def absolute(raw: str) -> str | None:
        raw = raw.strip()
        url = urljoin(base, raw) if base else raw
        return url if _is_http(url) else None

    def imp(m: re.Match[str]) -> str:
        raw = m.group("u1") if m.group("u1") is not None else m.group("u2") or ""
        return f'@import url("{to(absolute(raw), True)}")'

    def asset(m: re.Match[str]) -> str:
        raw = m.group("u")
        if raw.strip().lower().startswith("data:"):
            return m.group(0)
        return f'url("{to(absolute(raw), False)}")'

    out, pos = [], 0
    css = _CSS_COMMENT.sub("", css)
    for m in _CSS_IMPORT.finditer(css):
        out.append(_CSS_URL.sub(asset, css[pos : m.start()]))
        out.append(imp(m))
        pos = m.end()
    out.append(_CSS_URL.sub(asset, css[pos:]))
    return "".join(out)


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
        for url in parse_srcset(_attr(el, "srcset")):
            add(url, el.name)
    for el in soup.find_all(background=True):
        assert isinstance(el, Tag)
        add(_attr(el, "background"), "background")
    for el in soup.find_all("video"):
        assert isinstance(el, Tag)
        add(_attr(el, "poster"), "poster")
    for el in soup.find_all("input"):
        assert isinstance(el, Tag)
        if _attr(el, "type").strip().lower() == "image":
            add(_attr(el, "src"), "input")
    for el in soup.find_all(style=True):
        assert isinstance(el, Tag)
        for url in css_urls(_attr(el, "style"))[1]:
            add(url, "style")
    for el in soup.find_all("style"):
        imports, assets = css_urls(el.get_text() or "")
        for url in imports:
            add(url, "stylesheet")
        for url in assets:
            add(url, "style")
    for el in soup.find_all("link"):
        assert isinstance(el, Tag)
        rels = {r.lower() for r in el.get_attribute_list("rel") if r}
        if "stylesheet" in rels:
            add(_attr(el, "href"), "stylesheet")
        elif rels & _ICON_RELS:
            add(_attr(el, "href"), "icon")

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
