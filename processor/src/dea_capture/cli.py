"""``dea-capture`` command line.

``inventory`` is the offline dry run. ``issue`` is the first capture stage:
it stores one message's embeds and link inventory, then resolves its links and
queues them. ``capture`` runs one queued capture. ``page`` captures one page
into a local directory, for trying the crawler out. ``summary`` applies the
dashboard's retry requests and rebuilds its summary. ``relink`` follows again
the redirects that couldn't be followed. ``healthcheck`` is the pod's
liveness probe.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, capture, gallery, health, media, page, retry, summary
from .fetch import HostPacer, HttpFetcher
from .guard import GuardedFetcher, IPNetwork
from .inventory import Inventory, inventory
from .issue import process_issue, relink_issue
from .links import Relinked
from .proxy import GuardProxy
from .store import LocalStore, S3Store, Store, split_s3_uri


def _cmd_inventory(args: argparse.Namespace) -> int:
    totals: Counter[str] = Counter()
    for path in args.files:
        inv = inventory(Path(path).read_bytes())
        if args.summary:
            s = inv.summary()
            for key in ("embeds", "tracking_pixels", "cid_parts", "links", "unique_canonical"):
                totals[key] += s[key]
            for group in ("by_class", "by_decision"):
                for k, v in s[group].items():
                    totals[f"{group}.{k}"] += v
            if not args.total:
                print(json.dumps({"file": str(path), **s}))
        else:
            print(json.dumps({"file": str(path), **inv.to_json()}))
    if args.summary and args.total:
        print(json.dumps({"files": len(args.files), **dict(sorted(totals.items()))}, indent=2))
    return 0


def _extra_blocked() -> list[IPNetwork]:
    raw = os.environ.get("DEA_EXTRA_BLOCKED", "")
    return [ipaddress.ip_network(c.strip()) for c in raw.split(",") if c.strip()]


def _cmd_issue(args: argparse.Namespace) -> int:
    source: str = args.source
    store: Store
    if source.startswith("s3://"):
        bucket, key = split_s3_uri(source)
        s3 = S3Store(bucket)
        raw = s3.get(key)
        if raw is None:
            print(f"ISSUE missing source object {key}", file=sys.stderr)
            return 1
        store = LocalStore(Path(args.out)) if args.out else s3
    else:
        raw = Path(source).read_bytes()
        if not args.out and not args.dry_run:
            print("--out DIR is required for a local file", file=sys.stderr)
            return 2
        store = LocalStore(Path(args.out or "."))

    if args.dry_run or os.environ.get("DEA_MODE") == "dry-run":
        s = inventory(raw).summary()
        print("ISSUE dry-run " + json.dumps(s, sort_keys=True))
        return 0

    blocked = _extra_blocked()
    http = HttpFetcher(user_agent=_user_agent())
    # resolving reads status lines and headers, never bodies
    heads = HttpFetcher(user_agent=_user_agent(), max_bytes=0, truncate=True)

    def make_fetcher(inv: Inventory) -> GuardedFetcher:
        return GuardedFetcher(http, extra_blocked=blocked, refused_urls=inv.refused_urls())

    def make_resolver(inv: Inventory) -> GuardedFetcher:
        return GuardedFetcher(heads, extra_blocked=blocked, refused_urls=inv.refused_urls())

    result = process_issue(
        raw, store, make_fetcher, source=source, force=args.force,
        make_resolver=None if args.no_links else make_resolver,
    )  # fmt: skip
    print(result.line())
    return 0


def _user_agent() -> str:
    return os.environ.get("DEA_USER_AGENT") or HttpFetcher().user_agent


# bodies are held in memory; a bigger one is deferred for capture-media
DIRECT_MAX_BYTES = 100 * 1024 * 1024


def _cmd_capture(args: argparse.Namespace) -> int:
    bucket, key = split_s3_uri(args.due)
    store = S3Store(bucket)
    blocked = _extra_blocked()
    http = HttpFetcher(user_agent=_user_agent(), max_bytes=DIRECT_MAX_BYTES, total_timeout=300)
    fetcher = GuardedFetcher(http, extra_blocked=blocked)
    result = capture.run(
        store, key, fetcher, now=datetime.now(UTC),
        wayback_fetcher=None if args.no_wayback else fetcher,
        page_crawler=_page_crawler(blocked),
        media_grabber=_media_grabber(blocked),
        gallery_collector=_gallery_collector(blocked),
    )  # fmt: skip
    print(result.line())
    return 0


def _page_crawler(blocked: list[IPNetwork]) -> Callable[[str], page.PageOutcome]:
    def crawler(url: str, workdir: Path) -> page.Crawl:
        return page.run_crawler(url, workdir, GuardProxy(extra_blocked=blocked), _user_agent())

    return lambda url: page.crawl_page(url, crawler, blocked)


def _media_grabber(blocked: list[IPNetwork]) -> capture.MediaGrabber:
    def grabber(url: str, workdir: Path) -> media.Grab:
        return media.run_ytdlp(url, workdir, GuardProxy(extra_blocked=blocked))

    return lambda url, keep: media.grab_media(url, grabber, keep, blocked)


def _gallery_collector(blocked: list[IPNetwork]) -> capture.MediaGrabber:
    def collector(url: str, workdir: Path) -> gallery.Haul:
        return gallery.run_gallerydl(url, workdir, GuardProxy(extra_blocked=blocked))

    return lambda url, keep: gallery.collect(url, collector, keep, blocked)


def _cmd_media(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    blocked = _extra_blocked()
    grab, keep = (
        (_gallery_collector(blocked), gallery.store_gallery) if args.gallery
        else (_media_grabber(blocked), media.store_media)
    )  # fmt: skip
    got = grab(args.url, lambda g: keep(LocalStore(out), "", args.url, g, at))
    summary = {
        "outcome": got.outcome, "error": got.error, "files": sorted(got.files),
        "duration": got.info.get("duration"), "format": got.info.get("format"), **got.details,
    }  # fmt: skip
    print(json.dumps(summary))
    return 0 if got.outcome == "ok" else 1


def _cmd_page(args: argparse.Namespace) -> int:
    got = _page_crawler(_extra_blocked())(args.url)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if got.outcome == "ok":
        page.store_page(LocalStore(out), "", args.url, got,
                        datetime.now(UTC).isoformat(timespec="seconds"))  # fmt: skip
    summary = {
        "outcome": got.outcome, "http_status": got.http_status, "error": got.error,
        "files": {name: len(data) for name, data in got.files.items()},
        "proxy": got.info.get("proxy"), "limit_hit": got.info.get("limit_hit"),
    }  # fmt: skip
    print(json.dumps(summary))
    return 0 if got.outcome == "ok" else 1


def _bucket(uri: str) -> str:
    bucket = uri.removeprefix("s3://").strip("/")
    if not bucket or "/" in bucket:
        raise SystemExit(f"not an s3://bucket URI: {uri}")
    return bucket


def _relinker(store: Store, pacer: HostPacer) -> retry.Relinker:
    """Follows a message's failed redirects again, as the issue stage does."""
    blocked = _extra_blocked()
    heads = HttpFetcher(user_agent=_user_agent(), max_bytes=0, truncate=True)

    def make_resolver(inv: Inventory) -> GuardedFetcher:
        return GuardedFetcher(heads, extra_blocked=blocked, refused_urls=inv.refused_urls())

    def relink(raw: bytes, deadline: float) -> Relinked:
        return relink_issue(raw, store, make_resolver, now=datetime.now(UTC), pacer=pacer,
                            deadline=deadline)  # fmt: skip

    return relink


