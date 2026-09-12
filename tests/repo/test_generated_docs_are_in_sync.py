"""Tests ensuring generated documentation tables match model definitions."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "gen_service_config_table.py"


def test_the_service_config_table_matches_model_fields():
    result = subprocess.run(
        [sys.executable, str(TOOL), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"docs/api-reference.md is stale:\n{result.stderr}\n"
        "Run: python3 tools/gen_service_config_table.py"
    )


def test_CONTROL_the_checker_can_fail(tmp_path):
    """Verify table drift detector fails when doc table is missing rows."""
    doc = REPO / "docs" / "api-reference.md"
    copy = tmp_path / "api-reference.md"
    lines = doc.read_text().splitlines(keepends=True)
    row = next(i for i, line in enumerate(lines) if line.startswith("| `name` |"))
    copy.write_text("".join(lines[:row] + lines[row + 1 :]))

    result = subprocess.run(
        [sys.executable, str(TOOL), "--check", "--doc", str(copy)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, "the checker passed a doc with a row removed"
    assert doc.read_text() == "".join(lines), "the control touched the tracked doc"
