"""Changelog entries are fragments that merge without conflict and assemble at release.

Each entry is a file under `changelog.d/`. Two branches adding entries touch
different files, so they do not conflict the way two entries added at the top
of CHANGELOG.md's Unreleased block do. `scripts/assemble_changelog.py`
turns the fragments into a release section in the release-prep pull request.

The git-backed tests build their own repository under tmp_path, so what they
assert about merging and ordering is measured on commits they made, not on
whatever this repository's history happens to hold.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# Loaded from its file, as test_release_note.py loads its script: `scripts/` is
# not a package, and an import of `scripts.<name>` resolves only when pytest
# happens to run from the repository root.
_spec = importlib.util.spec_from_file_location(
    "assemble_changelog", REPO / "scripts" / "assemble_changelog.py"
)
assert _spec is not None and _spec.loader is not None
assemble_changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assemble_changelog)

FLAG = assemble_changelog.FLAG
SLUG = assemble_changelog.SLUG
AssemblyError = assemble_changelog.AssemblyError
assemble = assemble_changelog.assemble
fragment_paths = assemble_changelog.fragment_paths
fragment_problem = assemble_changelog.fragment_problem
main = assemble_changelog.main

HEADER = f"# Changelog\n\nWhat an upgrader needs, newest first.\n\n{FLAG}\n\n"
RELEASED = "## 1.0.0\n\n- Initial release.\n"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def commit_file(repo: Path, rel: str, text: str, message: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit_file(
        tmp_path,
        "CHANGELOG.md",
        HEADER + "## Unreleased\n- An entry written before fragments.\n\n" + RELEASED,
        "base",
    )
    return tmp_path


# --- merging -----------------------------------------------------------------


def test_two_branches_adding_fragments_merge_without_conflict(repo: Path):
    git(repo, "branch", "one")
    git(repo, "branch", "two")
    git(repo, "checkout", "-q", "one")
    commit_file(repo, "changelog.d/first-change.md", "- First.\n", "one")
    git(repo, "checkout", "-q", "two")
    commit_file(repo, "changelog.d/second-change.md", "- Second.\n", "two")

    result = subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", "one", "two"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout


def test_a_held_branch_merges_after_main_gained_a_different_fragment(repo: Path):
    """The case a held branch is in: cut from main, its fragment added, while main
    merged another pull request's fragment."""
    git(repo, "branch", "held")
    commit_file(repo, "changelog.d/landed-first.md", "- Landed first.\n", "main gains one")
    git(repo, "checkout", "-q", "held")
    commit_file(repo, "changelog.d/held-change.md", "- Held.\n", "held adds one")

    result = subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", "main", "held"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout


def test_CONTROL_two_branches_adding_to_the_unreleased_block_conflict(repo: Path):
    """What the fragments replace, measured the same way, so the merge checks
    above can fail: two entries added at the top of the block conflict."""
    text = (repo / "CHANGELOG.md").read_text()
    git(repo, "branch", "one")
    git(repo, "branch", "two")
    for branch, entry in (("one", "- From one.\n"), ("two", "- From two.\n")):
        git(repo, "checkout", "-q", branch)
        commit_file(
            repo, "CHANGELOG.md", text.replace("## Unreleased\n", "## Unreleased\n" + entry), branch
        )

    result = subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", "--name-only", "one", "two"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stdout
    assert "CONFLICT (content): Merge conflict in CHANGELOG.md" in result.stdout


# --- assembly ----------------------------------------------------------------


def test_assembly_writes_the_release_section_newest_first_and_folds_in_unreleased(repo: Path):
    commit_file(repo, "changelog.d/oldest-change.md", "- Oldest.\n", "a")
    commit_file(
        repo,
        "changelog.d/middle-change.md",
        "- **API Change**: middle,\n  on two lines.\n",
        "b",
    )
    git(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "changelog.d/merged-from-a-branch.md", "- From a branch.\n", "side")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    (repo / "changelog.d" / "not-yet-committed.md").write_text("- Uncommitted.\n")
    commit_file(repo, "changelog.d/README.md", "# Fragments\n", "readme")

    assert main(["1.1.0", "--repo", str(repo)]) == 0

    expected = HEADER + (
        "## 1.1.0\n"
        "- Uncommitted.\n"
        "- From a branch.\n"
        "- **API Change**: middle,\n  on two lines.\n"
        "- Oldest.\n"
        "- An entry written before fragments.\n"
        "\n" + RELEASED
    )
    assert (repo / "CHANGELOG.md").read_text() == expected
    assert fragment_paths(repo) == []
    assert (repo / "changelog.d" / "README.md").exists()


