"""Every `ServiceConfig` field says what it does, not merely that it exists.

`test_the_service_config_table_matches_model_fields` checks the table against
the model and catches drift in both directions: a field described in the model
with a stale table reds, and a table row whose field is gone reds. The one
state it cannot see is the field described in neither place, because the
generator emits an empty cell, the file holds an empty cell, and they agree.

That is not a weakness in the sync check -- it is a different property. The two
failures also send a reader somewhere different: "regenerate the table" and
"write a sentence about your field" are not the same instruction, so they are
separate tests with separate messages.

WHY THIS READS THE TABLE AND NOT `Field(description=...)`. I argued for the
model side first, on the grounds that a table cell has no source and a
regeneration would lose it. Counted against the tree, that spec would have
required moving 35 of 41 descriptions into the model -- the generator
keeps a hand-written cell for a field the model does not describe, so a human
can write a better sentence than a field declaration wants to carry. A field
that does carry `description=` has that text in its cell, always. So the cell
is the rendered answer for both routes, and it is also what a reader actually
sees. Reading the model would red for the fields documented only in the table.
"""

import importlib.util
import re
from pathlib import Path

import pytest

from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TABLE = REPO / "docs" / "api-reference.md"

# The table's bounds are the generator's own markers, read through its own function, so this guard
# and `test_generated_docs_are_in_sync` read the same block of the same file.
_spec = importlib.util.spec_from_file_location(
    "_gen_service_config_table", REPO / "tools" / "gen_service_config_table.py"
)
assert _spec is not None and _spec.loader is not None
GENERATOR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GENERATOR)

# `| `name` | default | description |`, with the description as everything up
# to the final pipe so a cell containing pipes in code spans is not truncated.
ROW = re.compile(r"^\|\s*`([A-Za-z_]\w*)`\s*\|(?P<default>.*?)\|(?P<desc>.*)\|\s*$")


def table_rows(text: str) -> dict[str, str]:
    """Field name -> description cell, for every row of the config table.

    Only the block between the generator's markers is read. Another table in the document whose
    first column is a backticked name, such as the TypeRef kinds, is not a config table.
    """
    split = GENERATOR.table_block(text)
    assert split is not None, (
        f"the ServiceConfig table's markers {GENERATOR.START} / {GENERATOR.END} are not both "
        f"in the document, so there is no table to read"
    )
    found = {}
    for line in split[1].splitlines():
        match = ROW.match(line)
        if match:
            found[match.group(1)] = match.group("desc").strip()
    return found


def undescribed(fields: list[str], rows: dict[str, str]) -> list[str]:
    """Every field with no description a reader could find.

    A field absent from the table counts as undescribed rather than being
    skipped: absence and emptiness are the same thing to someone looking the
    field up, and skipping it would make this pass by finding nothing.
    """
    return sorted(name for name in fields if not rows.get(name, "").strip())


def test_every_service_config_field_has_a_description():
    rows = table_rows(TABLE.read_text())
    missing = undescribed(list(ServiceConfig.model_fields), rows)

    assert not missing, (
        f"these ServiceConfig fields are in the table with no description: "
        f"{missing}. The sync check passes on them because the generator emits "
        f"an empty cell and the file holds one, so they agree. Write the cell in "
        f"docs/api-reference.md, or give the field a Field(description=...) and "
        f"regenerate with tools/gen_service_config_table.py."
    )


def test_the_table_was_read_and_pairs_with_the_model():
    """A positive reading: no undescribed fields and no rows read look alike.

    The pairing is asserted here rather than assumed, because `undescribed`
    treats an unparsed row as a missing description -- so a parser that stopped
    working would fail the test above with every field named, and this says
    which of the two happened.
    """
    rows = table_rows(TABLE.read_text())

    assert len(rows) >= 30, f"only {len(rows)} table rows parsed; the row pattern is not reading"
    only_in_table, only_in_model = drift(rows)
    assert not only_in_table and not only_in_model, (
        f"the table and the model have drifted: only in the table "
        f"{only_in_table}, only in the model {only_in_model}"
    )


