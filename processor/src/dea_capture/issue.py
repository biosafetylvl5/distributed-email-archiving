"""The ``issue`` stage: one archived message in, its embeds and link
inventory out.

Everything goes under ``issues/<sha256 of the .eml>/``:

- ``links.json``: the classified link inventory. Private: original hrefs
  carry subscriber tokens.
- ``assets/<sha256 of body><ext>``: each embed body, content-addressed, so a
  logo shared by every issue is still one object per issue, never fetched twice
  within one.
- ``assets/manifest.json``: one record per embed and inline ``cid:`` part.
  Written last, so its presence means the issue is done and a re-run (courier
  re-emits every key on restart) skips it.

One more object sits outside that prefix, so the dashboard can list every
issue's status without reading bodies:

- ``issue-status/<sha>.json``: counts only, also set as the object's
  metadata (R2 ``customMetadata``), plus the source key. Written after the
  manifest; an issue skipped as done gets one if it's missing.

Each embed is fetched once. Failures are recorded with their reason; the
retry schedule comes with the capture stages.

A manifest written by an older ``EXTRACT_VERSION`` is redone incrementally:
the message is inventoried again, embeds the old manifest already has as
``ok`` are carried over without a request, and only the rest are fetched.
Bump the version whenever a change to extraction alters which URLs a
message embeds.

Stylesheets are read after they are stored: the fonts and images they refer
to are fetched as ``css`` embeds, and their ``@import``s as further
stylesheets, up to ``MAX_CSS_DEPTH`` deep and ``MAX_CSS_ASSETS`` per
message. Past the limit they are recorded as ``deferred:budget``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from . import __version__
from .extract import Embed, css_urls, parse
from .fetch import FetchError, HostPacer, TooLarge, follow
from .guard import ForbiddenFetch, GuardedFetcher
from .inventory import Inventory, inventory
from .store import Store, put_json

Status = Literal["ok", "failed", "refused", "deferred:size", "deferred:budget"]

# 2: srcset URLs keep their commas
# 3: poster, input type=image, icon links, @import, and what stylesheets refer to
EXTRACT_VERSION = 3
MAX_CSS_DEPTH = 3
MAX_CSS_ASSETS = 300
_STYLESHEETS = frozenset({"stylesheet", "css-import"})


def issue_prefix(sha: str) -> str:
    return f"issues/{sha}/"


def manifest_key(sha: str) -> str:
    return f"{issue_prefix(sha)}assets/manifest.json"


def links_key(sha: str) -> str:
    return f"{issue_prefix(sha)}links.json"


def status_key(sha: str) -> str:
    return f"issue-status/{sha}.json"


def issue_status(manifest: dict[str, Any], links: dict[str, Any] | None) -> dict[str, str]:
    """Counts for the dashboard, as R2 metadata: string values, no URLs.

    ``state`` is ``partial`` while any embed failed or was deferred, else
    ``done``; refused embeds are deliberate, so they don't count against it.
    """
    assets = manifest.get("assets", [])
    by_status = Counter(str(a.get("status")) for a in assets)
    deferred = sum(n for k, n in by_status.items() if k.startswith("deferred"))
    link_list = (links or {}).get("links", [])
    decisions = Counter(str(link.get("decision")) for link in link_list)
    counts = {
        "assets_ok": by_status["ok"],
        "assets_failed": by_status["failed"],
        "assets_refused": by_status["refused"],
        "assets_deferred": deferred,
        "pixels": sum(1 for a in assets if a.get("pixel")),
        "links": len(link_list),
        "links_capture": decisions["capture"],
        "links_resolve": decisions["resolve"],
        "links_skip": decisions["skip"],
    }
    return {
        "source": str(manifest.get("source", "")),
        "state": "partial" if by_status["failed"] or deferred else "done",
        "extract_version": str(manifest.get("extract_version", 1)),
        "completed_at": str(manifest.get("completed_at", "")),
        **{k: str(v) for k, v in counts.items()},
    }


def write_status(
    store: Store, sha: str, manifest: dict[str, Any], links: dict[str, Any] | None
) -> None:
    meta = issue_status(manifest, links)
    put_json(store, status_key(sha), {"sha256": sha, **meta}, metadata=meta)


@dataclass
class AssetRecord:
    url: str
    via: str
    pixel: bool
    status: Status
    key: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    content_type: str | None = None
    http_status: int | None = None
    # redirects followed, when there were any
    chain: list[str] = field(default_factory=list)
    # for css and css-import: the stylesheet that refers to it
    found_in: str | None = None
    error: str | None = None
    fetched_at: str | None = None


@dataclass
class IssueResult:
    sha256: str
    skipped: bool
    counts: dict[str, int]

    def line(self) -> str:
        """One stdout line, counts only: courier logs it."""
        if self.skipped:
            return f"ISSUE {self.sha256} skipped: already done"
        detail = " ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"ISSUE {self.sha256} {detail}"


def _ext(content_type: str | None, url: str) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(base) if base else None
    if ext is None:
        ext = mimetypes.guess_extension(mimetypes.guess_type(url.split("?")[0])[0] or "") or ""
    return {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store_body(
    store: Store, sha: str, body: bytes, content_type: str | None, url: str
) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    key = f"{issue_prefix(sha)}assets/{digest}{_ext(content_type, url)}"
    store.put(key, body, content_type or "application/octet-stream", if_absent=True)
    return key, digest


def fetch_embed(
    url: str, fetcher: GuardedFetcher, store: Store, sha: str, pacer: HostPacer
) -> AssetRecord:
    rec = AssetRecord(url=url, via="", pixel=False, status="failed", fetched_at=_now())
    try:
        pacer.wait(url)
        fetched = follow(fetcher, url, "embed")
    except ForbiddenFetch as exc:
        rec.status, rec.error = "refused", str(exc)
        return rec
    except TooLarge as exc:
        rec.status, rec.error = "deferred:size", str(exc)
        return rec
    except FetchError as exc:
        rec.error = str(exc)
        return rec
    resp = fetched.response
    rec.http_status = resp.status
    rec.chain = list(fetched.chain[1:])
    rec.content_type = resp.headers.get("content-type")
    if not 200 <= resp.status < 300:
        rec.error = f"HTTP {resp.status}"
        return rec
    rec.key, rec.sha256 = _store_body(store, sha, resp.body, rec.content_type, url)
    rec.bytes = len(resp.body)
    rec.status = "ok"
    return rec


def process_issue(
    raw: bytes,
    store: Store,
    make_fetcher: Callable[[Inventory], GuardedFetcher],
    *,
    source: str,
    force: bool = False,
    pacer: HostPacer | None = None,
) -> IssueResult:
    sha = hashlib.sha256(raw).hexdigest()
    reuse: dict[str, AssetRecord] = {}
    if not force:
        previous = store.get(manifest_key(sha))
        if previous is not None:
            old = json.loads(previous)
            if old.get("extract_version", 1) >= EXTRACT_VERSION:
                if not store.exists(status_key(sha)):
                    links = store.get(links_key(sha))
                    write_status(store, sha, old, json.loads(links) if links else None)
                return IssueResult(sha, skipped=True, counts={})
            for a in old.get("assets", []):
                if a.get("status") == "ok" and a.get("via") != "cid":
                    reuse[a["url"]] = AssetRecord(**a)
    pacer = pacer or HostPacer()
    inv = inventory(raw)
    links_doc = {"source": source, **inv.to_json()}
    put_json(store, links_key(sha), links_doc)

    fetcher = make_fetcher(inv)
    records: list[AssetRecord] = []
    reused = 0
    # (embed, the stylesheet it came from, stylesheet depth)
    queue: deque[tuple[Embed, str | None, int]] = deque((e, None, 0) for e in inv.embeds)
    seen = {e.url for e in inv.embeds}
    from_css = 0
    while queue:
        embed, parent, depth = queue.popleft()
        if parent is not None and from_css >= MAX_CSS_ASSETS:
            rec = AssetRecord(
                url=embed.url, via="", pixel=False, status="deferred:budget",
                error=f"over {MAX_CSS_ASSETS} stylesheet assets",
            )  # fmt: skip
        elif embed.url in reuse:
            rec, reused = reuse[embed.url], reused + 1
        else:
            rec = fetch_embed(embed.url, fetcher, store, sha, pacer)
        if parent is not None:
            from_css += 1
        rec.via, rec.pixel, rec.found_in = embed.via, embed.pixel, parent
        records.append(rec)
        if embed.via not in _STYLESHEETS or rec.status != "ok" or rec.key is None:
            continue
        if depth >= MAX_CSS_DEPTH:
            continue
        body = store.get(rec.key)
        if body is None:
            continue
        base = rec.chain[-1] if rec.chain else rec.url
        imports, assets = css_urls(body.decode("utf-8", errors="replace"), base)
        for url, via in [(u, "css-import") for u in imports] + [(u, "css") for u in assets]:
            if url not in seen:
                seen.add(url)
                queue.append((Embed(url, via), embed.url, depth + 1))
    cid_parts = parse(raw).cid_parts
    for cid, part in cid_parts.items():
        key, digest = _store_body(store, sha, part.data, part.content_type, part.filename or "")
        records.append(
            AssetRecord(
                url=f"cid:{cid}", via="cid", pixel=False, status="ok", key=key, sha256=digest,
                bytes=len(part.data), content_type=part.content_type,
            )
        )  # fmt: skip

    counts: dict[str, int] = {"embeds": len(inv.embeds), "cid": len(cid_parts)}
    if from_css:
        counts["from_css"] = from_css
    for rec in records:
        counts[rec.status] = counts.get(rec.status, 0) + 1
    if reused:
        counts["reused"] = reused
    manifest = {
        "source": source,
        "sha256": sha,
        "generator": f"dea-capture {__version__}",
        "extract_version": EXTRACT_VERSION,
        "completed_at": _now(),
        "counts": counts,
        "assets": [asdict(r) for r in records],
    }
    put_json(store, manifest_key(sha), manifest)
    write_status(store, sha, manifest, links_doc)
    return IssueResult(sha, skipped=False, counts=counts)
