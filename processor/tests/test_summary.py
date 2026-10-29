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


ARCHIVED = f"incoming/archive/2026-10-25/{SHA}.eml"


def seed(store: LocalStore) -> None:
    store.put(f"incoming/archive/2026-10-25/{SHA}.eml", b"x" * 10, "message/rfc822",
              metadata={"user": "ann", "tag": "news", "envelope_from": "n@example.com",
                        "rcpt_to": "ann+news@example.org"})  # fmt: skip
    store.put("incoming/quarantine/2026-10-26/" + "b" * 64 + ".eml", b"y", "message/rfc822",
              metadata={"user": "bob"})  # fmt: skip
    store.put("incoming/archive/junk.txt", b"z", "text/plain")
    store.put(f"issue-status/{SHA}.json", b"{}", "application/json",
              metadata={"state": "done", "assets_ok": "3", "assets_failed": "0",
                        "captures_new": "2", "extract_version": "1",
                        "source": f"s3://bucket/{ARCHIVED}"})  # fmt: skip
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
                   "metadata_read": {"incoming": 2, "statuses": 1, "index": 3},
                   "storage_listed": True}  # fmt: skip
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
    # only what the dashboard reads, zeros left out, the source as a key
    assert s["statuses"] == [
        {
            "key": f"issue-status/{SHA}.json",
            "customMetadata": {
                "source": f"incoming/archive/2026-10-25/{SHA}.eml",
                "state": "done",
                "assets_ok": "3",
            },
        }
    ]
    assert b"\n" not in (store.get(summary.SUMMARY_KEY) or b"")  # compact
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


def test_carries_the_retry_requests_and_results(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.put(
        summary.RETRY_REQUESTS + "20261029T100000Z-0123456789abcdef.json", b"{}", "application/json"
    )
    store.put(summary.RETRY_RESULTS, b'[{"scope": "urls", "rescheduled": 2}]', "application/json")
    summary.build(store, now=lambda: NOW, workers=2)
    doc = json.loads(store.get(summary.SUMMARY_KEY) or b"")
    assert doc["retries"] == {"waiting": 1, "recent": [{"scope": "urls", "rescheduled": 2}]}


def test_storage_by_area_kind_and_message(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    seed(store)
    h0 = f"{0:064x}"
    store.put(f"captures/{h0[:2]}/{h0}/20261027T000000Z/page.wacz", b"w" * 100, "application/zip")
    store.put(f"captures/ff/{'f' * 64}/20261027T000000Z/x", b"q" * 7, "text/plain")
    store.put(f"issues/{SHA}/assets/{'c' * 64}.png", b"p" * 20, "image/png")
    store.put(f"issues/{SHA}/view/index.html", b"v" * 5, "text/html")
    store.put(f"issues/{SHA}/links.json", b"l" * 3, "application/json")
    store.put(f"captures/due/page/20261027T000000Z-{h0}.json", b"{}", "application/json")
    got = summary.build(store, now=lambda: NOW, workers=2)
    assert got["storage_listed"] is True
    st = load(store)["storage"]
    assert st["computed_at"] == "2026-10-27T12:00:00+00:00"
    a = st["areas"]
    assert a["archive"] == {"count": 2, "bytes": 11}  # incl. junk.txt
    assert a["quarantine"] == {"count": 1, "bytes": 1}
    assert (a["embeds"], a["views"]) == ({"count": 1, "bytes": 20}, {"count": 1, "bytes": 5})
    assert a["link_records"]["count"] == 2  # links.json and the status record
    assert a["captures"] == {"count": 2, "bytes": 107} and a["queue"]["count"] == 1
    assert st["captures"] == {"page": {"count": 1, "bytes": 100, "urls": 1},
                              "unknown": {"count": 1, "bytes": 7, "urls": 1}}  # fmt: skip
    assert st["messages"] == {SHA: 28}

    # within the hour it's carried over, not listed again
    store.put(f"issues/{SHA}/view/meta.json", b"m", "application/json")
    later = summary.build(store, now=lambda: NOW + summary.STORAGE_EVERY / 2, workers=2)
    assert later["storage_listed"] is False and load(store)["storage"]["messages"] == {SHA: 28}
    later = summary.build(store, now=lambda: NOW + summary.STORAGE_EVERY, workers=2)
    assert later["storage_listed"] is True and load(store)["storage"]["messages"] == {SHA: 29}


def test_largest_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary, "LARGEST", 3)
    store = LocalStore(tmp_path)
    seed(store)  # captures/index/<0>.json is a page, with no body worth reading
    h = "e" * 64
    store.put(f"captures/index/{h}.json", json.dumps({"canonical": "https://video.example.com/v/1"}).encode(),
              "application/json", metadata={"kind": "media", "status": "ok"})  # fmt: skip
    store.put(f"captures/ee/{h}/20261027T000000Z/media.mp4", b"m" * 500, "video/mp4")
    store.put(f"captures/ee/{h}/20261027T000000Z/info.json", b"i" * 2, "application/json")
    store.put(f"issues/{SHA}/assets/{'c' * 64}.png", b"p" * 300, "image/png")
    store.put(f"captures/ff/{'f' * 64}/20261027T000000Z/page.wacz", b"w" * 200, "application/zip")
    summary.build(store, now=lambda: NOW, workers=2)
    got = json.loads(store.get(summary.LARGEST_KEY) or b"")
    assert got["computed_at"] == "2026-10-27T12:00:00+00:00"
    files = got["files"]
    assert [(f["size"], f["area"]) for f in files] == [
        (500, "captures"),
        (300, "embeds"),
        (200, "captures"),
    ]
    assert files[0]["kind"] == "media" and files[0]["url"] == "https://video.example.com/v/1"
    assert files[1]["sha"] == SHA and "url" not in files[1]
    assert files[2]["kind"] == "unknown" and "url" not in files[2]
    # the summary itself carries no URLs
    assert "video.example.com" not in (store.get(summary.SUMMARY_KEY) or b"").decode()
