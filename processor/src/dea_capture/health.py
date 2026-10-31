"""Liveness for the courier service, read from courier's own metrics.

Courier marks a crashed plugin FAILED and never restarts it, while the
process carries on looking healthy. The S3 poller stamps
``courier_data_monitor_last_scan_timestamp_seconds`` after every scan, so a
stale or missing stamp means the poller is gone and the pod should restart.
"""

from __future__ import annotations

import math
import re
import time
import urllib.request

SCAN_METRIC = "courier_data_monitor_last_scan_timestamp_seconds"
_SAMPLE = re.compile(rf"^{SCAN_METRIC}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)\s*$", re.MULTILINE)


def last_scan(metrics_text: str) -> float | None:
    """The oldest last-scan stamp across monitors: every one must be fresh."""
    stamps = [float(m.group(1)) for m in _SAMPLE.finditer(metrics_text)]
    return min(stamps) if stamps and all(math.isfinite(s) and s > 0 for s in stamps) else None


def check(
    metrics_text: str,
    max_age: float,
    now: float | None = None,
    *,
    expected_monitors: int = 1,
    startup_grace: float = 0,
) -> tuple[bool, str]:
    samples = list(_SAMPLE.finditer(metrics_text))
    stamp = last_scan(metrics_text)
    if expected_monitors < 1:
        return False, "expected monitor count must be positive"
    if stamp is None or len(samples) < expected_monitors:
        uptime = re.search(
            r"^courier_service_uptime_seconds\s+([0-9.eE+-]+)\s*$", metrics_text, re.MULTILINE
        )
        if uptime and 0 <= float(uptime.group(1)) < startup_grace:
            return True, "waiting for scans during startup grace"
        return False, f"no fresh sample for every monitor ({len(samples)}/{expected_monitors})"
    age = (now if now is not None else time.time()) - stamp
    if not 0 <= age <= max_age:
        return False, f"last scan {age:.0f}s ago (limit {max_age:.0f}s)"
    return True, f"last scan {age:.0f}s ago"


def fetch_metrics(url: str, timeout: float = 5.0) -> str:
    # courier's own Prometheus endpoint on localhost; not archived content
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        text: str = resp.read().decode()
    return text
