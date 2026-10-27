"""Golden inventories: the full dry-run output for each fixture, checked in so
a classifier change shows up as a reviewable diff.

Regenerate with ``UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`` and
read the diff before committing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dea_capture.cli import main
from dea_capture.inventory import inventory

from .conftest import EML_FIXTURES


@pytest.mark.parametrize("path", EML_FIXTURES, ids=lambda p: p.stem)
def test_golden_inventory(path: Path) -> None:
    golden = path.with_suffix(".inventory.json")
    actual = json.dumps(inventory(path.read_bytes()).to_json(), indent=2) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(actual)
    assert golden.read_text() == actual


def test_cli_summary_has_no_urls(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inventory", "--summary", "--total", *map(str, EML_FIXTURES)]) == 0
    out = capsys.readouterr().out
    assert "http" not in out
    totals = json.loads(out)
    assert totals["files"] == len(EML_FIXTURES)
    assert totals["by_decision.skip"] > 0
