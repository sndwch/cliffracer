"""docs/upgrading.md has a section for every changelog fragment marked Breaking or Removed.

A fragment in `changelog.d/` is one bullet. One that starts `- **Breaking**` changes what a
deployment must do on upgrade, and one that starts `- **Removed**` takes away something a deployment
could have been calling or configuring, which is the same kind of change to the page's reader. The
upgrade guide is where it says what to change. Each section of the guide names the fragment it
covers in a comment, `<!-- changelog.d: <file>.md -->`, and this guard reads both sides: a Breaking
or Removed fragment no section names fails, so does a comment that names one twice, and so does a
comment that names a pending fragment which is neither Breaking nor Removed, because relabelling a
fragment away from either would otherwise leave its section in the guide with nothing to hold it.

A comment that names a fragment no longer in `changelog.d/` is fine: the release assembly deletes
the fragments it writes into `CHANGELOG.md`, and the guide keeps its section. So an empty
`changelog.d/` is not a failure either, and `test_the_guard_is_green_after_the_release_assembly`
runs the assembly on a copy to hold that.

What it does not check is that a section's text agrees with the fragment's. The snippets are run by
`tests/unit/test_the_upgrade_guide_snippets_behave_as_it_says.py`; the prose is read by a reviewer.
"""

import importlib.util
import inspect
import re
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "assemble_changelog", REPO / "scripts" / "assemble_changelog.py"
)
assert _spec is not None and _spec.loader is not None
assemble_changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assemble_changelog)

NEEDS_A_SECTION = re.compile(r"^- \*\*(?:Breaking|Removed)\*\*", re.M)
MARKER = re.compile(r"<!--\s*changelog\.d:\s*(\S+\.md)\s*-->")


def fragments_needing_a_section(directory: Path) -> list[str]:
    return sorted(
        path.name
        for path in directory.glob("*.md")
        if path.name != "README.md" and NEEDS_A_SECTION.search(path.read_text())
    )


def named_fragments(guide: str) -> list[str]:
    return MARKER.findall(guide)


def uncovered(directory: Path, guide: str) -> list[str]:
    named = set(named_fragments(guide))
    return [name for name in fragments_needing_a_section(directory) if name not in named]


def mislabelled(directory: Path, guide: str) -> list[str]:
    """Fragments still pending that a section names although they are neither Breaking nor Removed."""
    needing = set(fragments_needing_a_section(directory))
    return sorted(
        {
            name
            for name in named_fragments(guide)
            if (directory / name).is_file() and name not in needing
        }
    )


def named_twice(guide: str) -> list[str]:
    names = named_fragments(guide)
    return sorted({name for name in names if names.count(name) > 1})


def test_the_guard_reads_the_guide(repo: Path = REPO):
    guide = (repo / "docs" / "upgrading.md").read_text()

    assert len(named_fragments(guide)) >= 10, (
        "the guide names too few fragments; the marker is not read"
    )


def test_every_breaking_or_removed_fragment_has_a_section_in_the_upgrade_guide(repo: Path = REPO):
    guide = (repo / "docs" / "upgrading.md").read_text()

    missing = uncovered(repo / "changelog.d", guide)

    assert missing == [], (
        "these fragments are marked Breaking or Removed and no section of docs/upgrading.md names them. Add a "
        "section with what a deployment sees, what to change and a before and after, and put "
        "`<!-- changelog.d: <file> -->` under its heading:\n  " + "\n  ".join(missing)
    )


def test_every_pending_fragment_a_section_names_is_marked_breaking_or_removed(repo: Path = REPO):
    guide = (repo / "docs" / "upgrading.md").read_text()

    relabelled = mislabelled(repo / "changelog.d", guide)

    assert relabelled == [], (
        "docs/upgrading.md has a section for each of these fragments, and none starts "
        "`- **Breaking**` or `- **Removed**`. Restore the label, or take the section out of the "
        "guide:\n  " + "\n  ".join(relabelled)
    )


def test_no_fragment_is_the_subject_of_two_sections(repo: Path = REPO):
    assert named_twice((repo / "docs" / "upgrading.md").read_text()) == []


def directory_names(directory: Path) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.md"))


def _fragments(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / f"{name}.md").write_text(text)
    return tmp_path


