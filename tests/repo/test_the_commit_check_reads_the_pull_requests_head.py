"""On a pull request, the commit-message check measures from the pull request's head commit.

CI checks out a merge commit the forge made. A forge can make a new one and drop every ref to the
old, and then it no longer deepens the old one: a depth-1 checkout of it can never reach the base.
The workflows pass the head commit as `COMMIT_CHECK_HEAD`, a branch tip the forge always serves,
and the check measures `base..head` from it.

The checkout here is replayed from that case: origin holds the base branch, the pull request's
branch and a merge commit no ref points to, and the checkout holds only that merge commit, at
depth 1, with a fetch refspec that does not name the pull request's branch.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.repo.ci_workflows import ci_workflow_paths, load

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_commit_messages.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _replayed_checkout(tmp_path: Path, head_subject: str) -> tuple[Path, str]:
    """A checkout of a merge commit origin no longer references, and the pull request's head."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--initial-branch", "main")
    _git(origin, "config", "user.email", "ci@example.com")
    _git(origin, "config", "user.name", "CI")
    for n in range(3):
        (origin / f"base{n}.txt").write_text(f"{n}\n")
        _git(origin, "add", ".")
        _git(origin, "commit", "--quiet", "-m", f"feat: base commit {n}")
    _git(origin, "checkout", "--quiet", "-b", "feat/y")
    (origin / "head.txt").write_text("head\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "--quiet", "-m", head_subject)
    head = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "--quiet", "main")
    (origin / "later.txt").write_text("later\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "--quiet", "-m", "feat: a later base commit")
    _git(origin, "merge", "--quiet", "--no-ff", "feat/y", "-m", "Merge the pull request")
    merge = _git(origin, "rev-parse", "HEAD")
    # The forge's test merge, served while it is made. The forge then makes another and points
    # the pull request's merge ref at that one, so the first is referenced by nothing.
    _git(origin, "reset", "--quiet", "--hard", "HEAD~1")
    _git(origin, "merge", "--quiet", "--no-ff", "feat/y", "-m", "Merge the pull request again")
    _git(origin, "update-ref", "refs/pull/1/merge", "HEAD")
    _git(origin, "reset", "--quiet", "--hard", "HEAD~1")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet")
    _git(checkout, "remote", "add", "origin", origin.as_uri())
    _git(checkout, "config", "remote.origin.fetch", "+refs/pull/1/merge:refs/remotes/pull/1/merge")
    _git(checkout, "fetch", "--quiet", "--no-tags", "--depth=1", "origin", merge)
    _git(checkout, "checkout", "--quiet", "--force", merge)
    _git(origin, "config", "--unset", "uploadpack.allowAnySHA1InWant")
    return checkout, head


def _check(checkout: Path, head: str | None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_BASE_REF": "main",
        "COMMIT_CHECK_REPO": str(checkout),
    }
    env.pop("COMMIT_CHECK_HEAD", None)
    if head is not None:
        env["COMMIT_CHECK_HEAD"] = head
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True, text=True, env=env
    )


def test_given_the_head_it_reaches_the_base_a_dropped_merge_commit_cannot(tmp_path):
    checkout, head = _replayed_checkout(tmp_path, "feat: describe the code as it is")
    result = _check(checkout, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"1 commit message(s) in origin/main..{head}" in result.stdout, result.stdout


def test_without_the_head_it_fails_by_name_as_the_history_running_out(tmp_path):
    checkout, _ = _replayed_checkout(tmp_path, "feat: describe the code as it is")
    result = _check(checkout, None)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "the history ran out" in result.stderr, result.stderr


def test_from_the_head_it_still_reports_a_message_that_narrates_history(tmp_path):
    checkout, head = _replayed_checkout(tmp_path, "fix: the thing from #1234")
    result = _check(checkout, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[an issue or PR number]" in result.stdout, result.stdout


def test_a_head_the_remote_does_not_have_fails_by_name(tmp_path):
    checkout, _ = _replayed_checkout(tmp_path, "feat: describe the code as it is")
    result = _check(checkout, "0" * 40)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "the pull request's head commit could not be fetched" in result.stderr, result.stderr
    assert "COMMIT_CHECK_HEAD" in result.stderr, result.stderr


def test_every_pipeline_passes_the_pull_requests_head_to_the_check():
    paths = ci_workflow_paths()
    assert paths, "no CI pipeline found, so this guard read nothing"
    for _platform, path in paths:
        steps = load(path).get("jobs", {}).get("test", {}).get("steps", [])
        (step,) = [s for s in steps if "check_commit_messages.py" in str(s.get("run", ""))]
        given = step.get("env", {}).get("COMMIT_CHECK_HEAD")
        assert given == "${{ github.event.pull_request.head.sha }}", (
            f"{path.relative_to(REPO)} passes COMMIT_CHECK_HEAD={given!r} to the commit-message "
            "check; it measures from the pull request's head only when given it"
        )
