from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dea_capture import cli, health, summary
from dea_capture.links import Relinked
from dea_capture.store import LocalStore


def test_environment_dry_run_accepts_local_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "message.eml"
    source.write_bytes(b"From: writer@example.com\n\nbody\n")
    monkeypatch.setenv("DEA_MODE", "dry-run")
    assert cli.main(["issue", str(source)]) == 0
    env_output = capsys.readouterr().out
    monkeypatch.delenv("DEA_MODE")
    assert cli.main(["issue", str(source), "--dry-run"]) == 0
    assert capsys.readouterr().out == env_output
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("fresh", [True, False])
def test_relink_checks_cached_status_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh: bool,
) -> None:
    class BucketStore(LocalStore):
        bucket = "bucket"

    store = BucketStore(tmp_path)
    key = "issue-status/" + "a" * 64 + ".json"
    source = "incoming/archive/2026-10-27/" + "a" * 64 + ".eml"
    store.put(source, b"raw", "message/rfc822")
    store.put(key, b"{}", "application/json", metadata={"links_unresolved": "1", "source": source})
    listed = store.listing("issue-status/")[0]
    identity = [listed.etag, listed.modified.isoformat()] if fresh else ["old", "old"]
    cached = {"statuses": {key: [identity, {"links_unresolved": "0"}]}}
    store.put(summary.STATE_KEY, json.dumps(cached).encode(), "application/json")
    monkeypatch.setattr(cli, "S3Store", lambda bucket: store)
    tried: list[bytes] = []

    def relink(raw: bytes, deadline: float) -> Relinked:
        tried.append(raw)
        return Relinked()

    monkeypatch.setattr(cli, "_relinker", lambda *args: relink)
    assert cli.main(["relink", "s3://bucket"]) == 0
    assert tried == ([] if fresh else [b"raw"])


def test_healthcheck_passes_expected_monitor_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "fetch_metrics", lambda url: "")
    seen: dict[str, Any] = {}

    def check(text: str, age: float, **kwargs: Any) -> tuple[bool, str]:
        seen.update(kwargs)
        return True, "ok"

    monkeypatch.setattr(health, "check", check)
    assert cli.main(["healthcheck", "--expected-monitors", "3", "--startup-grace", "10"]) == 0
    assert seen == {"expected_monitors": 3, "startup_grace": 10}
