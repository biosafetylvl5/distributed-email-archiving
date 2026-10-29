"""A message as the admin dashboard shows it, under ``issues/<sha>/view/``:

- ``index.html``: the HTML part (or the text part, escaped), rewritten so
  that it loads nothing from elsewhere. Embeds point at the stored copies
  (``assets/<name>``, and ``css/<name>`` for stylesheets); one that wasn't
  stored, such as a tracking pixel, is dropped. Scripts, frames, ``<meta>``
  (a refresh would load a URL) and event handlers are removed.
- ``css/<name>``: each stored stylesheet, rewritten the same way.
- ``meta.json``: the headers, the MIME parts, sizes and what was removed.
  Written last, with ``view_version`` as metadata, so a view of an older
  ``VIEW_VERSION`` is rendered again.

Links point where they lead, never at a tracker: at the landing page the
link stage resolved, or a ``capture`` link's canonical URL, which carry no
reader token. Every other link (unsubscribe, actions, trackers that couldn't
be followed) loses its ``href``, so opening a message clicks nothing.

The Worker serves these from a sandboxed frame whose CSP allows only its
own origin, so a rewrite missed here still loads nothing.
"""

from __future__ import annotations

import html as htmllib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from bs4 import BeautifulSoup, Tag

from .classify import Classified, Decision, LinkClass, tracking_vendor
from .extract import parse, parse_srcset, rewrite_css
from .inventory import inventory
from .store import Store, put_json

VIEW_VERSION = 1
_STYLESHEETS = ("stylesheet", "css-import")
# removed with their content
_DROP = ["script", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "meta",
         "template", "portal", "form"]  # fmt: skip
# attributes that load an embed: pointed at the stored copy, or removed
_EMBED_ATTRS = ("src", "background", "poster")
# other attributes that request a URL, or act: removed
_URL_ATTRS = ("lowsrc", "dynsrc", "longdesc", "data", "xlink:href", "action", "formaction", "ping",
              "srcdoc", "http-equiv", "manifest", "codebase", "cite", "usemap")  # fmt: skip
_WHY = {
    LinkClass.TRACKING_REDIRECT: "a tracking link that couldn't be followed",
    LinkClass.UNSUBSCRIBE: "an unsubscribe link",
    LinkClass.ACTION: "it acts on the reader's subscription",
    LinkClass.NON_WEB: "not a web link",
}


def view_prefix(sha: str) -> str:
    return f"issues/{sha}/view/"


def view_meta_key(sha: str) -> str:
    return view_prefix(sha) + "meta.json"


@dataclass
class View:
    html: bytes
    css: dict[str, bytes]  # name -> rewritten stylesheet
    meta: dict[str, Any]
    removed: Counter[str] = field(default_factory=Counter)


def _name(key: str) -> str:
    return key.rsplit("/", 1)[1]


def _attr(el: Tag, name: str) -> str:
    value = el.get(name)
    return " ".join(value) if isinstance(value, list) else (value or "")


def _header(msg: EmailMessage, name: str) -> str:
    value = msg.get(name)
    return " ".join(str(value).split()) if value is not None else ""


def _parts(msg: EmailMessage) -> list[dict[str, Any]]:
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        info: dict[str, Any] = {
            "content_type": part.get_content_type(),
            "bytes": len(payload) if isinstance(payload, bytes) else 0,
        }
        if part.get_filename():
            info["filename"] = part.get_filename()
        if part.get_content_disposition():
            info["disposition"] = part.get_content_disposition()
        if part.get("Content-ID"):
            info["cid"] = True
        parts.append(info)
    return parts


class _Rewriter:
    def __init__(self, store: Store, manifest: dict[str, Any]) -> None:
        self.stored: dict[str, dict[str, Any]] = {
            a["url"]: a for a in manifest.get("assets", [])
            if a.get("status") == "ok" and a.get("key")
        }  # fmt: skip
        self.pixels = {a["url"] for a in manifest.get("assets", []) if a.get("pixel")}
        self.store = store
        self.removed: Counter[str] = Counter()

    def target(self, url: str | None, *, from_css: bool) -> str | None:
        """Where a stored embed is served, relative to index.html or to a
        stylesheet in css/; None if it wasn't stored."""
        a = self.stored.get((url or "").strip())
        if a is None:
            if url:
                self.removed["pixels" if url.strip() in self.pixels else "embeds"] += 1
            return None
        if a.get("via") in _STYLESHEETS:
            return _name(a["key"]) if from_css else f"css/{_name(a['key'])}"
        return f"../assets/{_name(a['key'])}" if from_css else f"assets/{_name(a['key'])}"

    def css_to(self, *, from_css: bool) -> Callable[[str | None, bool], str]:
        return lambda url, _imp: self.target(url, from_css=from_css) or "about:invalid"

    def stylesheets(self) -> dict[str, bytes]:
        out = {}
        for url, a in self.stored.items():
            if a.get("via") not in _STYLESHEETS:
                continue
            body = self.store.get(a["key"])
            if body is None:
                continue
            base = a["chain"][-1] if a.get("chain") else url
            css = rewrite_css(
                body.decode("utf-8", errors="replace"), base, self.css_to(from_css=True)
            )
            out[_name(a["key"])] = css.encode()
        return out