def _cmd_summary(args: argparse.Namespace) -> int:
    store = S3Store(_bucket(args.bucket))
    if not args.no_retries:
        keys = [o.key for o in store.listing(summary.RETRY_REQUESTS)]
        if keys:
            relink = _relinker(store, HostPacer(retry.RELINK_INTERVAL))
            done = retry.apply_requests(store, keys, datetime.now(UTC), relink)
            print("RETRY " + json.dumps([{k: v for k, v in r.items() if k != "sha"} for r in done]))
    print(json.dumps(summary.build(store)))
    return 0


def _cmd_relink(args: argparse.Namespace) -> int:
    """Follow again the redirects that couldn't be followed, message by message."""
    store = S3Store(_bucket(args.bucket))
    pacer = HostPacer(args.interval)
    relink = _relinker(store, pacer)
    # the summary's cache of each status record's metadata saves a HEAD each
    try:
        cached = json.loads(store.get(summary.STATE_KEY) or b"{}").get("statuses") or {}
    except ValueError:
        cached = {}
    sources = []
    for o in store.listing("issue-status/"):
        meta = cached[o.key][1] if o.key in cached else store.metadata(o.key) or {}
        if meta.get("links_unresolved", "0") not in ("", "0"):
            sources.append(meta.get("source", ""))
    totals: Counter[str] = Counter(messages=len(sources))
    deadline = float("inf")
    for i, source in enumerate(sources[: args.limit]):
        raw = store.get(source.removeprefix(f"s3://{store.bucket}/"))
        if raw is None:
            totals["missing"] += 1
            continue
        got = relink(raw, deadline)
        totals["tried"] += got.tried
        totals["resolved"] += got.resolved
        totals["done"] = i + 1
        print("RELINK " + json.dumps(dict(totals)), flush=True)
        if got.rate_limited:
            totals["rate_limited"] = 1
            break
    print("RELINK " + json.dumps(dict(totals)))
    return 1 if totals["rate_limited"] else 0


