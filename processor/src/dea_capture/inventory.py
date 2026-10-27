"""Everything one message would cause to be fetched, decided offline.

This is the dry run: it makes no requests. The capture stages consume the
same records, so what the dry run shows is what they will do.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from .classify import Classified, Decision, classify
from .extract import Embed, extract_embeds, extract_links, parse


@dataclass(frozen=True)
class Inventory:
    embeds: list[Embed]
    cid_parts: list[str]
    links: list[Classified]
    list_unsubscribe: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "embeds": [asdict(e) for e in self.embeds],
            "cid_parts": self.cid_parts,
            "links": [asdict(c) for c in self.links],
            "list_unsubscribe": list(self.list_unsubscribe),
        }

    def summary(self) -> dict[str, Any]:
        """Counts only: no URLs, so it's safe to share."""
        return {
            "embeds": len(self.embeds),
            "tracking_pixels": sum(e.pixel for e in self.embeds),
            "cid_parts": len(self.cid_parts),
            "links": len(self.links),
            "by_class": dict(Counter(str(c.link_class) for c in self.links)),
            "by_decision": dict(Counter(str(c.decision) for c in self.links)),
            "unique_canonical": len({c.canonical for c in self.links if c.canonical}),
        }

    def refused_urls(self) -> frozenset[str]:
        """Hrefs a ``GuardedFetcher`` for this message must refuse."""
        skipped = {c.href for c in self.links if c.decision == Decision.SKIP}
        return frozenset(skipped | set(self.list_unsubscribe))

    def to_fetch(self) -> list[Classified]:
        return [c for c in self.links if c.decision != Decision.SKIP]


def inventory(raw: bytes) -> Inventory:
    msg = parse(raw)
    lu = msg.list_unsubscribe
    return Inventory(
        embeds=extract_embeds(msg.html),
        cid_parts=sorted(msg.cid_parts),
        links=[classify(link.href, link.text, lu) for link in extract_links(msg)],
        list_unsubscribe=lu,
    )
