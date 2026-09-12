"""Tests verifying deprecated validated_rpc decorator is not referenced in active code or docs."""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (REPO / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


def test_the_name_appears_nowhere_in_code_or_docs():
    out = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "grep",
            "-n",
            "validated_rpc",
            "--",
            "src",
            "packages",
            "examples",
            "*.md",
            "docs",
            # Exempt historical design specifications.
            ":!docs/superpowers",
        ],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    allowed = ("CHANGELOG.md",)
    hits = [line for line in out if not line.startswith(allowed)]
    assert not hits, "validated_rpc still named:\n  " + "\n  ".join(hits)


def test_CONTROL_the_sweep_can_see_a_hit(tmp_path):
    """Verify search mechanism detects target pattern when present."""
    (tmp_path / "x.py").write_text("@validated_rpc\n")
    out = subprocess.run(
        ["grep", "-rn", "validated_rpc", str(tmp_path)], capture_output=True, text=True
    ).stdout
    assert "x.py" in out
