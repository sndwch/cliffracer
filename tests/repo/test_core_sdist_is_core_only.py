"""Tests verifying the core source distribution excludes workspace member packages."""

import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]
BLOCK = "[tool.hatch.build.targets.sdist]"


@pytest.fixture(autouse=True)
def _require_git():
    if not (ROOT / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


def _build_core_sdist(out: Path, cwd: Path) -> Path:
    """-> the built .tar.gz. Never touches dist/.

    Note `uv build --out-dir` also writes a `.gitignore` into the directory, so
    pick the artefact by suffix rather than by taking the only file there.
    """
    proc = subprocess.run(
        ["uv", "build", "--package", "cliffracer", "--sdist", "--out-dir", str(out)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"uv build failed:\n{proc.stdout}\n{proc.stderr}"
    sdists = [f for f in sorted(out.iterdir()) if f.name.endswith(".tar.gz")]
    assert len(sdists) == 1, f"expected exactly one sdist, got {[f.name for f in out.iterdir()]}"
    return sdists[0]


def _entries(sdist: Path) -> list[str]:
    """Paths inside the sdist, with the top-level <name>-<version>/ prefix removed."""
    with tarfile.open(sdist) as tf:
        return sorted(n.split("/", 1)[1] for n in tf.getnames() if "/" in n)


@pytest.mark.slow
def test_core_sdist_contains_no_workspace_member(tmp_path):
    entries = _entries(_build_core_sdist(tmp_path / "dist", ROOT))

    # The instrument first: an empty or unreadable archive would satisfy
    # "contains no packages/" vacuously.
    assert any(e.startswith("cliffracer/") for e in entries), (
        f"core's own package is missing from its sdist; the archive is {entries}"
    )

    members = [e for e in entries if e.startswith("packages/")]
    assert not members, (
        f"core's sdist ships {len(members)} entries from packages/, which are the "
        f"workspace members' own distributions: {members[:5]}"
    )


@pytest.mark.slow
def test_the_sdist_is_an_allowlist_not_a_default(tmp_path):
    """Verify non-core directories are excluded from the sdist."""
    entries = _entries(_build_core_sdist(tmp_path / "dist", ROOT))
    for unwanted in ("packages/", "tests/", "docs/", "examples/", "load-testing/", "deployment/"):
        assert not [e for e in entries if e.startswith(unwanted)], (
            f"{unwanted} is in core's sdist; the include list in "
            "[tool.hatch.build.targets.sdist] is what keeps it out"
        )


@pytest.mark.slow
def test_CONTROL_without_the_include_list_the_members_come_back(tmp_path):
    """Verify that removing the sdist include configuration includes workspace members."""
    work = tmp_path / "repo"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(work), "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        pyproject = work / "pyproject.toml"
        text = pyproject.read_text()
        # Use find() rather than index() to avoid ValueError before assertion.
        start = text.find(BLOCK)
        end = text.find("[tool.hatch.version]")
        assert start != -1 and end != -1, (
            f"{BLOCK} or [tool.hatch.version] is not in HEAD's pyproject.toml. "
            "This control reads the COMMITTED tree, so an uncommitted edit to "
            "the include list is invisible here -- commit it and re-run."
        )
        pyproject.write_text(text[:start] + text[end:])
        assert BLOCK not in pyproject.read_text()

        entries = _entries(_build_core_sdist(tmp_path / "dist2", work))
        members = [e for e in entries if e.startswith("packages/")]
        assert members, (
            "the control did not diverge: removing the include list should let "
            f"the workspace members back into core's sdist, got {len(entries)} entries"
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(work)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
