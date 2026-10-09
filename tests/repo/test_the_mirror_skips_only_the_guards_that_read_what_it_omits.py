"""On the GitHub mirror, a guard that reads a file the mirror omits is skipped, by one signal.

The mirror is a checkout with no `.gitea/` at its root. A `gitea_checkout` guard is skipped there,
with `MIRROR_REASON`. On a checkout that has `.gitea/`, the same guard runs, so a file it reads that
is missing fails it by name. Under Gitea Actions the checkout must never read as the mirror.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.repo.mirror import MIRROR_REASON, REPO, gitea_ci_on_the_mirror, is_mirror

pytestmark = pytest.mark.repo

#: A guard that reads AGENTS.md, which the mirror omits.
GUARD = "tests/repo/test_agents_md_names_the_gates.py::test_agents_md_exists"
#: A guard that is not marked and reads only pyproject.toml, which the mirror has.
UNMARKED = "tests/repo/test_dependency_lists_agree.py::test_the_two_dev_lists_are_identical"


def test_gitea_ci_is_never_the_mirror():
    if not os.environ.get("GITEA_ACTIONS"):
        pytest.skip("GITEA_ACTIONS is not set: this run is not on Gitea Actions")
    assert gitea_ci_on_the_mirror(dict(os.environ)) is None, gitea_ci_on_the_mirror(
        dict(os.environ)
    )


def test_a_root_without_gitea_is_the_mirror_and_one_with_it_is_not(tmp_path):
    assert is_mirror(tmp_path)
    (tmp_path / ".gitea").write_text("a file, not the directory")
    assert is_mirror(tmp_path)
    (tmp_path / ".gitea").unlink()
    (tmp_path / ".gitea").mkdir()
    assert not is_mirror(tmp_path)


def test_gitea_actions_without_gitea_fails_and_with_it_passes(tmp_path):
    on_gitea = {"GITEA_ACTIONS": "true"}
    reason = gitea_ci_on_the_mirror(on_gitea, tmp_path)
    assert reason is not None and "restore .gitea/" in reason, reason
    assert gitea_ci_on_the_mirror({}, tmp_path) is None
    (tmp_path / ".gitea").mkdir()
    assert gitea_ci_on_the_mirror(on_gitea, tmp_path) is None


def _checkout(tmp_path: Path, *, gitea: bool) -> Path:
    """A checkout holding the suite and no AGENTS.md, with or without `.gitea/`."""
    root = tmp_path / "checkout"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(REPO / "tests", root / "tests", ignore=ignore)
    for name in ("conftest.py", "pyproject.toml"):
        shutil.copy(REPO / name, root / name)
    (root / ".git").mkdir()
    if gitea:
        shutil.copytree(REPO / ".gitea", root / ".gitea")
    return root


def _run(root: Path, guard: str = GUARD) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", guard, "-p", "no:cacheprovider", "-rs", "-q"],
        cwd=root,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"},
        timeout=120,
    )
    return result.stdout + result.stderr


def test_on_the_mirror_a_guard_reading_an_omitted_file_is_skipped_with_the_reason(tmp_path):
    out = _run(_checkout(tmp_path, gitea=False))
    assert "1 skipped" in out and MIRROR_REASON in out, out[-3000:]


@pytest.mark.gitea_checkout  # it copies this checkout's .gitea/
def test_off_the_mirror_the_same_guard_fails_by_name_on_the_missing_file(tmp_path):
    out = _run(_checkout(tmp_path, gitea=True))
    assert "1 failed" in out and "AGENTS.md is missing" in out, out[-3000:]


def test_on_the_mirror_a_guard_that_is_not_marked_still_runs_and_passes(tmp_path):
    out = _run(_checkout(tmp_path, gitea=False), UNMARKED)
    assert f"{UNMARKED} PASSED" in out and "= 1 passed in" in out, out[-3000:]
