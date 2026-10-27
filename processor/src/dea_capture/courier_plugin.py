"""``s3_due_poller``: courier's ``s3_poller``, but a key under
``captures/due/`` is only emitted once the time in its name has passed.

A key that isn't due yet is left out of the poller's seen-set, so a later
scan emits it. This is what turns ``captures/due/`` into a retry schedule
(see ``capture``). Needs the ``service`` extra; courier finds it through the
``courier.data_monitors`` entry point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from courier.plugins.data_monitors.s3_poller import S3Poller

from .capture import parse_due_key


def is_due(key: str, now: datetime | None = None) -> bool:
    try:
        _, when, _ = parse_due_key(key)
    except ValueError:
        return False
    return when <= (now or datetime.now(UTC))


class S3DuePoller(S3Poller):  # type: ignore[misc,unused-ignore]
    name: ClassVar[str] = "s3_due_poller"
    version: ClassVar[str] = "0.1.0"

    def _matches_suffix(self, key: str) -> bool:
        return bool(super()._matches_suffix(key)) and is_due(key)
