"""A release note carries the changelog as it stood at the tag.

Changelog entries are fragments under `changelog.d/` until a release-prep pull
request assembles them into `CHANGELOG.md`. A release candidate is tagged with
its fragments still pending, so its note carries every fragment at the tag,
newest first, as assembly would order them. A final release is tagged after
assembly, with no fragments left, so its note carries CHANGELOG.md's section
for its version. Both are read from the tag itself, never from the working tree.

Every case builds a real repository, as `test_release_note.py` does.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


release_note = _load("release_note")
assemble_changelog = _load("assemble_changelog")

RC_LINE = "These are the changes pending since the last final release."


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, message: str, write: dict[str, str] | None = None, remove=()) -> None:
    for rel, text in (write or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
        _git(repo, "add", rel)
    for rel in remove:
        _git(repo, "rm", "-q", rel)
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """v1.0.0, then two fragments in two commits, tagged v1.1.0-rc.1."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    _commit(
        d,
        "chore: base",
        {"CHANGELOG.md": "# Changelog\n\n<!-- version list -->\n\n## 1.0.0\n- Old entry.\n"},
    )
    _git(d, "tag", "v1.0.0")
    _commit(d, "feat: first", {"changelog.d/first-change.md": "- First change.\n"})
    _commit(
        d,
        "fix: second",
        {"changelog.d/second-change.md": "- Second change,\n  over two lines.\n"},
    )
    _git(d, "tag", "v1.1.0-rc.1")
    return d


def test_an_rc_note_carries_its_pending_fragments_newest_first_above_the_commits(repo: Path):
    note = release_note.render("v1.0.0..v1.1.0-rc.1", repo=str(repo), fragments_at="v1.1.0-rc.1")

    assert note.startswith(
        f"## Changelog\n\n{RC_LINE}\n\n- Second change,\n  over two lines.\n- First change.\n"
    ), note
    assert note.index("## Changelog") < note.index("## Commits"), note


def test_the_note_orders_fragments_as_assembly_does(repo: Path):
    _git(repo, "checkout", "-q", "v1.1.0-rc.1")
    assembly = [
        p.read_text()
        for p in assemble_changelog.ordered(repo, assemble_changelog.fragment_paths(repo))
    ]

    note = release_note.render("v1.0.0..v1.1.0-rc.1", repo=str(repo), fragments_at="v1.1.0-rc.1")

    positions = [note.index(text) for text in assembly]
    assert positions == sorted(positions), (assembly, note)


def test_the_fragments_are_read_at_the_tag_not_the_working_tree(repo: Path):
    (repo / "changelog.d" / "local-only.md").write_text("- Local only.\n")
    (repo / "changelog.d" / "first-change.md").unlink()

    note = release_note.render("v1.0.0..v1.1.0-rc.1", repo=str(repo), fragments_at="v1.1.0-rc.1")

    assert "- First change." in note
    assert "Local only" not in note


def test_a_later_rc_carries_every_fragment_pending_at_it(repo: Path):
    _commit(repo, "feat: third", {"changelog.d/third-change.md": "- Third change.\n"})
    _git(repo, "tag", "v1.1.0-rc.2")

    note = release_note.render(
        "v1.1.0-rc.1..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2"
    )

    assert [line for line in note.splitlines() if line.startswith("- ") and "change" in line] == [
        "- Third change.",
        "- Second change,",
        "- First change.",
    ], note


def test_a_final_release_carries_its_changelog_section(repo: Path):
    _commit(
        repo,
        "chore: assemble the changelog for 1.1.0",
        {
            "CHANGELOG.md": "# Changelog\n\n<!-- version list -->\n\n"
            "## 1.1.0\n- Second change,\n  over two lines.\n- First change.\n\n"
            "## 1.0.0\n- Old entry.\n"
        },
        remove=("changelog.d/first-change.md", "changelog.d/second-change.md"),
    )
    _git(repo, "tag", "v1.1.0")

    note = release_note.render("v1.1.0-rc.1..v1.1.0", repo=str(repo), fragments_at="v1.1.0")

    assert note.startswith(
        "## Changelog\n\n- Second change,\n  over two lines.\n- First change.\n"
    ), note
    assert RC_LINE not in note
    assert "Old entry" not in note