def _rewrite_embeds(soup: BeautifulSoup, rw: _Rewriter) -> None:
    for el in soup.find_all(True):
        assert isinstance(el, Tag)
        if el.name == "link":
            rels = {r.lower() for r in el.get_attribute_list("rel") if r}
            to = rw.target(_attr(el, "href"), from_css=False) if "stylesheet" in rels else None
            if to and to.startswith("css/"):
                el.attrs = {"rel": "stylesheet", "href": to}
            else:
                el.decompose()
            continue
        for attr in _EMBED_ATTRS:
            if not el.has_attr(attr):
                continue
            value = _attr(el, attr).strip()
            if value.lower().startswith("data:"):
                continue
            if value.lower().startswith("cid:"):  # as the issue stage records it
                value = "cid:" + value[4:].strip("<>")
            to = rw.target(value, from_css=False)
            if to:
                el[attr] = to
            else:
                del el[attr]
        if el.has_attr("srcset"):
            value = _attr(el, "srcset")
            urls = parse_srcset(value)
            targets = [rw.target(u, from_css=False) for u in urls]
            if urls and all(targets):
                for u, t in zip(urls, targets, strict=True):
                    value = value.replace(u, t or "")
                el["srcset"] = value
            else:
                del el["srcset"]
        if el.has_attr("style"):
            el["style"] = rewrite_css(_attr(el, "style"), None, rw.css_to(from_css=False))
        if el.name == "style":
            el.string = rewrite_css(el.get_text(), None, rw.css_to(from_css=False))


def _rewrite_links(soup: BeautifulSoup, links: dict[str, Classified],
                   resolved: dict[str, dict[str, Any]], counts: Counter[str]) -> None:  # fmt: skip
    for el in soup.find_all(["a", "area"]):
        assert isinstance(el, Tag)
        href = _attr(el, "href").strip()
        if not href or href.startswith("#"):
            continue
        c, r = links.get(href), resolved.get(href)
        to = (r or {}).get("canonical") or (
            c.canonical if c and c.decision == Decision.CAPTURE else None
        )
        if to and to.lower().startswith(("http://", "https://")) and not tracking_vendor(to):
            el["href"], el["target"], el["rel"] = to, "_blank", "noopener noreferrer"
            counts["links_kept"] += 1
            continue
        del el["href"]
        why = _WHY.get(c.link_class, "not followed") if c else "not followed"
        if c and c.link_class == LinkClass.TRACKING_REDIRECT and r is None:
            why = "a tracking link, not followed yet"
        el["title"] = f"Link removed: {why}"
        counts["links_removed"] += 1


def _strip(soup: BeautifulSoup) -> None:
    for el in soup.find_all(_DROP):
        assert isinstance(el, Tag)
        el.decompose()
    for el in soup.find_all(True):
        assert isinstance(el, Tag)
        for attr in list(el.attrs):
            name = attr.lower()
            if (
                name.startswith("on")
                or name in _URL_ATTRS
                or (name == "href" and el.name not in ("a", "area", "link"))
            ):
                del el[attr]


def render(raw: bytes, store: Store, manifest: dict[str, Any], captures: dict[str, Any] | None,
           *, now: datetime | None = None) -> View:  # fmt: skip
    msg = parse(raw)
    rw = _Rewriter(store, manifest)
    counts: Counter[str] = Counter()
    if msg.html:
        soup = BeautifulSoup(msg.html, "html.parser")
        _strip(soup)
        _rewrite_embeds(soup, rw)
        links = {c.href: c for c in inventory(raw).links}
        resolved = {str(r.get("href")): r for r in (captures or {}).get("links", [])}
        _rewrite_links(soup, links, resolved, counts)
        body = str(soup)
    else:
        body = (
            '<pre style="white-space:pre-wrap;font:14px/1.45 ui-monospace,monospace">'
            f"{htmllib.escape(msg.text or '')}</pre>"
        )
    doc = '<!doctype html>\n<meta charset="utf-8">\n' + body
    counts.update(rw.removed)
    assets = {a["key"]: a.get("bytes") or 0 for a in rw.stored.values()}
    meta = {
        "view_version": VIEW_VERSION,
        "rendered_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(manifest.get("source", "")),
        "headers": {
            h.lower(): _header(msg.message, h)
            for h in ("Subject", "From", "To", "Date", "List-Id")
            if msg.message.get(h)
        },
        "body": "html" if msg.html else "text",
        "parts": _parts(msg.message),
        "sizes": {"eml": len(raw), "assets": sum(assets.values()), "asset_files": len(assets)},
        "removed": dict(counts),
    }
    return View(doc.encode(), rw.stylesheets(), meta, counts)


def view_current(store: Store, sha: str) -> bool:
    try:
        meta = json.loads(store.get(view_meta_key(sha)) or b"{}")
    except ValueError:
        return False
    return isinstance(meta, dict) and meta.get("view_version") == VIEW_VERSION


def write_view(store: Store, sha: str, raw: bytes, manifest: dict[str, Any],
               captures: dict[str, Any] | None) -> None:  # fmt: skip
    v = render(raw, store, manifest, captures)
    prefix = view_prefix(sha)
    for name, css in v.css.items():
        store.put(f"{prefix}css/{name}", css, "text/css; charset=utf-8")
    store.put(f"{prefix}index.html", v.html, "text/html; charset=utf-8")
    put_json(store, view_meta_key(sha), v.meta, metadata={"view_version": str(VIEW_VERSION)})
