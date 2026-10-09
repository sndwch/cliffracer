"""docs/checks-and-guards.md has a table row for every guard in tests/repo/, and only for guards that exist.

The index says it "covers every guard in that directory", and nothing compared
the two: a guard added without a row, a row left behind by a renamed file, and a
row that slid out of its table into the prose under it all read as a finished
index. A guard is a `test_*.py` module; `ci_workflows.py` and the conftest are
helpers and are not indexed.

ONE GUARD IS INDEXED BY DESCRIPTION. The guard that sweeps the tree for a
stopped decorator's name fails on any file that spells the name, and this
document is one of them, so its filename cannot appear in the table. It is the
only module allowed to be absent, and it is found by what its filename contains
rather than by being listed here. The name is joined from two halves below for
the same reason: spelled whole it would need an allowlist entry in that guard.

What this does not read is whether a row's description is true of its module;
that is a claim about a guard's behaviour and only reading the guard decides it.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "docs" / "checks-and-guards.md"

# The stopped decorator's name, in two halves so this file does not spell it.
DESCRIBED_ONLY = "validated" + "_rpc"

# A backticked bare module name opening a table row: `| \`test_x.py\` | ...`.
_ROW_SUBJECT = re.compile(r"^\|\s*`(test_[a-z0-9_]+\.py)`")


def guards() -> set[str]:
    """Every tracked guard module in tests/repo/."""
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "tests/repo/test_*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {Path(rel).name for rel in listed}


def indexed(text: str) -> set[str]:
    """The guards that open a table row."""
    found = set()
    for line in text.splitlines():
        match = _ROW_SUBJECT.match(line)
        if match:
            found.add(match.group(1))
    return found


def unindexed(text: str, modules: set[str]) -> list[str]:
    """Guards with no row, the one described by description excepted."""
    return sorted(name for name in modules - indexed(text) if DESCRIBED_ONLY not in name)


def rows_for_missing_guards(text: str, modules: set[str]) -> list[str]:
    """Rows whose module is not in tests/repo/."""
    return sorted(indexed(text) - modules)


def rows_outside_a_table(text: str) -> list[str]:
    """Table rows that follow something other than a table line.

    A row after a blank line or a paragraph renders as literal pipes, and its
    guard reads as indexed to a search of the file while being unreadable to a
    person.
    """
    lines = text.splitlines()
    return [
        f"line {number}: {line[:70]}"
        for number, line in enumerate(lines, 1)
        if _ROW_SUBJECT.match(line) and (number == 1 or not lines[number - 2].startswith("|"))
    ]


def test_every_guard_has_a_row():
    missing = unindexed(INDEX.read_text(), guards())
    assert not missing, (
        "tests/repo/ holds guards the index does not list. Add a row to the table "
        f"they belong in, saying what each reads and what makes it fail: {missing}"
    )


def test_every_row_names_a_guard_that_exists():
    stale = rows_for_missing_guards(INDEX.read_text(), guards())
    assert not stale, f"the index has rows for modules tests/repo/ does not hold: {stale}"


def test_every_row_sits_inside_its_table():
    stray = rows_outside_a_table(INDEX.read_text())
    assert not stray, "these rows are not part of a table:\n  " + "\n  ".join(stray)


def test_the_one_guard_indexed_by_description_exists_and_is_not_named():
    """The omission is the exception it says it is: one module, and not in the table."""
    described = sorted(name for name in guards() if DESCRIBED_ONLY in name)
    assert len(described) == 1, f"the index describes exactly one guard by description: {described}"
    assert described[0] not in indexed(INDEX.read_text())


def test_the_sweep_reads_the_directory_and_the_index():
    """A sweep that found nothing would report no guard missing."""
    assert len(guards()) > 80, sorted(guards())
    assert len(indexed(INDEX.read_text())) > 80


# --- controls: the checks above, run on a planted index -----------------------

_TABLE = "| Guard | Reads | Fails when |\n|---|---|---|\n"
_PLANTED = _TABLE + "| `test_a.py` | x | y |\n| `test_b.py` | x | y |\n"


def test_CONTROL_a_guard_with_no_row_is_reported():
    assert unindexed(_PLANTED, {"test_a.py", "test_b.py", "test_c.py"}) == ["test_c.py"]


def test_CONTROL_a_guard_named_only_in_prose_is_reported():
    text = _PLANTED + "\nSee also `test_c.py` for the rest.\n"
    assert unindexed(text, {"test_a.py", "test_b.py", "test_c.py"}) == ["test_c.py"]


def test_CONTROL_the_described_guard_is_the_one_exception():
    assert unindexed(_PLANTED, {"test_a.py", f"test_no_{DESCRIBED_ONLY}.py"}) == []


def test_CONTROL_a_row_for_a_renamed_file_is_reported():
    assert rows_for_missing_guards(_PLANTED, {"test_a.py"}) == ["test_b.py"]


def test_CONTROL_a_row_after_a_paragraph_is_reported():
    text = _PLANTED + "\nSome prose.\n\n| `test_c.py` | x | y |\n"
    assert len(rows_outside_a_table(text)) == 1
    assert rows_outside_a_table(_PLANTED) == []


def test_CONTROL_a_row_after_a_blank_line_inside_the_table_is_reported():
    text = _TABLE + "| `test_a.py` | x | y |\n\n| `test_b.py` | x | y |\n"
    assert len(rows_outside_a_table(text)) == 1
