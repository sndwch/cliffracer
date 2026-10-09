"""Shared fixtures for the repository guards.

The git check lives here, once. Nine modules carried a copy of it, and a copy
per file is how the check came to disagree with itself: a fix applied to one
spelling left the others skipping silently. One definition cannot drift from
itself, and `test_no_repo_guard_defines_its_own_git_fixture` stops a copy
coming back one file at a time.
"""

from pathlib import Path

import pytest

from tests.repo.mirror import MIRROR_REASON, is_mirror

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    """Skip in unpacked release tarballs where .git is absent.

    Skipping when .git does not exist is intentional by design so users
    running tests from an unpacked release tarball can run the suite.
    In git checkouts and worktrees, .git exists and guards run: in a worktree
    it is a pointer file, which is why this asks whether it exists rather than
    whether it is a directory.
    """
    if not (REPO / ".git").exists():
        pytest.skip("Not running inside a git repository (release tarball)")


@pytest.fixture(autouse=True)
def _skip_a_gitea_guard_on_the_mirror(request):
    """Skip a guard marked `gitea_checkout` on the GitHub mirror, which omits what it reads.

    `tests/repo/mirror.py` says what the mirror is and why the signal is its missing `.gitea/`,
    never the missing file itself.
    """
    if request.node.get_closest_marker("gitea_checkout") and is_mirror():
        pytest.skip(MIRROR_REASON)