def test_a_malformed_fragment_is_rendered_as_it_is_and_warned_about(repo: Path, capsys):
    _commit(repo, "docs: a broken fragment", {"changelog.d/broken-entry.md": "no bullet here\n"})
    _git(repo, "tag", "v1.1.0-rc.2")

    note = release_note.render(
        "v1.1.0-rc.1..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2"
    )

    assert "no bullet here" in note
    assert "- First change." in note
    err = capsys.readouterr().err
    assert "::warning::" in err and "broken-entry.md" in err, err


def test_CONTROL_without_the_option_the_note_is_what_it_was(repo: Path):
    plain = release_note.render("v1.0.0..v1.1.0-rc.1", repo=str(repo))

    assert "## Changelog" not in plain
    assert plain.startswith("## Commits"), plain


def test_CONTROL_a_tag_with_nothing_to_carry_renders_what_it_did(repo: Path):
    """No fragments at the tag and no section for its version: the note is unchanged."""
    _commit(
        repo,
        "chore: drop the fragments without assembling",
        remove=("changelog.d/first-change.md", "changelog.d/second-change.md"),
    )
    _git(repo, "tag", "v1.2.0")

    rng = "v1.1.0-rc.1..v1.2.0"
    assert release_note.render(rng, repo=str(repo), fragments_at="v1.2.0") == release_note.render(
        rng, repo=str(repo)
    )


