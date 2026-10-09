"""Tests verifying built workspace artifacts derive lockstep versions from VCS tags."""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.repo.built_distributions import build_all

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]


ARTEFACT = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*?)(?:-py3-none-any\.whl|\.tar\.gz)$")


def expected_workspace_distributions() -> set[str]:
    """Set of distribution names expected to exist based on filesystem layout."""
    dists = {"cliffracer"}
    for p in (ROOT / "packages").iterdir():
        if (p / "pyproject.toml").exists():
            dists.add(p.name.replace("-", "_"))
    return dists


def test_every_pyproject_configures_vcs_lockstep_version():
    """Verify all pyproject.toml files derive version dynamically from VCS."""
    pyprojects = [ROOT / "pyproject.toml"] + sorted(ROOT.glob("packages/*/pyproject.toml"))
    for pyproj in pyprojects:
        data = tomllib.loads(pyproj.read_text())
        project_table = data.get("project", {})
        assert "version" not in project_table, (
            f"{pyproj} declares a static version literal: {project_table.get('version')}"
        )
        assert "version" in project_table.get("dynamic", []), (
            f"{pyproj} does not list 'version' in project.dynamic"
        )
        hatch_version = data.get("tool", {}).get("hatch", {}).get("version", {})
        assert hatch_version.get("source") == "vcs", (
            f"{pyproj} does not configure tool.hatch.version.source = 'vcs'"
        )


def _build_into(out: Path, cwd: Path) -> list[tuple[str, str]]:
    """Build distributions and return a list of (distribution, version) tuples."""
    found = []
    for f in build_all(cwd, out):
        m = ARTEFACT.match(f.name)
        assert m, f"unrecognised artefact name: {f.name}"
        found.append((m.group("name"), m.group("version")))
    return found


@pytest.mark.slow
def test_every_built_artefact_carries_the_same_version(tmp_path):
    """Verify all workspace distributions build with identical VCS-derived version."""
    found = _build_into(tmp_path / "dist", ROOT)

    # Verify all workspace distributions built.
    built_distributions = {name.replace("-", "_") for name, _ in found}
    expected = expected_workspace_distributions()
    assert built_distributions == expected, (
        f"Built distributions do not match workspace packages: missing {expected - built_distributions}"
    )

    versions = {v for _, v in found}
    assert len(versions) == 1, f"workspace versions disagree: {sorted(found)}"

    # Verify version is derived from VCS rather than a static literal
    built_version = next(iter(versions))
    short_sha = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # The `+g<commit>` local segment is what shows the version came from the
    # repository. A `.dev` component does not: any literal may contain one.
    assert f"+g{short_sha}" in built_version, (
        f"Built version {built_version!r} carries no +g{short_sha} segment, so "
        "nothing in it shows it was derived from this commit"
    )


@pytest.mark.slow
def overlay_the_working_tree(source: Path, work: Path) -> None:
    """Make `work`, a checkout of HEAD, hold what `source`'s working tree holds.

    The control below plants its divergent package in a checkout, and `test_every_built_artefact_
    carries_the_same_version` builds the working tree: a checkout of HEAD alone differs from it by
    every uncommitted change (a narrowed workspace, say), so the control would be green about a
    configuration the test it controls is not using. Tracked files are overwritten, untracked ones
    that are not ignored are added, and a tracked file deleted in the working tree is removed.
    """

    def listed(*flags: str) -> list[str]:
        out = subprocess.run(
            ["git", "ls-files", "-z", *flags], cwd=source, capture_output=True, check=True
        ).stdout
        return [name.decode() for name in out.split(b"\0") if name]

    deleted = set(listed("--deleted"))
    for name in listed("--cached", "--others", "--exclude-standard"):
        if name in deleted:
            continue
        origin = source / name
        if origin.is_file() or origin.is_symlink():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).unlink(missing_ok=True)
            shutil.copy2(origin, work / name, follow_symlinks=False)
    for name in deleted:
        (work / name).unlink(missing_ok=True)


def test_CONTROL_the_overlay_carries_uncommitted_changes_into_the_checkout(tmp_path):
    """Modified, untracked and deleted files each reach the checkout; an ignored one does not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.invalid",
        "PATH": "/usr/bin:/bin",
    }

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True)

    git("init", "-q")
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "kept.txt").write_text("committed\n")
    (repo / "changed.txt").write_text("committed\n")
    (repo / "gone.txt").write_text("committed\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    work = tmp_path / "work"
    git("worktree", "add", "--detach", str(work), "HEAD")

    (repo / "changed.txt").write_text("edited\n")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "ignored.txt").write_text("ignored\n")
    (repo / "gone.txt").unlink()
    overlay_the_working_tree(repo, work)

    assert (work / "kept.txt").read_text() == "committed\n"
    assert (work / "changed.txt").read_text() == "edited\n"
    assert (work / "new.txt").read_text() == "untracked\n"
    assert not (work / "ignored.txt").exists()
    assert not (work / "gone.txt").exists()


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
        overlay_the_working_tree(ROOT, work)
        pkg = work / "packages" / "cliffracer-literal" / "src" / "cliffracer_literal"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (work / "packages" / "cliffracer-literal" / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
            '[project]\nname = "cliffracer-literal"\nversion = "9.9.9"\n'
            'requires-python = ">=3.12"\n\n'
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