def test_a_second_assembly_finds_no_unreleased_block_and_inserts_above_the_last(repo: Path):
    commit_file(repo, "changelog.d/first-change.md", "- First.\n", "a")
    assert main(["1.1.0", "--repo", str(repo)]) == 0
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "release 1.1.0")
    commit_file(repo, "changelog.d/second-change.md", "- Second.\n", "b")

    assert main(["1.2.0", "--repo", str(repo)]) == 0

    text = (repo / "CHANGELOG.md").read_text()
    assert text.startswith(HEADER + "## 1.2.0\n- Second.\n\n## 1.1.0\n- First.\n")
    assert "## Unreleased" not in text


def test_a_name_reused_after_a_release_is_ordered_by_its_new_add(repo: Path):
    """Assembly deletes the fragments, so a later change can add a file of the same
    name. It is the newest entry, not the oldest."""
    commit_file(repo, "changelog.d/reused-name.md", "- Before the release.\n", "a")
    assert main(["1.1.0", "--repo", str(repo)]) == 0
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "release 1.1.0")
    commit_file(repo, "changelog.d/in-between.md", "- In between.\n", "b")
    commit_file(repo, "changelog.d/reused-name.md", "- After the release.\n", "c")

    assert main(["1.2.0", "--repo", str(repo)]) == 0

    text = (repo / "CHANGELOG.md").read_text()
    assert "## 1.2.0\n- After the release.\n- In between.\n" in text


def test_dry_run_prints_the_result_and_changes_nothing(repo: Path, capsys):
    commit_file(repo, "changelog.d/first-change.md", "- First.\n", "a")
    before = (repo / "CHANGELOG.md").read_text()

    assert main(["1.1.0", "--repo", str(repo), "--dry-run"]) == 0

    assert "## 1.1.0\n- First.\n" in capsys.readouterr().out
    assert (repo / "CHANGELOG.md").read_text() == before
    assert len(fragment_paths(repo)) == 1


@pytest.mark.parametrize(
    ("name", "text", "says"),
    [
        ("a-fix-for-1234.md", "- Entry.\n", "no digits"),
        ("Capitalised.md", "- Entry.\n", "lower-case"),
        ("two-bullets.md", "- One.\n- Two.\n", "exactly one bullet"),
        ("not-a-bullet.md", "Entry.\n", "starts with '- '"),
    ],
)
def test_a_malformed_fragment_stops_the_assembly_by_name(repo: Path, capsys, name, text, says):
    commit_file(repo, f"changelog.d/{name}", text, "bad")
    before = (repo / "CHANGELOG.md").read_text()

    assert main(["1.1.0", "--repo", str(repo)]) == 1

    err = capsys.readouterr().err
    assert name in err and says in err, err
    assert (repo / "CHANGELOG.md").read_text() == before


def test_a_version_that_already_has_a_section_is_refused():
    with pytest.raises(AssemblyError, match="already has a '## 1.0.0' section"):
        assemble(HEADER + RELEASED, "1.0.0", ["- New.\n"])


def test_nothing_to_assemble_is_refused():
    with pytest.raises(AssemblyError, match="no fragments and no Unreleased entries"):
        assemble(HEADER + RELEASED, "1.1.0", [])


def test_a_changelog_without_the_flag_is_refused():
    with pytest.raises(AssemblyError, match="version list"):
        assemble("# Changelog\n\n" + RELEASED, "1.1.0", ["- New.\n"])


# --- the real tree -----------------------------------------------------------


def test_the_repository_changelog_carries_the_flag_the_assembly_inserts_under():
    assert FLAG in (REPO / "CHANGELOG.md").read_text()


def test_every_fragment_in_the_tree_is_one_bullet_with_a_plain_name():
    problems = [p for p in map(fragment_problem, fragment_paths(REPO)) if p]

    assert problems == [], problems


def test_CONTROL_the_fragment_rules_reject_what_they_describe(tmp_path: Path):
    """The tree check above passes on an empty directory, so the rules it applies
    are shown refusing a bad fragment here."""
    bad = tmp_path / "fix-1234.md"
    bad.write_text("- Entry.\n")
    good = tmp_path / "a-plain-topic.md"
    good.write_text("- Entry.\n")

    assert fragment_problem(bad) is not None
    assert fragment_problem(good) is None
    assert SLUG.fullmatch("a-plain-topic.md")
