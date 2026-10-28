"""courier plugins for dea-capture's service.

``s3_due_poller``: courier's ``s3_poller``, but a key under
``captures/due/`` is only emitted once the time in its name has passed.

A key that isn't due yet is left out of the poller's seen-set, so a later
scan emits it. This is what turns ``captures/due/`` into a retry schedule
(see ``capture``).

``ticker``: emits one datum every ``interval_seconds``, for a job that runs on
a schedule rather than on new data (the dashboard summary).

Both need the ``service`` extra; courier finds them through the
``courier.data_monitors`` entry point.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any, ClassVar

from courier.interfaces.data_monitors import DataMonitorBasePlugin
from courier.plugins.data_monitors.s3_poller import S3Poller
from courier.types.datum import Datum

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


class Ticker(DataMonitorBasePlugin):  # type: ignore[misc,unused-ignore]
    """Config: ``interval_seconds`` (default 300) and ``hostname`` (for the
    job builder's filter). Each datum's ``file`` is ``tick-<UTC time>``."""

    interface: ClassVar[str] = "data_monitors"
    family: ClassVar[str] = "standard"
    name: ClassVar[str] = "ticker"
    version: ClassVar[str] = "0.1.0"

    def __init__(self, service: Any, config: dict[str, Any] | None = None,
                 identifier: str | None = None) -> None:  # fmt: skip
        super().__init__(service, config, identifier=identifier)
        cfg = config or {}
        self.interval = float(cfg.get("interval_seconds", 300))
        if self.interval <= 0:
            raise ValueError("interval_seconds must be positive")
        self.hostname = str(cfg.get("hostname", "ticker"))
        self.health = False
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        super().stop()

    def is_healthy(self) -> bool:
        return self.health

    def find_file(self) -> Generator[Datum[Any], None, None]:
        self.health = True
        try:
            while not self._stop_event.is_set():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                yield Datum(file=f"tick-{stamp}", hostname=self.hostname)
                if self._stop_event.wait(self.interval):
                    return
        finally:
            self.health = False
