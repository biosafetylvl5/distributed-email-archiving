from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dea_capture.guard import IPAddress, Response

FIXTURES = Path(__file__).parent / "fixtures"
EML_FIXTURES = sorted(FIXTURES.glob("*.eml"))

# every fixture URL that must never be requested carries one of these
FORBIDDEN_MARKERS = ("SECRET", "FORBIDDEN")

PUBLIC_IP = "93.184.215.14"
PRIVATE_HOSTS = {"intranet.example.com": ["10.1.2.3"]}


def fake_resolver(host: str, port: int) -> list[str]:
    return PRIVATE_HOSTS.get(host.lower(), [PUBLIC_IP])


@dataclass
class RecordingFetcher:
    """Stands in for the network. Fails the test outright if asked for a URL
    the fixtures mark as forbidden, so a guard bug can't hide behind a
    swallowed exception."""

    requested: list[str] = field(default_factory=list)

    def get(self, url: str, *, addresses: Sequence[IPAddress]) -> Response:
        for marker in FORBIDDEN_MARKERS:
            if marker in url:
                pytest.fail(f"forbidden URL reached the fetcher: {url}")
        for addr in addresses:
            assert ipaddress.ip_address(addr).is_global, f"{url} -> {addr}"
        self.requested.append(url)
        return Response(url=url, status=200, headers={}, body=b"")


@pytest.fixture
def recording() -> RecordingFetcher:
    return RecordingFetcher()


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
