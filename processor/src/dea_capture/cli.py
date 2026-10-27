"""``dea-capture`` command line.

Only the offline ``inventory`` command exists so far. The capture stages
(``issue``, ``page``, ``media``, ``direct``) follow in later phases.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .inventory import inventory


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

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
