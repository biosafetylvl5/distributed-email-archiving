"""``dea-capture`` command line.

``inventory`` is the offline dry run. ``issue`` is the first capture stage:
it stores one message's embeds and link inventory. ``healthcheck`` is the
pod's liveness probe. The link capture stages follow in later phases.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from . import __version__, health
from .fetch import HttpFetcher
from .guard import GuardedFetcher, IPNetwork
from .inventory import Inventory, inventory
from .issue import process_issue
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
    http = HttpFetcher(user_agent=os.environ.get("DEA_USER_AGENT") or HttpFetcher().user_agent)

    def make_fetcher(inv: Inventory) -> GuardedFetcher:
        return GuardedFetcher(http, extra_blocked=blocked, refused_urls=inv.refused_urls())

    result = process_issue(raw, store, make_fetcher, source=source, force=args.force)
    print(result.line())
    return 0


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
    p.set_defaults(func=_cmd_issue)

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
