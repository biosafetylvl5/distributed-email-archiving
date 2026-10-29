from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dea_capture import summary
from dea_capture.store import LocalStore, S3Store

SHA = "a" * 64
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@dataclass
class CountingStore(LocalStore):
    reads: list[str] = field(default_factory=list)

    def metadata(self, key: str) -> dict[str, str] | None:
        self.reads.append(key)
        return super().metadata(key)


def seed(store: LocalStore) -> None:
    store.put(f"incoming/archive/2026-10-25/{SHA}.eml", b"x" * 10, "message/rfc822",
              metadata={"user": "ann", "tag": "news", "envelope_from": "n@example.com",
                        "rcpt_to": "ann+news@example.org"})  # fmt: skip
    store.put("incoming/quarantine/2026-10-26/" + "b" * 64 + ".eml", b"y", "message/rfc822",
              metadata={"user": "bob"})  # fmt: skip
    store.put("incoming/archive/junk.txt", b"z", "text/plain")
    store.put(f"issue-status/{SHA}.json", b"{}", "application/json",
              metadata={"state": "done", "assets_ok": "3"})  # fmt: skip
    for i, (kind, status) in enumerate([("page", "ok"), ("page", "queued"), ("media", "ok")]):
        meta = {"kind": kind, "status": status, "attempts": "1",
                "updated_at": f"2026-10-27T0{i}:00:00+00:00"}  # fmt: skip
        if i == 0:
            meta["wayback"] = "20260101000000"
        store.put(f"captures/index/{i:064x}.json", b"{}", "application/json", metadata=meta)


def load(store: LocalStore) -> dict[str, Any]:
    got: dict[str, Any] = json.loads(store.get(summary.SUMMARY_KEY) or b"")
    return got


def test_builds_the_summary(tmp_path: Path) -> None:
    store = CountingStore(tmp_path)
    seed(store)
    got = summary.build(store, now=lambda: NOW, workers=2)
    assert got == {"incoming": 2, "statuses": 1, "urls": 3,
                   "metadata_read": {"incoming": 2, "statuses": 1, "index": 3}}  # fmt: skip
    s = load(store)
    assert s["generated_at"] == "2026-10-27T12:00:00+00:00"
    first = s["incoming"][0]
    assert first["key"] == f"incoming/archive/2026-10-25/{SHA}.eml"
    assert first["size"] == 10
    # only what the dashboard shows: no recipient address
    assert first["customMetadata"] == {
        "user": "ann",
        "tag": "news",
        "envelope_from": "n@example.com",
    }
    assert s["statuses"] == [{"key": f"issue-status/{SHA}.json",
                              "customMetadata": {"state": "done", "assets_ok": "3"}}]  # fmt: skip
    assert s["urls"] == {"total": 3, "byKind": {"page": {"ok": 1, "queued": 1}, "media": {"ok": 1}},
                         "wayback": 1, "lastUpdated": "2026-10-27T02:00:00+00:00"}  # fmt: skip


def test_reads_only_what_changed_and_drops_deleted(tmp_path: Path) -> None:
    store = CountingStore(tmp_path)
    seed(store)
    summary.build(store, now=lambda: NOW, workers=2)
    store.reads.clear()
    assert summary.build(store, now=lambda: NOW, workers=2)["metadata_read"] == {
        "incoming": 0, "statuses": 0, "index": 0,
    }  # fmt: skip

    key = f"captures/index/{1:064x}.json"
    store.put(key, b'{"x": 1}', "application/json", metadata={"kind": "page", "status": "ok"})
    store.delete(f"captures/index/{2:064x}.json")
    summary.build(store, now=lambda: NOW, workers=2)
    assert store.reads == [key]
    assert load(store)["urls"]["byKind"] == {"page": {"ok": 2}}
    state = json.loads(store.get(summary.STATE_KEY) or b"")
    assert len(state["index"]) == 2


def test_starts_over_on_a_bad_state(tmp_path: Path) -> None:
    store = CountingStore(tmp_path)
    seed(store)
    store.put(summary.STATE_KEY, b"not json", "application/json")
    assert summary.build(store, now=lambda: NOW, workers=2)["metadata_read"]["index"] == 3


@dataclass
class FakeS3:
    pages: list[list[dict[str, Any]]]
    heads: dict[str, dict[str, str]]
    exceptions: Any = None

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        pages = self.pages

        class P:
            def paginate(self, **kw: Any) -> list[dict[str, Any]]:
                return [{"Contents": p} for p in pages]

        return P()

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        if Key not in self.heads:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": self.heads[Key]}


def test_s3_listing_and_metadata() -> None:
    pytest.importorskip("botocore")
    when = datetime(2026, 10, 27, tzinfo=UTC)
    client = FakeS3(
        pages=[[{"Key": "a", "Size": 1, "ETag": '"e1"', "LastModified": when}],
               [{"Key": "b", "Size": 2, "ETag": '"e2"', "LastModified": when}]],
        heads={"a": {"user": "ann"}},
    )  # fmt: skip
    store = S3Store("bucket", client=client)
    assert [(o.key, o.size, o.etag) for o in store.listing("")] == [
        ("a", 1, '"e1"'),
        ("b", 2, '"e2"'),
    ]
    assert store.metadata("a") == {"user": "ann"}
    assert store.metadata("b") is None


def test_ticker_ticks_and_reports_a_dead_thread() -> None:
    pytest.importorskip("courier")
    import time
    from types import SimpleNamespace

    from courier.config import ServiceConfig

    from dea_capture.courier_plugin import Ticker

    t = Ticker(SimpleNamespace(config=ServiceConfig()), {"interval_seconds": 0.05})
    sent: list[str] = []
    t.add_metadata_to_file = lambda file: file  # type: ignore[method-assign]
    t.emit = lambda d, *a, **k: sent.append(d.file)  # type: ignore[method-assign]

    def wait_for(n: int) -> None:
        deadline = time.monotonic() + 5
        while len(sent) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sent) >= n

    t.start()
    wait_for(2)
    assert t.is_healthy() and sent[0].startswith("tick-")
    t.stop()
    assert not t.is_healthy()
    # courier restarts a plugin on the same object
    t.start()
    wait_for(len(sent) + 1)
    assert t.is_healthy()
    # a thread that has died is unhealthy, whatever its flag says
    assert t._main_thread is not None
    t._stop_event.set()
    t._main_thread.join(2)
    t.health = True
    assert not t.is_healthy()
