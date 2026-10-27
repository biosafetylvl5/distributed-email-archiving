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

Each embed is fetched once. Failures are recorded with their reason; the
retry schedule comes with the capture stages.

A manifest written by an older ``EXTRACT_VERSION`` is redone incrementally:
the message is inventoried again, embeds the old manifest already has as
``ok`` are carried over without a request, and only the rest are fetched.
Bump the version whenever a change to extraction alters which URLs a
message embeds.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from . import __version__
from .extract import parse
from .fetch import FetchError, HostPacer, TooLarge, follow
from .guard import ForbiddenFetch, GuardedFetcher
from .inventory import Inventory, inventory
from .store import Store, put_json

Status = Literal["ok", "failed", "refused", "deferred:size"]

# 2: srcset URLs keep their commas
EXTRACT_VERSION = 2


def issue_prefix(sha: str) -> str:
    return f"issues/{sha}/"


def manifest_key(sha: str) -> str:
    return f"{issue_prefix(sha)}assets/manifest.json"


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
                return IssueResult(sha, skipped=True, counts={})
            for a in old.get("assets", []):
                if a.get("status") == "ok" and a.get("via") != "cid":
                    reuse[a["url"]] = AssetRecord(**a)
    pacer = pacer or HostPacer()
    inv = inventory(raw)
    put_json(store, f"{issue_prefix(sha)}links.json", {"source": source, **inv.to_json()})

    fetcher = make_fetcher(inv)
    records: list[AssetRecord] = []
    reused = 0
    for embed in inv.embeds:
        if embed.url in reuse:
            rec, reused = reuse[embed.url], reused + 1
        else:
            rec = fetch_embed(embed.url, fetcher, store, sha, pacer)
        rec.via, rec.pixel = embed.via, embed.pixel
        records.append(rec)
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
    for rec in records:
        counts[rec.status] = counts.get(rec.status, 0) + 1
    if reused:
        counts["reused"] = reused
    put_json(
        store,
        manifest_key(sha),
        {
            "source": source,
            "sha256": sha,
            "generator": f"dea-capture {__version__}",
            "extract_version": EXTRACT_VERSION,
            "completed_at": _now(),
            "counts": counts,
            "assets": [asdict(r) for r in records],
        },
    )
    return IssueResult(sha, skipped=False, counts=counts)
