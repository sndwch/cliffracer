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


def _check(copy: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--check", "--doc", str(copy)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def test_CONTROL_the_checker_fails_on_a_wrong_default(tmp_path):
    """The table's default column is generated from the model, so a different one is drift."""
    doc = REPO / "docs" / "api-reference.md"
    text = doc.read_text()
    row = "| `request_timeout` | `30.0` |"
    assert text.count(row) == 1, "the row this control damages is gone; pick another"
    copy = tmp_path / "api-reference.md"
    copy.write_text(text.replace(row, "| `request_timeout` | `31.0` |"))

    result = _check(copy)

    assert result.returncode == 1, "the checker passed a table with a wrong default"
    assert doc.read_text() == text, "the control touched the tracked doc"


def test_CONTROL_the_checker_fails_on_a_reordered_table(tmp_path):
    """Rows follow the model's field order, so two swapped rows are drift."""
    doc = REPO / "docs" / "api-reference.md"
    text = doc.read_text()
    lines = text.splitlines(keepends=True)
    first = next(i for i, line in enumerate(lines) if line.startswith("| `name` |"))
    lines[first], lines[first + 1] = lines[first + 1], lines[first]
    copy = tmp_path / "api-reference.md"
    copy.write_text("".join(lines))

    result = _check(copy)

    assert result.returncode == 1, "the checker passed a table with two rows swapped"
    assert doc.read_text() == text, "the control touched the tracked doc"


def _fields_with_a_description_in_the_model() -> list[str]:
    from cliffracer.core.service_config import ServiceConfig

    return [
        name
        for name, field in ServiceConfig.model_fields.items()
        if (field.description or "").strip()
    ]


def _row_cells(text: str, name: str) -> tuple[int, str]:
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(f"| `{name}` |"))
    return index, lines[index]


@pytest.mark.parametrize("name", _fields_with_a_description_in_the_model())
def test_a_row_edited_away_from_the_models_description_is_reported(name, tmp_path):
    """A field that says what it does in the model has that sentence in its row, always.

    The generator used to keep whatever a human had in the row over the model's text, so the
    sentence was written twice and nothing compared them: changing "default of 120" to "90" in
    the `ping_interval` row passed every test.
    """
    doc = REPO / "docs" / "api-reference.md"
    text = doc.read_text()
    index, row = _row_cells(text, name)
    lines = text.splitlines(keepends=True)
    lines[index] = row.rstrip("| ").rstrip() + " (edited by hand) |\n"
    copy = tmp_path / "api-reference.md"
    copy.write_text("".join(lines))

    result = _check(copy)

    assert result.returncode == 1, f"the checker passed a changed description for {name}"
    assert doc.read_text() == text, "the control touched the tracked doc"


def test_the_ping_interval_row_cannot_say_a_different_default_than_the_model(tmp_path):
    """The case from the report: the number inside a description, not the default column."""
    doc = REPO / "docs" / "api-reference.md"
    text = doc.read_text()
    assert text.count("default of 120") == 1, "the sentence this control damages is gone"
    copy = tmp_path / "api-reference.md"
    copy.write_text(text.replace("default of 120", "default of 90"))

    assert _check(copy).returncode == 1


def test_a_hand_written_row_for_a_field_the_model_does_not_describe_survives_regeneration(tmp_path):
    """The one cell nothing else derives is kept, or the generator would erase 35 descriptions."""
    doc = REPO / "docs" / "api-reference.md"
    text = doc.read_text()
    index, row = _row_cells(text, "request_timeout")
    lines = text.splitlines(keepends=True)
    lines[index] = row.rstrip("| ").rstrip() + " A sentence a human added. |\n"
    copy = tmp_path / "api-reference.md"
    copy.write_text("".join(lines))

    rewritten = subprocess.run(
        [sys.executable, str(TOOL), "--doc", str(copy)], cwd=REPO, capture_output=True, text=True
    )

    assert rewritten.returncode == 0, rewritten.stderr
    assert "A sentence a human added." in copy.read_text()
    assert _check(copy).returncode == 0