def test_the_command_line_takes_the_tag_to_read(repo: Path):
    script = str(ROOT / "scripts" / "release_note.py")

    proc = subprocess.run(
        [sys.executable, script, "v1.0.0..v1.1.0-rc.1", "--fragments-at", "v1.1.0-rc.1"],
        capture_output=True,
        text=True,
        cwd=repo,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("## Changelog"), proc.stdout


@pytest.mark.parametrize("workflow", [".gitea/workflows/ci.yml", ".github/workflows/ci.yml"])
def test_both_release_jobs_pass_the_tag_to_the_renderer(workflow: str):
    text = (ROOT / workflow).read_text()

    assert 'python3 scripts/release_note.py "$RANGE" --fragments-at "$TAG"' in text, workflow


# --- a prerelease tagged after its fragments were assembled -----------------

ASSEMBLED = (
    "# Changelog\n\n<!-- version list -->\n\n"
    "## 1.1.0\n"
    "- **Breaking**: first removal,\n  over two lines.\n"
    "- A plain fix.\n"
    "- **Behaviour change**: a change of behaviour.\n"
    "- **Breaking**: second removal.\n"
    "- **Breaking**: third removal.\n\n"
    "## 1.0.0\n- Old entry.\n"
)
BREAKING = ["first removal,", "second removal.", "third removal."]


def _assemble(repo: Path, tag: str, changelog: str = ASSEMBLED) -> None:
    """The release-prep commit: every fragment deleted, CHANGELOG.md written, then tagged."""
    _commit(
        repo,
        "chore: assemble the changelog",
        {"CHANGELOG.md": changelog},
        remove=("changelog.d/first-change.md", "changelog.d/second-change.md"),
    )
    _git(repo, "tag", tag)


def test_a_prerelease_tagged_after_assembly_carries_every_breaking_entry_of_its_section(
    repo: Path,
):
    """The release job runs at the tag, where the release-prep has already deleted the fragments.

    The tag is `v1.1.0-rc.1` and the section is `## 1.1.0`. Reading the section by the tag's own
    name found nothing, so the note held only what commit footers said was breaking.
    """
    _assemble(repo, "v1.1.0-rc.2")

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    for entry in BREAKING:
        assert entry in note, (entry, note)
    changelog = note.split("## Breaking changes", 1)[0]
    assert changelog.count("- **Breaking**") == len(BREAKING), note
    assert "over two lines." in note
    assert "- A plain fix." in note
    assert "Old entry" not in note


def test_a_prerelease_note_says_its_entries_are_pending_since_the_last_final_release(repo: Path):
    _assemble(repo, "v1.1.0-rc.2")

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    assert note.startswith(f"## Changelog\n\n{RC_LINE}\n\n- **Breaking**: first removal,"), note


def test_the_section_is_the_one_of_the_version_the_prerelease_leads_to(repo: Path):
    """A prerelease of 1.1.0 does not read the section of 1.10.0, 1.0.0 or 1.1.0's neighbour."""
    other = ASSEMBLED.replace("## 1.1.0\n", "## 1.10.0\n")
    _assemble(repo, "v1.1.0-rc.2", changelog=other)

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    assert "## Changelog" not in note, note
    assert "removal" not in note


def test_pending_fragments_still_take_precedence_over_a_section_of_the_same_version(repo: Path):
    _commit(repo, "chore: a section already present", {"CHANGELOG.md": ASSEMBLED})
    _git(repo, "tag", "v1.1.0-rc.2")

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    assert "- First change." in note and "- Second change," in note, note
    assert "removal" not in note


# --- the Breaking changes section, and the release job's count of it -------


FOOTER_COMMIT = (
    "fix(idempotency): a change with a footer\n\n"
    "BREAKING CHANGE: the footer's own account of the change.\n"
)


def _breaking_section(note: str) -> str:
    return note.split("## Breaking changes\n", 1)[1].split("\n## ", 1)[0]


def test_an_rc_note_lists_the_breaking_fragments_and_not_a_commit_footer(repo: Path):
    """The 1.2.0 rc, in small: Breaking fragments pending at the tag, beside a conventional commit
    with a `BREAKING CHANGE:` footer. The section lists the fragments; the footer is not read."""
    _commit(
        repo,
        "docs: two breaking fragments",
        {
            "changelog.d/a-removal.md": "- **Breaking**: the removal,\n  over two lines.\n",
            "changelog.d/a-rename.md": "- **Breaking**: the rename.\n",
        },
    )
    _commit(repo, FOOTER_COMMIT, {"extra.txt": "x"})
    _git(repo, "tag", "v1.1.0-rc.2")

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    section = _breaking_section(note)
    # Two fragments of one commit are ordered by file name, as assembly orders them.
    assert section.strip().splitlines() == [
        "- **Breaking**: the removal,",
        "  over two lines.",
        "- **Breaking**: the rename.",
    ], section
    assert "the footer's own account" not in note, note
    assert "### " not in note, note
    assert release_note.breaking_count(note) == 2


def test_an_assembled_prerelease_lists_its_sections_breaking_entries(repo: Path):
    _commit(repo, FOOTER_COMMIT, {"extra.txt": "x"})
    _assemble(repo, "v1.1.0-rc.2")

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    section = _breaking_section(note)
    for entry in BREAKING:
        assert entry in section, (entry, section)
    assert "A plain fix" not in section and "a change of behaviour" not in section
    assert "the footer's own account" not in note
    assert release_note.breaking_count(note) == len(BREAKING), note


def test_a_release_with_no_breaking_entry_has_no_breaking_section(repo: Path):
    _commit(repo, FOOTER_COMMIT, {"extra.txt": "x"})
    _git(repo, "tag", "v1.1.0-rc.2")

    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    assert "## Breaking changes" not in note, note
    assert release_note.breaking_count(note) == 0


def test_CONTROL_a_commit_subject_that_looks_like_an_entry_is_not_counted(repo: Path):
    _commit(repo, "- **Breaking**: only a commit subject", {"extra.txt": "x"})
    _assemble(repo, "v1.1.0-rc.2")
    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")

    assert "- - **Breaking**: only a commit subject" in note
    assert release_note.breaking_count(note) == len(BREAKING), note


def test_the_command_line_counts_a_note_given_on_stdin(repo: Path):
    _commit(repo, FOOTER_COMMIT, {"extra.txt": "x"})
    _assemble(repo, "v1.1.0-rc.2")
    note = release_note.render("v1.0.0..v1.1.0-rc.2", repo=str(repo), fragments_at="v1.1.0-rc.2")
    script = str(ROOT / "scripts" / "release_note.py")

    out = subprocess.run(
        [sys.executable, script, "--count-breaking"],
        input=note,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert out.split() == [str(len(BREAKING))], out


def test_the_gitea_release_job_counts_through_the_renderer_not_by_heading():
    run = "\n".join(
        step["run"]
        for step in yaml.safe_load((ROOT / ".gitea/workflows/ci.yml").read_text())["jobs"][
            "release"
        ]["steps"]
        if "run" in step
    )

    assert "release_note.py --count-breaking" in run
    assert "grep -c '^### '" not in run
