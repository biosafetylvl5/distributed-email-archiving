from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from dea_capture import health
from dea_capture.store import LocalStore, S3Store, ext_for, split_s3_uri


@pytest.mark.parametrize(
    ("ctype", "url", "ext"),
    [
        ("font/woff2; charset=binary", "https://cdn.example.net/f", ".woff2"),
        ("application/font-woff", "https://cdn.example.net/f", ".woff"),
        ("application/x-font-ttf", "https://cdn.example.net/f", ".ttf"),
        ("application/vnd.ms-fontobject", "https://cdn.example.net/f", ".eot"),
        # a generic type defers to the URL, if that names one
        ("application/octet-stream", "https://cdn.example.net/f.woff2?v=3", ".woff2"),
        ("application/octet-stream", "https://cdn.example.net/f", ".bin"),
        # a specific type wins over the URL
        ("image/png", "https://cdn.example.net/f.jpg", ".png"),
        ("image/jpeg", "https://cdn.example.net/f", ".jpg"),
        (None, "https://cdn.example.net/f.woff#x", ".woff"),
        (None, "https://cdn.example.net/f", ""),
    ],
)
def test_ext_for(ctype: str | None, url: str, ext: str) -> None:
    assert ext_for(ctype, url) == ext


def test_local_store_claims_once(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    assert s.put("a/b.json", b"1", "application/json", if_absent=True)
    assert not s.put("a/b.json", b"2", "application/json", if_absent=True)
    assert s.get("a/b.json") == b"1"
    assert s.put("a/b.json", b"3", "application/json")
    assert s.get("a/b.json") == b"3" and s.exists("a/b.json") and not s.exists("nope")


def test_local_store_stays_inside_its_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalStore(tmp_path / "root").put("../escape", b"", "text/plain")


def _s3() -> S3Store:
    client = boto3.client(
        "s3", region_name="auto", endpoint_url="https://s3.example.invalid",
        aws_access_key_id="x", aws_secret_access_key="y",
    )  # fmt: skip
    return S3Store("bucket", client)


def test_s3_conditional_put() -> None:
    s = _s3()
    with Stubber(s.client) as stub:
        expected = {
            "Bucket": "bucket", "Key": "k", "Body": b"d", "ContentType": "text/plain",
            "IfNoneMatch": "*",
        }  # fmt: skip
        stub.add_response("put_object", {}, expected)
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
        assert s.put("k", b"d", "text/plain", if_absent=True)
        assert not s.put("k", b"d", "text/plain", if_absent=True)


def test_s3_put_sends_metadata() -> None:
    s = _s3()
    with Stubber(s.client) as stub:
        expected = {
            "Bucket": "bucket", "Key": "k", "Body": b"d", "ContentType": "application/json",
            "Metadata": {"state": "done"},
        }  # fmt: skip
        stub.add_response("put_object", {}, expected)
        assert s.put("k", b"d", "application/json", metadata={"state": "done"})


def test_local_store_keeps_metadata_out_of_the_way(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put("a/b.json", b"1", "application/json", metadata={"state": "done"})
    assert s.metadata("a/b.json") == {"state": "done"}
    assert s.metadata("a/c.json") is None
    assert s.get("a/b.json") == b"1"


def test_s3_exists() -> None:
    s = _s3()
    with Stubber(s.client) as stub:
        stub.add_response("head_object", {}, {"Bucket": "bucket", "Key": "k"})
        stub.add_client_error("head_object", "404", http_status_code=404)
        assert s.exists("k")
        assert not s.exists("k")


def test_split_s3_uri() -> None:
    assert split_s3_uri("s3://b/incoming/archive/x.eml") == ("b", "incoming/archive/x.eml")
    for bad in ("s3://b/", "https://b/k", "s3:///k"):
        with pytest.raises(ValueError):
            split_s3_uri(bad)


METRICS = """\
# HELP courier_data_monitor_last_scan_timestamp_seconds Last scan
# TYPE courier_data_monitor_last_scan_timestamp_seconds gauge
courier_data_monitor_last_scan_timestamp_seconds{monitor_identifier="watch",monitor_name="s3_poller"} 1.7615e+09
courier_service_uptime_seconds 99.0
"""  # noqa: E501


def test_health_fresh_and_stale() -> None:
    assert health.check(METRICS, 900, now=1.7615e9 + 60) == (True, "last scan 60s ago")
    ok, why = health.check(METRICS, 900, now=1.7615e9 + 1000)
    assert not ok and "1000s" in why


def test_health_without_a_scan_is_unhealthy() -> None:
    ok, why = health.check("courier_service_uptime_seconds 5\n", 900, now=0)
    assert not ok and "no " in why
