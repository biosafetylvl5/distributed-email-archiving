"""Liveness for the courier service, read from courier's own metrics.

Courier marks a crashed plugin FAILED and never restarts it, while the
process carries on looking healthy. The S3 poller stamps
``courier_data_monitor_last_scan_timestamp_seconds`` after every scan, so a
stale or missing stamp means the poller is gone and the pod should restart.
"""

from __future__ import annotations

import re
import time
import urllib.request

SCAN_METRIC = "courier_data_monitor_last_scan_timestamp_seconds"
_SAMPLE = re.compile(rf"^{SCAN_METRIC}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)\s*$", re.MULTILINE)


def last_scan(metrics_text: str) -> float | None:
    """The oldest last-scan stamp across monitors: every one must be fresh."""
    stamps = [float(m.group(1)) for m in _SAMPLE.finditer(metrics_text)]
    stamps = [s for s in stamps if s > 0]
    return min(stamps) if stamps else None


def check(metrics_text: str, max_age: float, now: float | None = None) -> tuple[bool, str]:
    stamp = last_scan(metrics_text)
    if stamp is None:
        return False, f"no {SCAN_METRIC} sample yet"
    age = (now if now is not None else time.time()) - stamp
    if age > max_age:
        return False, f"last scan {age:.0f}s ago (limit {max_age:.0f}s)"
    return True, f"last scan {age:.0f}s ago"


def fetch_metrics(url: str, timeout: float = 5.0) -> str:
    # courier's own Prometheus endpoint on localhost; not archived content
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        text: str = resp.read().decode()
    return text
