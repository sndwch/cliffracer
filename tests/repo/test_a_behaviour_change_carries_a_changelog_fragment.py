"""A pull request that changes src/ or packages/*/src/ adds a changelog fragment, or says why not.

Each case builds its own remote and checkout: a bare repository with a `main`,
and a clone on a branch cut from it. The check runs as CI runs it, as a
subprocess with the pull-request environment, against that checkout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_changelog_fragment.py"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=f", "-c", "user.email=f@f", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _commit(tree: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        assert _git("add", rel, cwd=tree).returncode == 0
    result = _git("commit", "-q", "-m", message, cwd=tree)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A clone on branch `change`, cut from a remote `main` with src, packages and docs."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=seed)
    _commit(
        seed,
        {
            "src/pkg/mod.py": "VALUE = 1\n",
            "packages/extra/src/extra/mod.py": "VALUE = 1\n",
            "packages/extra/tests/test_mod.py": "",
            "docs/guide.md": "# Guide\n",
        },
        "base",
    )
    bare = tmp_path / "remote.git"
    assert _git("clone", "--bare", "-q", str(seed), str(bare), cwd=tmp_path).returncode == 0
    work = tmp_path / "work"
    assert _git("clone", "-q", str(bare), str(work), cwd=tmp_path).returncode == 0
    assert _git("checkout", "-q", "-b", "change", cwd=work).returncode == 0
    return work


def _check(work: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_BASE_REF": "main",
        "COMMIT_CHECK_REPO": str(work),
        "COMMIT_CHECK_REMOTE": "origin",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def test_a_src_change_without_a_fragment_fails_and_names_the_file(checkout: Path):
    _commit(checkout, {"src/pkg/mod.py": "VALUE = 2\n"}, "Change the value")

    result = _check(checkout)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/pkg/mod.py" in result.stderr
    assert "Changelog: none" in result.stderr
    assert "same paragraph as any Co-Authored-By line" in result.stderr


def test_a_package_src_change_without_a_fragment_fails(checkout: Path):
    _commit(checkout, {"packages/extra/src/extra/mod.py": "VALUE = 2\n"}, "Change the value")

    result = _check(checkout)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "packages/extra/src/extra/mod.py" in result.stderr


def test_a_src_change_with_a_fragment_passes(checkout: Path):
    _commit(
        checkout,
        {
            "src/pkg/mod.py": "VALUE = 2\n",
            "changelog.d/the-value-is-two.md": "- The value is two.\n",
        },
        "Change the value",
    )

    result = _check(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "changelog.d/the-value-is-two.md" in result.stdout


def test_a_src_change_whose_commit_carries_the_opt_out_trailer_passes(checkout: Path):
    _commit(
        checkout,
        {"src/pkg/mod.py": "VALUE = 1  # the same value\n"},
        "Comment the value\n\nChangelog: none -- a comment only",
    )

    result = _check(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Changelog: none" in result.stdout


def test_a_change_outside_src_needs_no_fragment(checkout: Path):
    _commit(
        checkout,
        {"docs/guide.md": "# Guide\n\nMore.\n", "packages/extra/tests/test_mod.py": "# test\n"},
        "Expand the guide",
    )

    result = _check(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No change under src/" in result.stdout


def test_CONTROL_the_opt_out_written_in_a_message_body_is_not_a_trailer(checkout: Path):
    """A trailer is the last paragraph. The same words mid-message are prose."""
    _commit(
        checkout,
        {"src/pkg/mod.py": "VALUE = 2\n"},
        "Change the value\n\nChangelog: none -- would be a trailer\n\nbut this paragraph follows it.",
    )

    result = _check(checkout)

    assert result.returncode == 1, result.stdout + result.stderr


def test_CONTROL_a_changelog_trailer_whose_value_is_not_none_is_not_an_opt_out(checkout: Path):
    """A real trailer, read by git as one, whose value says something other than none."""
    _commit(
        checkout,
        {"src/pkg/mod.py": "VALUE = 2\n"},
        "Change the value\n\nChangelog: added",
    )
    trailer = _git(
        "log", "-1", "--format=%(trailers:key=Changelog,valueonly)", cwd=checkout
    ).stdout.strip()
    assert trailer == "added"

    result = _check(checkout)

    assert result.returncode == 1, result.stdout + result.stderr


def test_CONTROL_adding_the_fragment_readme_is_not_a_fragment(checkout: Path):
    """The base here has no changelog.d/, so the README is ADDED in this range,
    which is the case the exclusion exists for: an edit is not an add anyway."""
    _commit(
        checkout,
        {"src/pkg/mod.py": "VALUE = 2\n", "changelog.d/README.md": "# Fragments\n"},
        "Change the value",
    )

    result = _check(checkout)

    assert result.returncode == 1, result.stdout + result.stderr


def test_CONTROL_a_fragment_main_gained_does_not_count_for_this_branch(checkout: Path, tmp_path):
    """The range is this pull request's own. A fragment another change added to
    main, brought in by merging main, is not this change's entry."""
    other = tmp_path / "other"
    assert (
        _git("clone", "-q", str(tmp_path / "remote.git"), str(other), cwd=tmp_path).returncode == 0
    )
    _commit(other, {"changelog.d/someone-elses-change.md": "- Theirs.\n"}, "Their change")
    assert _git("push", "-q", "origin", "main", cwd=other).returncode == 0

    _commit(checkout, {"src/pkg/mod.py": "VALUE = 2\n"}, "Change the value")
    assert (
        _git("pull", "-q", "--no-rebase", "--no-edit", "origin", "main", cwd=checkout).returncode
        == 0
    )
    assert (checkout / "changelog.d" / "someone-elses-change.md").exists()

    result = _check(checkout)

    assert result.returncode == 1, result.stdout + result.stderr


def test_the_check_does_nothing_outside_a_pull_request(checkout: Path):
    _commit(checkout, {"src/pkg/mod.py": "VALUE = 2\n"}, "Change the value")
    env = {**os.environ, "GITHUB_EVENT_NAME": "push", "COMMIT_CHECK_REPO": str(checkout)}

    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode == 0
    assert "Not a pull request" in result.stdout
