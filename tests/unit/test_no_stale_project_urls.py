"""Ensure repository files point to canonical GitHub URLs rather than legacy hostnames."""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (REPO / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


OLD_HOME = "datatrellis"

CANONICAL = "https://github.com/sndwch/cliffracer"

# This test file contains pattern strings and is exempt from self-checking.
EXEMPT = {"tests/unit/test_no_stale_project_urls.py"}

# Active allowlist for in-flight migrations; empty when all files are compliant.
ALLOWED: dict[str, str] = {}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return out


def _is_text(path: Path) -> bool:
    try:
        path.read_text()
    except (UnicodeDecodeError, OSError):
        return False
    return True


def old_home_lines(paths=None) -> list[str]:
    found = []
    for rel in paths if paths is not None else tracked_files():
        doc = rel if isinstance(rel, Path) else REPO / rel
        name = rel if isinstance(rel, str) else doc.name
        if isinstance(rel, str) and (rel in EXEMPT or rel in ALLOWED):
            continue
        if not doc.is_file() or not _is_text(doc):
            continue
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            if OLD_HOME in line:
                found.append(f"{name}:{number} {line.strip()[:100]}")
    return found


@pytest.mark.unit
def test_the_sweep_reads_the_repository():
    """Verify repository files are discovered by git ls-files."""
    files = tracked_files()
    assert len(files) >= 50, f"only found {len(files)} tracked files"
    assert all((REPO / f).exists() for f in files), "git listed a file that is not on disk"


@pytest.mark.unit
def test_no_tracked_file_names_the_old_home():
    found = old_home_lines()
    assert not found, (
        f"cliffracer lives at {CANONICAL}. These name the address it moved from, "
        "which ships in the wheel metadata and sends readers to the wrong "
        "place:\n  " + "\n  ".join(found)
    )


@pytest.mark.unit
def test_the_packaging_metadata_points_at_the_canonical_home():
    """Verify pyproject.toml project.urls point to the canonical URL."""
    import tomllib

    with open(REPO / "pyproject.toml", "rb") as fh:
        urls = tomllib.load(fh)["project"]["urls"]

    assert set(urls) == {"Homepage", "Documentation", "Repository", "Issues"}, urls
    wrong = {k: v for k, v in urls.items() if not v.startswith(CANONICAL)}
    assert not wrong, f"[project.urls] entries not under {CANONICAL}: {wrong}"


@pytest.mark.unit
def test_the_allowlist_has_no_stale_entries():
    """Verify no redundant entries remain in ALLOWED."""
    clean = sorted(rel for rel in ALLOWED if OLD_HOME not in (REPO / rel).read_text())
    assert not clean, (
        "these files no longer name the old home, so their allowlist entries "
        f"exempt them for nothing -- delete them: {clean}"
    )


@pytest.mark.unit
def test_the_allowlist_and_exemptions_name_files_that_exist():
    missing = sorted(rel for rel in (set(ALLOWED) | EXEMPT) if not (REPO / rel).exists())
    assert not missing, f"allowlisted or exempt files that do not exist: {missing}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        'Homepage = "https://github.com/datatrellis/cliffracer"',
        "git clone https://github.com/datatrellis/microservices.git",
        'Documentation = "https://datatrellis.github.io/cliffracer"',
        "- Issues: [GitHub Issues](https://github.com/datatrellis/cliffracer/issues)",
    ],
)
def test_CONTROL_each_form_of_the_old_address_is_caught(tmp_path: Path, line: str):
    """Verify all legacy host and organization patterns are detected."""
    doc = tmp_path / "f.toml"
    doc.write_text(line + "\n")
    assert old_home_lines([doc]), f"not caught: {line!r}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        'Homepage = "https://github.com/sndwch/cliffracer"',
        "git clone https://github.com/sndwch/cliffracer.git",
        "See https://github.com/astral-sh/ruff for the linter.",
        "The registry is at https://pypi.org/simple",
    ],
)
def test_CONTROL_the_canonical_home_and_other_github_links_pass(tmp_path: Path, line: str):
    """Verify canonical URLs and legitimate external references are permitted."""
    doc = tmp_path / "f.md"
    doc.write_text(line + "\n")
    assert not old_home_lines([doc]), f"wrongly flagged: {line!r}"
    doc = tmp_path / "f.md"
    doc.write_text(line + "\n")
    assert not old_home_lines([doc]), f"wrongly flagged: {line!r}"
