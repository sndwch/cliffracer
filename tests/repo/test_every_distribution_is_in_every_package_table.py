"""Each package table in the docs lists every distribution under `packages/`.

`README.md`'s table was held against `packages/` and the three tables below were not: a package added
to the repository and to the README was missed from `docs/ARCHITECTURE.md`, `docs/extensions.md` and
`docs/api-reference.md`, which are the lists a reader uses to find what extensions exist. `cliffracer-
cyanide` was in none of the three. The table in each is the one whose header row starts with
`| distribution |`, and its first column is read.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
DOCS = ("docs/ARCHITECTURE.md", "docs/extensions.md", "docs/api-reference.md")
ROW = re.compile(r"^\|\s*`(cliffracer-[^`]+)`\s*\|")


def _distributions_under_packages() -> list[str]:
    return sorted(
        p.name
        for p in (REPO / "packages").iterdir()
        if p.is_dir() and (p / "pyproject.toml").exists() and p.name.startswith("cliffracer-")
    )


def _first_column_of_the_distribution_tables(text: str) -> list[list[str]]:
    """The distribution names in each table whose header row starts with `| distribution |`."""
    tables: list[list[str]] = []
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if not line.lstrip().startswith("| distribution |"):
            continue
        names: list[str] = []
        for row in lines[start + 1 :]:
            if not row.lstrip().startswith("|"):
                break
            match = ROW.match(row.strip())
            if match:
                names.append(match.group(1))
        tables.append(names)
    return tables


def test_the_guard_finds_the_packages_it_is_holding_the_docs_to():
    assert len(_distributions_under_packages()) >= 8


@pytest.mark.parametrize("doc", DOCS)
def test_the_doc_has_one_package_table_and_it_lists_every_distribution(doc):
    tables = _first_column_of_the_distribution_tables((REPO / doc).read_text())
    assert len(tables) == 1, f"{doc} has {len(tables)} tables headed `| distribution |`, not one"
    (names,) = tables
    actual = _distributions_under_packages()

    assert len(names) >= 8, f"{doc}'s table has {len(names)} rows; the guard reads the wrong table"
    assert len(names) == len(set(names)), f"{doc} lists a distribution twice: {names}"
    missing = sorted(set(actual) - set(names))
    extra = sorted(set(names) - set(actual))
    assert not missing, (
        f"{missing} are under packages/ but not in {doc}'s package table. The table is the list "
        f"a reader uses to find what ships, so a package missing from it is invisible."
    )
    assert not extra, f"{doc}'s package table names {extra}, which are not under packages/"


def test_CONTROL_a_table_missing_a_distribution_is_read_as_missing_it():
    text = "| distribution | provides |\n|---|---|\n| `cliffracer-auth` | auth |\n| `cliffracer-kv` | kv |\n\nafter\n"

    (names,) = _first_column_of_the_distribution_tables(text)

    assert names == ["cliffracer-auth", "cliffracer-kv"]
    assert set(_distributions_under_packages()) - set(names)


def test_CONTROL_rows_after_the_table_are_not_read_into_it():
    text = (
        "| distribution | provides |\n|---|---|\n| `cliffracer-auth` | auth |\n\n"
        "| other | table |\n|---|---|\n| `cliffracer-kv` | not this table's |\n"
    )

    (names,) = _first_column_of_the_distribution_tables(text)

    assert names == ["cliffracer-auth"]
