"""The deprecated validated_rpc decorator is named nowhere it could be believed.

The sweep is the whole tracked tree, with no path list. A path list is the wrong
shape for this: `git grep` accepts a pathspec matching nothing and reports no
hits, so renaming a directory shrinks the sweep to silence without touching the
check. Files that legitimately carry the name are listed below with the number
of times each does, so a resurrection inside one of them reds too.
"""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


# Files that name the decorator legitimately, with how many lines each does it
# on. The count is exact: a new occurrence in one of these reds, and clearing
# one means lowering the number.
ALLOWED: dict[str, tuple[int, str]] = {
    "tests/repo/test_no_validated_rpc.py": (
        14,
        "This guard names the decorator it searches for: in its docstring, its "
        "search term, its assertion messages, its allowlist and the sample its "
        "control plants.",
    ),
    "tests/unit/test_decorator_functionality.py": (
        1,
        "One docstring line contrasts the annotation with the decorator syntax "
        "it replaces, so the name appears as prose about the past API rather "
        "than a call.",
    ),
}


def find_validated_rpc_hits(repo_path: Path) -> list[str]:
    """Execute git grep for validated_rpc across every tracked file."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "grep", "-n", "validated_rpc"],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed with returncode {proc.returncode}: {proc.stderr}")
    return proc.stdout.splitlines()


def hits_by_file(repo_path: Path) -> dict[str, int]:
    """Count validated_rpc hits per tracked file."""
    counts: dict[str, int] = {}
    for hit in find_validated_rpc_hits(repo_path):
        rel = hit.split(":", 1)[0]
        counts[rel] = counts.get(rel, 0) + 1
    return counts


def test_the_name_appears_nowhere_in_code_or_docs():
    """Verify validated_rpc does not appear outside documented exemptions."""
    raw_hits = find_validated_rpc_hits(REPO)
    unauthorized = []
    for hit in raw_hits:
        rel_path = hit.split(":", 1)[0]
        if rel_path in ALLOWED:
            continue
        unauthorized.append(hit)
    assert not unauthorized, "validated_rpc still named:\n  " + "\n  ".join(unauthorized)


def test_the_allowlist_entries_are_documented_and_active():
    """Every allowlist entry exists, still names it, and names it exactly as recorded.

    Exempting a whole file would hide a genuine resurrection added to a file
    that already mentions the name, so the count is what is exempted.
    """
    assert ALLOWED, "the allowlist is empty; delete it and the branch that reads it"

    measured = hits_by_file(REPO)
    for rel_path, (count, reason) in sorted(ALLOWED.items()):
        assert isinstance(reason, str) and reason.strip(), (
            f"Missing reason for allowed hit: {rel_path}"
        )
        assert (REPO / rel_path).exists(), f"Allowed file does not exist: {rel_path}"
        actual = measured.get(rel_path, 0)
        assert actual == count, (
            f"{rel_path} names validated_rpc on {actual} line(s), the allowlist "
            f"records {count}. Lower the number when you remove one; a new "
            "mention needs a deliberate edit."
        )


def test_CONTROL_the_sweep_can_see_a_hit(tmp_path: Path):
    """Verify search mechanism detects target pattern using the real git grep sweep helper."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=tmp_path, check=True)

    test_file = tmp_path / "src" / "sample.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("@validated_rpc\ndef handler(): pass\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    hits = find_validated_rpc_hits(tmp_path)
    assert len(hits) == 1
    assert "src/sample.py:1:@validated_rpc" in hits[0]
