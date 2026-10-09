"""Ensure repository files point to canonical GitHub URLs rather than legacy hostnames."""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


OLD_HOME = "datatrellis"

CANONICAL = "https://github.com/sndwch/cliffracer"

# This test file contains pattern strings and is exempt from self-checking.
EXEMPT = {"tests/repo/test_no_stale_project_urls.py"}

# Active allowlist for in-flight migrations; maps repo-relative path to documented justification.
ALLOWED: dict[str, str] = {
    # AGENTS.md documents the internal Gitea instance on purpose: it is held
    # back from the public push by hand and exists to tell an agent working
    # against that instance how to do it. The entry is here rather than in
    # EXEMPT so it stays visible -- anyone configuring an automated mirror must
    # deal with this file, and this line is where they will find out.
    "AGENTS.md": "internal-only, unmirrored; names the Gitea instance deliberately",
}


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


def _to_rel_path_str(rel: Path | str) -> str:
    if isinstance(rel, Path):
        try:
            return rel.resolve().relative_to(REPO.resolve()).as_posix()
        except ValueError:
            return rel.as_posix()
    return rel


def old_home_lines(paths=None) -> list[str]:
    found = []
    candidates = paths if paths is not None else tracked_files()
    for rel in candidates:
        rel_str = _to_rel_path_str(rel)
        if rel_str in EXEMPT or rel_str in ALLOWED:
            continue
        doc = rel if isinstance(rel, Path) else REPO / rel
        if not doc.is_file() or not _is_text(doc):
            continue
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            if OLD_HOME in line:
                found.append(f"{rel_str}:{number} {line.strip()[:100]}")
    return found


def test_tracked_files_discovers_the_whole_repository():
    """Pin the shape of what the sweep reads, not just that it returns something.

    Entries are repo-relative and include files at the top level. A discovery
    step that dropped those would leave README.md and pyproject.toml unswept
    while every other test in this file still passed.
    """
    files = tracked_files()
    assert len(files) >= 50, f"only found {len(files)} tracked files"
    assert all((REPO / f).exists() for f in files), "git listed a file that is not on disk"

    absolute = [f for f in files if Path(f).is_absolute()]
    assert not absolute, f"tracked_files must yield repo-relative paths, got {absolute[:3]}"

    for expected in ("README.md", "pyproject.toml", "CONTRIBUTING.md"):
        assert expected in files, (
            f"{expected} is tracked but not discovered; the sweep cannot see "
            "top-level files and a stale address in one would go unreported"
        )


def test_no_tracked_file_names_the_old_home():
    found = old_home_lines()
    assert not found, (
        f"cliffracer lives at {CANONICAL}. These name the address it moved from, "
        "which ships in the wheel metadata and sends readers to the wrong "
        "place:\n  " + "\n  ".join(found)
    )


def test_the_packaging_metadata_points_at_the_canonical_home():
    """Verify pyproject.toml project.urls point to the canonical URL."""
    import tomllib

    with open(REPO / "pyproject.toml", "rb") as fh:
        urls = tomllib.load(fh)["project"]["urls"]

    assert set(urls) == {"Homepage", "Documentation", "Repository", "Issues"}, urls
    wrong = {k: v for k, v in urls.items() if not v.startswith(CANONICAL)}
    assert not wrong, f"[project.urls] entries not under {CANONICAL}: {wrong}"


def urls_outside_the_canonical_home(pyprojects) -> dict[str, dict[str, str]]:
    """Each pyproject's `[project.urls]` entries that are not under CANONICAL.

    A distribution with no `[project.urls]` has none to be wrong, so absence passes.
    """
    import tomllib

    wrong: dict[str, dict[str, str]] = {}
    for path in pyprojects:
        with open(path, "rb") as fh:
            urls = tomllib.load(fh).get("project", {}).get("urls", {})
        bad = {k: v for k, v in urls.items() if not v.startswith(CANONICAL)}
        if bad:
            wrong[str(path)] = bad
    return wrong


def test_a_member_distribution_that_declares_urls_points_them_at_the_canonical_home():
    """The members declare none today; one that starts to has the root's rule applied.

    The text sweep above flags only the address the project moved from. A member that pointed
    at some other place would pass it and put that place on its registry page.
    """
    members = sorted((REPO / "packages").glob("*/pyproject.toml"))
    assert len(members) >= 8, f"only found {len(members)} member pyprojects"

    assert urls_outside_the_canonical_home(members) == {}


def test_CONTROL_a_url_outside_the_canonical_home_is_reported(tmp_path):
    stale = tmp_path / "stale.toml"
    stale.write_text(
        f'[project]\nname = "x"\n[project.urls]\nHomepage = "{CANONICAL}/x"\n'
        'Repository = "https://example.org/x"\n'
    )
    fine = tmp_path / "fine.toml"
    fine.write_text(f'[project]\nname = "y"\n[project.urls]\nHomepage = "{CANONICAL}/y"\n')
    none = tmp_path / "none.toml"
    none.write_text('[project]\nname = "z"\n')

    assert urls_outside_the_canonical_home([stale, fine, none]) == {
        str(stale): {"Repository": "https://example.org/x"}
    }


def test_the_allowlist_has_no_stale_entries():
    """Verify no redundant entries remain in ALLOWED, and all entries have documented reasons.

    While ALLOWED is empty the loop below runs zero times, so the empty case is
    asserted on its own: nothing is exempted by omission.
    """
    if not ALLOWED:
        assert not old_home_lines(), (
            "the allowlist is empty, so the sweep must report nothing; it reports "
            f"{old_home_lines()}"
        )

    for rel, reason in ALLOWED.items():
        assert isinstance(reason, str) and reason.strip(), (
            f"Missing justification for allowlisted file: {rel}"
        )
        assert (REPO / rel).exists(), f"Allowlisted file does not exist: {rel}"
        assert OLD_HOME in (REPO / rel).read_text(), (
            f"Allowlisted file no longer contains target string: {rel}"
        )


def test_the_allowlist_and_exemptions_name_files_that_exist():
    missing = sorted(rel for rel in (set(ALLOWED) | EXEMPT) if not (REPO / rel).exists())
    assert not missing, f"allowlisted or exempt files that do not exist: {missing}"


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


def test_CONTROL_sweep_integrates_discovery_and_exemption(monkeypatch, tmp_path: Path):
    """Control: old_home_lines() with no arguments joins discovery to the filter.

    Discovery is replaced with repo-relative names, the shape `git ls-files`
    really returns, so every phase exercises the input the sweep sees in anger.
    """
    rel_name = "test_fake_legacy.txt"
    (tmp_path / rel_name).write_text("Reference to datatrellis in tracked file\n")
    monkeypatch.setattr("tests.repo.test_no_stale_project_urls.REPO", tmp_path)
    monkeypatch.setattr(
        "tests.repo.test_no_stale_project_urls.tracked_files",
        lambda: [rel_name],
    )

    assert len(old_home_lines()) == 1, "the sweep did not read what discovery returned"

    monkeypatch.setattr("tests.repo.test_no_stale_project_urls.EXEMPT", {rel_name})
    assert len(old_home_lines()) == 0, "an exempt path was still reported"

    monkeypatch.setattr("tests.repo.test_no_stale_project_urls.EXEMPT", set())
    monkeypatch.setattr(
        "tests.repo.test_no_stale_project_urls.ALLOWED", {rel_name: "documented reason"}
    )
    assert len(old_home_lines()) == 0, "an allowlisted path was still reported"
