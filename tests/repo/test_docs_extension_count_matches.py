"""Verify README.md Extensions table matches workspace packages."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def _extract_extensions_table_content(readme_text: str) -> str:
    """Extract markdown table lines specifically under the Extensions heading."""
    match = re.search(
        r"^##\s+Extensions\b.*?(?=^##?\s+|\Z)",
        readme_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    section = match.group(0)
    table_lines = [line for line in section.splitlines() if line.strip().startswith("|")]
    return "\n".join(table_lines)


def test_readme_extension_table_matches_packages_dir():
    """Assert that the README table lists exactly the extensions that exist in packages/."""
    packages_dir = REPO / "packages"
    actual_packages = sorted(
        p.name
        for p in packages_dir.iterdir()
        if p.is_dir() and (p / "pyproject.toml").exists() and p.name.startswith("cliffracer-")
    )

    readme_path = REPO / "README.md"
    readme_content = readme_path.read_text()

    # Scope extraction strictly to the Extensions table
    table_content = _extract_extensions_table_content(readme_content)
    table_packages = re.findall(r"\|\s*`(cliffracer-[^`]+)`\s*\|", table_content)
    unique_table_packages = sorted(set(table_packages))

    # Instrument floor assertions: prevent vacuous 0 == 0 pass
    assert len(actual_packages) >= 7, (
        f"Floor check failed: expected at least 7 extension packages in packages/, "
        f"found {len(actual_packages)}: {actual_packages}"
    )
    assert len(table_packages) >= 7, (
        f"Floor check failed: expected at least 7 extension rows in Extensions table, "
        f"found {len(table_packages)}: {table_packages}"
    )
    assert len(table_packages) == len(unique_table_packages), (
        f"Duplicate entries found in Extensions table: {table_packages}"
    )

    missing_in_readme = set(actual_packages) - set(unique_table_packages)
    missing_in_dir = set(unique_table_packages) - set(actual_packages)

    assert not missing_in_readme, (
        f"{sorted(missing_in_readme)} exist in packages/ but are not in the "
        f"Extensions table in README.md. Add a row there -- the table is the "
        f"list a reader uses to find out what extensions exist, so a package "
        f"missing from it is invisible however well it is built."
    )
    assert not missing_in_dir, (
        f"{sorted(missing_in_dir)} are listed in README.md's Extensions table "
        f"but have no directory under packages/. Remove the row, or find out "
        f"why the package stopped existing."
    )
