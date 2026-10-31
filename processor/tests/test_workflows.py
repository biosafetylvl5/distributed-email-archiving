"""CI workflows pin third-party actions to commit SHAs."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_workflow_actions_are_commit_pinned() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text().splitlines():
            if "uses:" in line:
                assert re.fullmatch(r"\s*(?:- )?uses: [\w./-]+@[0-9a-f]{40} # v\S+", line), (
                    path,
                    line,
                )
