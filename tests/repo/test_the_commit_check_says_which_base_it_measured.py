"""The commit check's success line names the range, the remote, and whether the fetch worked.

`fetch_base` judges the base by whether `<remote>/<base>` resolves and not by whether the fetch
succeeded, which is right in CI and leaves a working copy that already has the ref measuring a
STALE range when the fetch fails, and printing "N commit message(s) describe the code as it is"
with no word of it. The range is still measured; the line now says which ref, through which
remote, and that the fetch failed, so a green result cannot pass for a current one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_commit_messages.py"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _commit(tree: Path, message: str) -> None:
    (tree / "file.txt").write_text(message)
    _git("add", "file.txt", cwd=tree)
    result = _git(
        "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q",
        "-m", message, cwd=tree,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def _working_copy(tmp_path: Path, *, branch_commit: bool = True) -> Path:
    """A full clone of a bare remote, holding `origin/main`, with one commit on top if asked."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=seed)
    for n in range(3):
        _commit(seed, f"main commit {n}")
    bare = tmp_path / "remote.git"
    assert _git("clone", "--bare", "-q", str(seed), str(bare), cwd=tmp_path).returncode == 0
    copy = tmp_path / "copy"
    assert _git("clone", "-q", f"file://{bare}", str(copy), cwd=tmp_path).returncode == 0
    if branch_commit:
        _commit(copy, "a commit on the branch")
    return copy


def _run(copy: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "COMMIT_CHECK_REPO": str(copy),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_BASE_REF": "main",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def test_a_successful_fetch_is_named_in_the_success_line(tmp_path):
    copy = _working_copy(tmp_path)

    result = _run(copy)

    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert line.startswith("1 commit message(s) in origin/main..HEAD describe the code as it is")
    assert "remote 'origin'" in line and "base fetched" in line, line
    assert "FAILED" not in line and result.stderr == "", (line, result.stderr)


def test_a_failed_fetch_with_a_local_ref_still_measures_and_says_the_fetch_failed(tmp_path):
    copy = _working_copy(tmp_path)
    _git("remote", "set-url", "origin", f"file://{tmp_path}/not-a-remote.git", cwd=copy)

    result = _run(copy)

    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert "in origin/main..HEAD describe the code as it is" in line, line
    assert "remote 'origin'" in line and "not-a-remote.git" in line, line
    assert "the fetch FAILED, so the base is the local ref as it was" in line, line
    assert "Warning: the fetch of origin/main failed" in result.stderr, result.stderr


def test_an_empty_range_names_the_same_things(tmp_path):
    copy = _working_copy(tmp_path, branch_commit=False)
    _git("remote", "set-url", "origin", f"file://{tmp_path}/not-a-remote.git", cwd=copy)

    result = _run(copy)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("No commits in origin/main..HEAD (remote 'origin'")
    assert "the fetch FAILED" in result.stdout, result.stdout


def test_a_credential_in_the_remote_url_is_not_printed_by_the_failure_text(tmp_path):
    copy = _working_copy(tmp_path)
    _git("remote", "set-url", "origin", "https://user:s3cr3t-token@127.0.0.1:1/x.git", cwd=copy)

    result = _run(copy)

    assert result.returncode == 0, result.stderr
    assert "s3cr3t-token" not in result.stdout + result.stderr
    assert "***@127.0.0.1:1" in result.stdout, result.stdout


def test_a_base_that_cannot_resolve_at_all_is_still_a_refusal(tmp_path):
    """The success line's new honesty does not soften the refusal for no local ref."""
    copy = _working_copy(tmp_path)
    _git("remote", "set-url", "origin", f"file://{tmp_path}/not-a-remote.git", cwd=copy)
    _git("update-ref", "-d", "refs/remotes/origin/main", cwd=copy)

    result = _run(copy)

    assert result.returncode == 1
    assert "the base ref could not be fetched" in result.stderr, result.stderr


def _load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_commit_check_which_base", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_what_git_says_about_a_failed_fetch_is_returned_with_any_credential_removed():
    """Git strips a token from its own messages today; this is for the ones it does not."""
    module = _load_script()

    def fake_git(*args: str, **kwargs: object) -> _Result:
        if args[0] == "fetch":
            return _Result(1, stderr="fatal: unable to access 'https://s3cr3t-token@host/x.git/'")
        return _Result(0)  # the ref resolves and a merge base exists

    module._git = fake_git

    ref, code, detail = module.fetch_base("main")

    assert (ref, code) == ("origin/main", "")
    assert "s3cr3t-token" not in detail and "***@host" in detail, detail


def test_CONTROL_a_successful_fetch_returns_no_detail():
    module = _load_script()
    module._git = lambda *args, **kwargs: _Result(0)

    assert module.fetch_base("main") == ("origin/main", "", "")
