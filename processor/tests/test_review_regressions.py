"""Regressions for the merged review fixes, using only synthetic input."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from dea_capture import capture, classify, gallery, issue, links, media
from dea_capture.fetch import HostPacer
from dea_capture.guard import GuardedFetcher
from dea_capture.inventory import inventory
from dea_capture.store import LocalStore, put_json

from .conftest import fake_resolver
from .test_issue import FakeCDN, _manifest, _run
from .test_store_health import _s3


@pytest.mark.parametrize("scrub", [media.safe_info, gallery.scrub])
@pytest.mark.parametrize(
    "url", ["https://example.org/SECRET@example.org", "https://example.org:bad/post"]
)
def test_extractor_metadata_drops_uncanonicalizable_urls(scrub: Any, url: str) -> None:
    assert scrub({"url": url, "nested": [url, {"url": url, "title": "ok"}]}) == {
        "nested": [{"title": "ok"}]
    }


def test_media_size_uses_one_stat_and_ignores_vanished_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present, vanished = tmp_path / "media.mp4", tmp_path / "media.part"
    calls: dict[Path, int] = {}

    def stat(path: Path, **kwargs: Any) -> os.stat_result:
        calls[path] = calls.get(path, 0) + 1
        if path == vanished or calls[path] > 1:
            raise FileNotFoundError(path)
        return os.stat_result((0o100600, 0, 0, 1, 0, 0, 7, 0, 0, 0))

    monkeypatch.setattr(Path, "rglob", lambda *a: iter([present, vanished]))
    monkeypatch.setattr(Path, "stat", stat)
    assert media._size(tmp_path) == 7
    assert calls == {present: 1, vanished: 1}


def test_capture_update_bounds_conditional_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalStore(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    url = "https://example.org/post"
    capture.queue(store, url, "page", now)
    attempts = 0

    def conflict(*args: Any, **kwargs: Any) -> bool:
        nonlocal attempts
        attempts += 1
        assert attempts <= 5, "conditional writes retried without a bound"
        return False

    monkeypatch.setattr(capture, "save", conflict)
    with pytest.raises(RuntimeError, match=r"conditional write.*5 attempts"):
        capture.update(store, capture.url_hash(url), lambda rec: True)
    assert attempts == 5


@pytest.mark.parametrize("code", ["AccessDenied", "InternalError"])
def test_s3_conditional_write_propagates_non_conflicts(code: str) -> None:
    store = _s3()
    with Stubber(store.client) as stub:
        stub.add_client_error("put_object", code, http_status_code=500)
        with pytest.raises(ClientError, match=code):
            store.put("k", b"x", "text/plain", if_match='"etag"')


@pytest.mark.parametrize("suffix", ["", "@invalid"])
def test_classify_mailbox_detection_is_linear(suffix: str) -> None:
    code = (
        "from dea_capture.classify import has_email_path; "
        f"assert not has_email_path('https://example.org/' + 'a' * 100000 + {suffix!r})"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=3)


@pytest.mark.parametrize(
    "path", ["SECRET@example.org", "a@SECRET@example.org", "SECRET@example.org@b"]
)
@pytest.mark.parametrize("passes", range(4))
def test_classify_still_detects_mailboxes(path: str, passes: int) -> None:
    from urllib.parse import quote

    for _ in range(passes):
        path = quote(path, safe="")
    with pytest.raises(ValueError):
        classify.canonicalize("https://example.org/" + path)


def test_classify_refuses_excessive_percent_nesting(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "%" + "25" * 1000 + "40"
    calls = 0

    def counted(value: str) -> str:
        nonlocal calls
        calls += 1
        assert calls <= 3, "unbounded percent decoding"
        return unquote(value)

    monkeypatch.setattr(classify, "unquote", counted)
    with pytest.raises(ValueError):
        classify.canonicalize("https://example.org/" + value)
    assert calls == 3


@pytest.mark.parametrize("always_conflict", [False, True])
def test_relink_conditional_merge_preserves_a_concurrent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, always_conflict: bool
) -> None:
    href = "https://click.example.com/SECRET"
    msg = EmailMessage()
    msg.set_content(f'<a href="{href}">Read</a>', subtype="html")
    inv = inventory(msg.as_bytes())
    store = LocalStore(tmp_path)
    key = links.captures_key("synthetic")
    failed = {"href": href, "decision": "resolve", "outcome": "failed"}
    resolved = {**failed, "outcome": "ok", "canonical": "https://example.org/post"}
    put_json(store, key, {"links": [failed]})
    original_put = store.put
    attempts = 0

    def put(
        key: str,
        data: bytes,
        content_type: str,
        *,
        if_absent: bool = False,
        if_match: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> bool:
        nonlocal attempts
        attempts += 1
        assert attempts <= 5, "conditional writes retried without a bound"
        if attempts == 1:
            original_put(key, json.dumps({"links": [resolved]}).encode(), content_type)
        if always_conflict:
            return False
        return original_put(
            key, data, content_type, if_absent=if_absent, if_match=if_match, metadata=metadata
        )

    monkeypatch.setattr(store, "put", put)
    monkeypatch.setattr(
        links, "_link_record", lambda *a: links.LinkRecord(href, "resolve", "failed")
    )

    def retry() -> dict[str, Any] | None:
        doc, _ = links.retry_unresolved(
            "synthetic",
            inv,
            store,
            GuardedFetcher(FakeCDN(), resolve=fake_resolver),
            now=datetime(2026, 1, 1, tzinfo=UTC),
            pacer=HostPacer(0),
        )
        return doc

    if always_conflict:
        with pytest.raises(RuntimeError, match=r"conditional write.*5 attempts"):
            retry()
        assert attempts == 5
    else:
        doc = retry()
        assert attempts == 2
        assert doc and doc["links"] == [resolved]
    assert links.load_links(store, "synthetic")["links"] == [resolved]  # type: ignore[index]


def test_issue_reuses_assets_before_applying_original_fetch_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = EmailMessage()
    msg.set_content(
        "".join(f'<img src="https://example.org/{i}.png">' for i in range(5)), subtype="html"
    )
    raw = msg.as_bytes()
    store = LocalStore(tmp_path)
    first = FakeCDN(missing={"https://example.org/0.png", "https://example.org/4.png"})
    _run(raw, store, first)
    old = _manifest(store, raw)
    old["extract_version"] = 0
    put_json(store, issue.manifest_key(old["sha256"]), old)
    monkeypatch.setattr(issue, "MAX_ORIGINAL_EMBEDS", 1)
    cdn = FakeCDN()
    _run(raw, store, cdn)
    manifest = _manifest(store, raw)
    assert cdn.requested == ["https://example.org/0.png"]
    assert [a["status"] for a in manifest["assets"]] == ["ok", "ok", "ok", "ok", "deferred:budget"]
    assert manifest["counts"]["reused"] == 3
