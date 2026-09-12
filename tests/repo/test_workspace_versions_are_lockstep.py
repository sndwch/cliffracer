"""Tests verifying built workspace artifacts derive lockstep versions from VCS tags."""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (ROOT / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


ARTEFACT = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*?)(?:-py3-none-any\.whl|\.tar\.gz)$")


def _build_into(out: Path, cwd: Path) -> list[tuple[str, str]]:
    """Build distributions and return a list of (distribution, version) tuples."""
    proc = subprocess.run(
        ["uv", "build", "--all-packages", "--out-dir", str(out)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"uv build failed:\n{proc.stdout}\n{proc.stderr}"
    found = []
    for f in sorted(out.iterdir()):
        # uv writes a .gitignore into --out-dir; only distributions are artefacts.
        if f.suffix not in (".whl", ".gz"):
            continue
        m = ARTEFACT.match(f.name)
        assert m, f"unrecognised artefact name: {f.name}"
        found.append((m.group("name"), m.group("version")))
    return found


@pytest.mark.slow
def test_every_built_artefact_carries_the_same_version(tmp_path):
    found = _build_into(tmp_path / "dist", ROOT)

    # Verify multiple packages built.
    assert len(found) >= 2, f"expected core plus at least one member, got {found}"
    assert any(
        n.startswith("cliffracer_http") or n.startswith("cliffracer-http") for n, _ in found
    ), found

    versions = {v for _, v in found}
    assert len(versions) == 1, f"workspace versions disagree: {sorted(found)}"


@pytest.mark.slow
def test_CONTROL_a_member_with_a_literal_version_fails_this(tmp_path):
    """Verify divergent package versions trigger a failure."""
    work = tmp_path / "repo"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(work), "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        pkg = work / "packages" / "cliffracer-literal" / "src" / "cliffracer_literal"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (work / "packages" / "cliffracer-literal" / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
            '[project]\nname = "cliffracer-literal"\nversion = "9.9.9"\n'
            'requires-python = ">=3.11"\n\n'
            '[tool.hatch.build.targets.wheel]\npackages = ["src/cliffracer_literal"]\n'
        )
        found = _build_into(tmp_path / "dist2", work)
        versions = {v for _, v in found}
        assert "9.9.9" in versions, found
        assert len(versions) > 1, f"the control did not diverge: {sorted(found)}"
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(work)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