def _cmd_healthcheck(args: argparse.Namespace) -> int:
    try:
        text = health.fetch_metrics(args.metrics)
    except OSError as exc:
        print(f"unhealthy: metrics unreachable: {exc}")
        return 1
    ok, why = health.check(text, args.max_scan_age)
    print(("healthy: " if ok else "unhealthy: ") + why)
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dea-capture", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "inventory",
        help="list what each .eml would fetch, without fetching anything",
        description=(
            "Print, for each .eml, its embeds and every link with its class, decision "
            "and canonical URL. Makes no network requests. The full output contains the "
            "original hrefs, which carry per-subscriber tokens: keep it private. "
            "--summary prints counts only."
        ),
    )
    p.add_argument("files", nargs="+", metavar="EML")
    p.add_argument("--summary", action="store_true", help="counts only, no URLs")
    p.add_argument("--total", action="store_true", help="with --summary: one total for all files")
    p.set_defaults(func=_cmd_inventory)

    p = sub.add_parser(
        "issue",
        help="store one message's embeds and link inventory",
        description=(
            "Fetch every embed of one archived message (tracking pixels included) through "
            "the guard and store them, with a manifest and the link inventory, under "
            "issues/<sha256>/ in the source bucket, or in --out DIR. Skips messages that "
            "already have a manifest. DEA_MODE=dry-run, or --dry-run, prints counts and "
            "fetches nothing."
        ),
    )
    p.add_argument("source", metavar="SOURCE", help="s3://bucket/key.eml or a local .eml")
    p.add_argument("--out", metavar="DIR", help="write to this directory, not the bucket")
    p.add_argument("--force", action="store_true", help="redo a message that is already done")
    p.add_argument("--dry-run", action="store_true", help="fetch and write nothing")
    p.add_argument("--no-links", action="store_true", help="embeds only: don't resolve or queue")
    p.set_defaults(func=_cmd_issue)

    p = sub.add_parser(
        "capture",
        help="run one queued capture",
        description=(
            "Run the capture a captures/due/<kind>/ key asks for: request the canonical URL, "
            "store what comes back under captures/, and schedule a retry if it failed. After "
            "the last retry, store the closest Wayback Machine snapshot instead. Prints one "
            "line with no URLs."
        ),
    )
    p.add_argument("due", metavar="DUE", help="s3://bucket/captures/due/<kind>/<key>.json")
    p.add_argument("--no-wayback", action="store_true", help="no Wayback Machine fallback")
    p.set_defaults(func=_cmd_capture)

    p = sub.add_parser(
        "page",
        help="capture one page with the crawler, into a local directory",
        description=(
            "Load URL in browsertrix-crawler behind the guarding proxy and write page.wacz, "
            "screenshot.png, text.txt and capture.json to --out DIR. Needs the crawler, so "
            "run it in the container image. Prints a summary with no URLs."
        ),
    )
    p.add_argument("url", metavar="URL")
    p.add_argument("--out", metavar="DIR", required=True)
    p.set_defaults(func=_cmd_page)

    p = sub.add_parser(
        "media",
        help="capture one video or audio post with yt-dlp, into a local directory",
        description=(
            "Grab URL with yt-dlp behind the guarding proxy and write media.<ext>, "
            "thumbnail.<ext>, info.json and capture.json to --out DIR. Prints a summary "
            "with no URLs."
        ),
    )
    p.add_argument("url", metavar="URL")
    p.add_argument("--out", metavar="DIR", required=True)
    p.set_defaults(func=_cmd_media, gallery=False)

    p = sub.add_parser(
        "gallery",
        help="capture one social media post with gallery-dl, into a local directory",
        description=(
            "Collect URL's post with gallery-dl behind the guarding proxy and write "
            "files/001.<ext>..., info.json and capture.json to --out DIR. Prints a "
            "summary with no URLs."
        ),
    )
    p.add_argument("url", metavar="URL")
    p.add_argument("--out", metavar="DIR", required=True)
    p.set_defaults(func=_cmd_media, gallery=True)

    p = sub.add_parser(
        "summary",
        help="rebuild the dashboard's summary object",
        description=(
            "List the archive, the issue status records and the capture index in "
            "s3://BUCKET, read the metadata of what changed since the last run, and write "
            "dashboard/summary.json for the admin Worker. Prints counts only."
        ),
    )
    p.add_argument("bucket", metavar="s3://BUCKET")
    p.add_argument("--no-retries", action="store_true",
                   help="don't apply the dashboard's retry requests first")  # fmt: skip
    p.set_defaults(func=_cmd_summary)

    p = sub.add_parser(
        "relink",
        help="follow again the tracking redirects that couldn't be followed",
        description=(
            "For each message in s3://BUCKET with links whose redirect couldn't be followed "
            "(a tracker that answered 429, say), follow them again and queue where they land. "
            "Stops at the first 429. Prints counts only."
        ),
    )
    p.add_argument("bucket", metavar="s3://BUCKET")
    p.add_argument("--interval", type=float, default=retry.RELINK_INTERVAL,
                   help="seconds between requests to one host (default %(default)s)")  # fmt: skip
    p.add_argument("--limit", type=int, default=None, help="at most this many messages")
    p.set_defaults(func=_cmd_relink)

    p = sub.add_parser("healthcheck", help="liveness probe for the courier service")
    p.add_argument("--metrics", default="http://127.0.0.1:8000/metrics")
    p.add_argument(
        "--max-scan-age", type=float, default=900.0, metavar="SECONDS",
        help="fail when the S3 poller's last completed scan is older than this",
    )  # fmt: skip
    p.set_defaults(func=_cmd_healthcheck)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
