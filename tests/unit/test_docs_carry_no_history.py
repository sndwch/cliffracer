"""Tests verifying documentation contains no historical or ticket narratives."""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (REPO / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


EXEMPT = {
    "CHANGELOG.md",
    "docs/decisions.md",
}

PATTERNS = {
    "an issue or PR number": re.compile(r"(?<![\w/])#\d{1,4}\b"),
    "a commit SHA": re.compile(r"\b(?:merged as|SHA)\s+`?[0-9a-f]{7,40}`?", re.I),
    "'used to' / 'no longer'": re.compile(
        r"\b(used to|no longer|previously|formerly|until 1\.0|until #\d+|"
        r"before 1\.0|before the fix|since 1\.0)\b",
        re.I,
    ),
    # Match subordinate clauses with named entities (e.g., 'before X existed').
    "'before X existed'": re.compile(
        r"\bbefore\s+[`\w.()]+\s+(existed|was added|was introduced|landed|shipped)\b",
        re.I,
    ),
    "a 1.x comparison": re.compile(r"\b1\.x\b"),
    "'was removed' / 'was replaced'": re.compile(
        r"\b(was|were|has been|have been)\s+(removed|deleted|dropped|renamed|replaced)\b",
        re.I,
    ),
    "rationale narration": re.compile(
        r"\b(we decided|the ruling|it was decided|the reason we)\b", re.I
    ),
    "release narration": re.compile(
        r"\b(4\.0 (deleted|removed|replaced|split)|0\.0\.70 removed|orphan sweep|the 1\.0 split)\b", re.I
    ),
}


def tracked_markdown() -> list[Path]:
    """Return tracked *.md files from git index, excluding exempt files."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / rel for rel in out if rel not in EXEMPT]


def history_lines(paths=None) -> list[str]:
    found = []
    for doc in paths if paths is not None else tracked_markdown():
        rel = doc.relative_to(REPO) if doc.is_relative_to(REPO) else doc
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    found.append(f"{rel}:{number} [{label}] {line.strip()[:100]}")
    return found


@pytest.mark.unit
def test_the_sweep_reads_the_documentation():
    """Verify tracked markdown files exist and are discovered."""
    docs = tracked_markdown()
    assert len(docs) >= 15, f"only found {len(docs)} tracked .md files: {docs}"
    assert all(d.exists() for d in docs), "git listed a file that is not on disk"


@pytest.mark.unit
def test_the_exemptions_all_exist():
    """Verify all exempt documentation files exist on disk."""
    missing = [rel for rel in EXEMPT if not (REPO / rel).exists()]
    assert not missing, f"exempt files that do not exist: {missing}"


@pytest.mark.unit
def test_no_document_narrates_its_own_history():
    found = history_lines()
    assert not found, (
        "documentation is for what the code does now. Move a removal to "
        "CHANGELOG.md, a still-binding decision to docs/decisions.md, and an "
        "old name a reader might search for to CHANGELOG.md:\n  " + "\n  ".join(found)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "This was fixed in #103.",
        "Merged as 7730342, so the behaviour is now correct.",
        "`HTTPMixin` no longer exists.",
        "The database package was removed.",
        "We decided to split the packages.",
        "0.0.70 removed the mixin hierarchy.",
        "Before `RejectMessage` existed, the message reached the handler.",
        "The decorators are unchanged from 1.x.",
    ],
)
def test_CONTROL_each_pattern_catches_its_own_shape(tmp_path: Path, line: str):
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert history_lines([doc]), f"not caught: {line!r}"


@pytest.mark.unit
def test_CONTROL_ordinary_present_tense_prose_passes():
    """Verify standard present-tense prose is not flagged."""
    doc = REPO / "docs" / "extensions.md"
    prose = (
        "Declare the extension as a class attribute. The attribute name is how "
        "you reach it and how its decorators are spelled. Build mutable state "
        "in setup(), not __init__."
    )
    tmp = REPO / ".pytest-history-control.md"
    try:
        tmp.write_text(prose + "\n")
        assert not history_lines([tmp]), "present-tense prose was flagged"
    finally:
        tmp.unlink(missing_ok=True)
    assert doc.exists()