def drift(rows: dict[str, str]) -> tuple[list[str], list[str]]:
    """The names read from the table and not in the model, and the reverse."""
    fields = set(ServiceConfig.model_fields)
    return sorted(set(rows) - fields), sorted(fields - set(rows))


def _with_markers(*lines: str) -> str:
    return "\n".join([GENERATOR.START, *lines, GENERATOR.END])


def test_the_rows_read_are_the_rows_the_generator_writes():
    """The same block, read by both: the field names this guard reads, in order, are the ones
    the generator emits for the model, so the two guards cannot disagree on what the table is."""
    text = TABLE.read_text()
    split = GENERATOR.table_block(text)
    assert split is not None
    written = [
        match.group(1)
        for line in GENERATOR.render(split[1]).splitlines()
        if (match := ROW.match(line))
    ]

    assert list(table_rows(text)) == written == list(ServiceConfig.model_fields)


def test_CONTROL_a_backticked_table_outside_the_markers_is_not_read():
    """The case that showed it: describe's TypeRef kinds, a three-column table whose first column
    is a backticked name, in the same document as the config table."""
    text = TABLE.read_text()
    kinds = "\n".join(
        [
            "| kind | fields | Python |",
            "|---|---|---|",
            "| `scalar` | `name` | the type |",
            "| `stream` | `item` | `AsyncIterator[item]` |",
        ]
    )
    for where in (kinds + "\n\n" + text, text + "\n\n" + kinds + "\n"):
        rows = table_rows(where)
        assert "scalar" not in rows and "stream" not in rows, sorted(rows)
        assert rows == table_rows(text)


def test_CONTROL_a_drifted_row_inside_the_markers_is_still_read():
    """Bounding the read must not stop it reading: a row inside the block for a field the model
    does not have is reported as only in the table, and a field's row removed as only in the
    model."""
    text = TABLE.read_text()
    extra = "| `not_a_field` | `1` | A row for a field the model does not have. |"
    added = text.replace(GENERATOR.END, extra + "\n" + GENERATOR.END)
    assert drift(table_rows(added)) == (["not_a_field"], [])

    removed = "\n".join(line for line in text.splitlines() if not line.startswith("| `name` |"))
    assert drift(table_rows(removed)) == ([], ["name"])


def test_CONTROL_a_document_without_the_markers_fails_by_name():
    """No markers is not "zero rows": the failure says the table's bounds are missing."""
    text = TABLE.read_text().replace(GENERATOR.START, "")

    with pytest.raises(AssertionError, match="markers"):
        table_rows(text)


def test_CONTROL_a_field_with_a_blank_cell_is_reported():
    """The offence, since the real table currently has none."""
    rows = {"named": "It does a thing.", "blank": "", "spaces": "   "}

    assert undescribed(["named", "blank", "spaces"], rows) == ["blank", "spaces"]


def test_CONTROL_a_field_with_no_row_at_all_is_reported():
    """Absence reads the same as emptiness to whoever looks the field up."""
    assert undescribed(["absent"], {"other": "described"}) == ["absent"]


def test_CONTROL_a_described_field_is_not_reported():
    """And the rule is not "report everything", which would satisfy the rest."""
    assert undescribed(["named"], {"named": "It does a thing."}) == []


def test_CONTROL_the_row_pattern_reads_a_cell_containing_pipes():
    """A description with a table-breaking character in a code span survives.

    The cell is taken up to the final pipe, so `\\|` inside a code span does not
    truncate the description and read as blank -- which would report a
    documented field.
    """
    line = "| `namespace` | `None` | A token: no `.`, `*`, `>` or a pipe `|` here. |"
    rows = table_rows(_with_markers(line))

    assert "namespace" in rows, "the row did not parse at all"
    assert rows["namespace"].endswith("here."), rows["namespace"]
    assert undescribed(["namespace"], rows) == []