def test_CONTROL_a_breaking_fragment_the_guide_does_not_name_is_reported(tmp_path: Path):
    directory = _fragments(
        tmp_path,
        named="- **Breaking**: a thing.\n",
        unnamed="- **Breaking**: another thing.\n",
        other="- **Behaviour change**: a third.\n",
        README="# readme\n- **Breaking** in prose\n",
    )
    guide = "<!-- changelog.d: named.md -->\n"

    assert uncovered(directory, guide) == ["unnamed.md"]


def test_CONTROL_a_removed_fragment_the_guide_does_not_name_is_reported(tmp_path: Path):
    directory = _fragments(
        tmp_path,
        named="- **Removed**: a thing.\n",
        unnamed="- **Removed**: another thing.\n",
        breaking="- **Breaking**: a third.\n",
        README="# readme\n- **Removed** in prose\n",
    )
    guide = "<!-- changelog.d: named.md -->\n<!-- changelog.d: breaking.md -->\n"

    assert uncovered(directory, guide) == ["unnamed.md"]


def test_CONTROL_a_comment_for_a_fragment_that_has_been_assembled_is_accepted(tmp_path: Path):
    directory = _fragments(tmp_path, named="- **Breaking**: a thing.\n")
    guide = "<!-- changelog.d: named.md -->\n<!-- changelog.d: assembled-and-deleted.md -->\n"

    assert uncovered(directory, guide) == []


def test_CONTROL_a_pending_fragment_relabelled_away_from_breaking_is_reported(tmp_path: Path):
    directory = _fragments(
        tmp_path,
        breaking="- **Breaking**: a thing.\n",
        removed="- **Removed**: a thing.\n",
        relabelled="- **Bug fix**: a thing.\n",
        behaviour="- **Behaviour change**: a thing.\n",
    )
    guide = "".join(f"<!-- changelog.d: {name}.md -->\n" for name in directory_names(directory))

    assert mislabelled(directory, guide) == ["behaviour.md", "relabelled.md"]


def test_CONTROL_a_comment_for_a_fragment_that_has_been_assembled_is_not_a_mislabel(tmp_path: Path):
    directory = _fragments(tmp_path, named="- **Bug fix**: a thing.\n")
    guide = "<!-- changelog.d: assembled-and-deleted.md -->\n"

    assert mislabelled(directory, guide) == []


def test_CONTROL_a_fragment_named_by_two_sections_is_reported():
    guide = "<!-- changelog.d: a.md -->\n<!-- changelog.d: b.md -->\n<!-- changelog.d: a.md -->\n"

    assert named_twice(guide) == ["a.md"]


def _checks_of_the_real_tree():
    """Every test in this module that reads a tree, which takes it as `repo`."""
    return [
        (name, function)
        for name, function in globals().items()
        if name.startswith("test_")
        and callable(function)
        and "repo" in inspect.signature(function).parameters
    ]


def test_the_guard_is_green_after_the_release_assembly(tmp_path: Path):
    """The release-prep change assembles every fragment into CHANGELOG.md and deletes it.

    Run on a copy of the tree, with a Breaking and a Removed fragment planted so the copy always has
    some to assemble, and a version no release carries so the copy takes the section whether or not
    the real one has it. The guard's own tests, every one that takes a tree, are then run on the
    assembled copy: an assertion in any of them that needs a fragment to be pending goes red here,
    which is the state the release-prep change leaves the tree in.
    """
    (tmp_path / "docs").mkdir()
    shutil.copy(REPO / "docs" / "upgrading.md", tmp_path / "docs" / "upgrading.md")
    shutil.copytree(REPO / "changelog.d", tmp_path / "changelog.d")
    shutil.copy(REPO / "CHANGELOG.md", tmp_path / "CHANGELOG.md")
    directory = tmp_path / "changelog.d"
    (directory / "a-planted-breaking-entry.md").write_text("- **Breaking**: a planted entry.\n")
    (directory / "a-planted-removed-entry.md").write_text("- **Removed**: a planted entry.\n")
    guide = (tmp_path / "docs" / "upgrading.md").read_text()
    assert {"a-planted-breaking-entry.md", "a-planted-removed-entry.md"} <= set(
        uncovered(directory, guide)
    ), "the plants are not seen"

    assert assemble_changelog.main(["999.0.0", "--repo", str(tmp_path)]) == 0

    assert not [p for p in directory.glob("*.md") if p.name != "README.md"]
    checks = _checks_of_the_real_tree()
    assert len(checks) >= 3, f"found {len(checks)} tests that take a tree; the selection is empty"
    for _name, check in checks:
        check(repo=tmp_path)
