"""When the commit-message check cannot determine the range, it says which cause.

The check refuses rather than reporting clean when it cannot resolve
`base..HEAD` -- that part is right. But it used to print one sentence for four
different causes, and the sentence named the one that is usually wrong:

    Cannot resolve 'main' against HEAD after deepening, so the commit range is
    unknown.

A pull request hit that with a one-commit branch 28 behind main. The distance
was not the cause: a later push of the same branch, same merge base, same
distance, passed. The cause was a deepen fetch that failed, and "after
deepening" reads as a history-distance problem, so the measurement went after
distance first.

EVERY FIXTURE HERE IS ITS OWN CHECKOUT. The first version of this measurement
reused one working copy across cases, and case two passed because case one had
already deepened it -- a probe that had destroyed its own evidence.

The remote is a local bare repository, which allows fetching a SHA by name.
That is looser than a forge with `uploadpack.allowReachableSHA1InWant` off, so
these tests do not speak for that configuration.
"""

from __future__ import annotations

import importlib.util
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
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@localhost",
        "commit",
        "-q",
        "-m",
        message,
        cwd=tree,
    )
    assert result.returncode == 0, result.stderr


def _remote(tmp_path: Path, *, main_commits: int = 8, behind: int = 6) -> Path:
    """A bare remote with a main branch and a one-commit branch `behind` back.

    Built rather than cloned from this repository, so the fixture does not
    depend on how far behind anything happens to be today.
    """
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=work)
    for n in range(main_commits):
        _commit(work, f"main commit {n}")
    base = _git("rev-parse", f"HEAD~{behind}", cwd=work).stdout.strip()
    _git("checkout", "-q", "-b", "stale", base, cwd=work)
    _commit(work, "the one commit on the stale branch")
    _git("checkout", "-q", "main", cwd=work)

    bare = tmp_path / "remote.git"
    assert _git("clone", "--bare", "-q", str(work), str(bare), cwd=tmp_path).returncode == 0
    return bare


def _depth_one_checkout(tmp_path: Path, remote: Path, name: str, branch: str = "stale") -> Path:
    """A checkout the way `actions/checkout@v4` leaves one at fetch-depth: 1."""
    checkout = tmp_path / name
    checkout.mkdir()
    _git("init", "-q", ".", cwd=checkout)
    _git("remote", "add", "origin", f"file://{remote}", cwd=checkout)
    # checkout@v4 narrows the remote's fetch refspec to the ref it checked out
    _git(
        "config",
        "remote.origin.fetch",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        cwd=checkout,
    )
    _git(
        "fetch",
        "--no-tags",
        "--prune",
        "--depth=1",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        cwd=checkout,
    )
    _git("checkout", "-q", "--detach", f"refs/remotes/origin/{branch}", cwd=checkout)

    assert _git("rev-parse", "--is-shallow-repository", cwd=checkout).stdout.strip() == "true"
    assert _git("rev-list", "--count", "HEAD", cwd=checkout).stdout.strip() == "1", (
        "the fixture is not actually shallow, so nothing below measures deepening"
    )
    return checkout


def _run(
    checkout: Path, base: str = "main", remote: str | None = None
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "COMMIT_CHECK_REPO": str(checkout),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_BASE_REF": base,
    }
    if remote is not None:
        env["COMMIT_CHECK_REMOTE"] = remote
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def _break_the_remote(checkout: Path, tmp_path: Path) -> None:
    _git("remote", "set-url", "origin", f"file://{tmp_path}/not-a-remote.git", cwd=checkout)


# --- the resolvable case, so the failures below mean something ----------------


def test_a_stale_one_commit_branch_resolves(tmp_path):
    """One deepen step reaches a merge base six commits back.

    This is the case the original report thought was failing. It passes, which
    is why the acceptance test for the fix had to be written against a failing
    fetch instead of against distance.
    """
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "resolvable")

    result = _run(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "describe the code as it is" in result.stdout, result.stdout


# --- each cause names itself --------------------------------------------------


def test_a_failing_deepen_says_the_fetch_failed_and_how_many_tries(tmp_path):
    """The cause that actually fired on the pull request.

    The base is already local and only the deepen breaks, which is the state a
    run is in when the network blips mid-job.
    """
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "deepen-fails")
    _git(
        "fetch",
        "--no-tags",
        "--quiet",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
        cwd=checkout,
    )
    _break_the_remote(checkout, tmp_path)

    result = _run(checkout)

    assert result.returncode == 1, result.stderr
    assert "the deepen fetch failed" in result.stderr, result.stderr
    assert "attempt(s) at deepen step" in result.stderr, result.stderr
    assert "usually transient" in result.stderr, result.stderr
    assert "Re-run the job" in result.stderr, result.stderr
    # and it must not send the reader after the cause that did not fire
    assert "Rebase" not in result.stderr, result.stderr
    assert "the history ran out" not in result.stderr, result.stderr


def test_an_unfetchable_base_says_so(tmp_path):
    """A base branch that does not exist on the remote is not a distance problem."""
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "no-base")

    result = _run(checkout, base="release/9.9")

    assert result.returncode == 1, result.stderr
    assert "the base ref could not be fetched" in result.stderr, result.stderr
    assert "exists on the remote" in result.stderr, result.stderr
    assert "the history ran out" not in result.stderr, result.stderr


def test_unrelated_histories_say_so(tmp_path):
    """A branch cut from a different root deepens to the end and still fails.

    Distinguished from exhausted history by the repository no longer being
    shallow: there is nothing left to pull, so more deepening cannot help.
    """
    remote = _remote(tmp_path)
    # an orphan branch on the same remote, sharing no commit with main
    seed = tmp_path / "seed"
    _git("checkout", "-q", "--orphan", "orphan", cwd=seed)
    _git("rm", "-q", "-rf", ".", cwd=seed)
    _commit(seed, "an unrelated root")
    assert _git("push", "-q", str(remote), "orphan", cwd=seed).returncode == 0

    checkout = _depth_one_checkout(tmp_path, remote, "unrelated", branch="orphan")

    result = _run(checkout)

    assert result.returncode == 1, result.stderr
    assert "the histories are unrelated" in result.stderr, result.stderr
    assert "does not descend from it" in result.stderr, result.stderr


def test_exhausted_history_says_rebase(tmp_path):
    """Tested against `fetch_base` directly, because the real bound is 1000 commits.

    `MAX_DEEPEN * DEEPEN_STEP` is 20 x 50, so producing this through the CLI
    would need a thousand-commit fixture. Patching the two constants on an
    imported copy asks the same question of the same code.
    """
    spec = importlib.util.spec_from_file_location("commit_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "exhausted")

    module.DEEPEN_STEP = 1
    module.MAX_DEEPEN = 1
    os.environ["COMMIT_CHECK_REPO"] = str(checkout)
    try:
        ref, code, detail = module.fetch_base("main")
    finally:
        os.environ.pop("COMMIT_CHECK_REPO", None)

    assert ref is None
    assert code == module.UNRESOLVED_HISTORY_EXHAUSTED, (code, detail)
    assert "deepened 1 times by 1 commits" in detail, detail


# --- controls -----------------------------------------------------------------


def test_CONTROL_the_four_reasons_are_distinct_strings():
    """Four causes reported by one sentence is the defect this fixes.

    If two of these ever collapse to the same text, the tests above still pass
    while the distinction they exist for is gone.
    """
    spec = importlib.util.spec_from_file_location("commit_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    reasons = {
        module.UNRESOLVED_FETCH_FAILED,
        module.UNRESOLVED_HISTORY_EXHAUSTED,
        module.UNRESOLVED_BASE_UNFETCHABLE,
        module.UNRESOLVED_UNRELATED,
    }

    assert len(reasons) == 4, reasons


def test_CONTROL_the_old_single_sentence_is_gone(tmp_path):
    """The text that named the wrong cause must not survive anywhere.

    Asserted against the source rather than an outcome, because it was the
    wording that misdirected the investigation.
    """
    source = SCRIPT.read_text()

    # A substring that exists on ONE source line. The first version of this
    # control asked for "after deepening, so the commit range is unknown",
    # which spans a string concatenation and so appears nowhere in the file --
    # it passed against the very version it was meant to reject.
    assert "against HEAD after deepening" not in source


def test_a_missing_pattern_set_refuses_instead_of_tracebacking(tmp_path):
    """The patterns come from a test module, which imports pytest.

    So a bare `python scripts/check_commit_messages.py` outside the project's
    environment cannot reach them. It must say so and refuse, not die with a
    `ModuleNotFoundError` traceback -- a gate that dies unexplained reads as a
    broken gate rather than an unanswered question.

    Arranged by copying the script into a tree that has no `tests/` directory,
    because the script derives its repo root from its own location. An
    in-process fake cannot do this faithfully: the pattern module IS importable
    inside a pytest run, and blocking it with a meta-path finder did not fire
    under `--import-mode=importlib`, which is what this suite uses.
    """
    lonely = tmp_path / "lonely" / "scripts"
    lonely.mkdir(parents=True)
    copied = lonely / SCRIPT.name
    copied.write_text(SCRIPT.read_text())

    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "patterns-missing")

    env = {
        **os.environ,
        "COMMIT_CHECK_REPO": str(checkout),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_BASE_REF": "main",
    }
    result = subprocess.run(
        [sys.executable, str(copied)], capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not reporting the messages as clean" in result.stderr, result.stderr
    assert "test dependencies" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_CONTROL_the_pattern_set_is_importable_here(tmp_path):
    """Otherwise the test above would pass for the wrong reason.

    It asserts a failure path; this asserts the path is not failing by default,
    so the blocker above is what caused it.
    """
    spec = importlib.util.spec_from_file_location("commit_check_patterns_ok", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    found = module.patterns()

    assert found, "the pattern set is empty"
    for label in module.HISTORY_LABELS:
        assert label in found, f"{label!r} is selected but not in the pattern set"


# --- which remote, and which ref ---------------------------------------------
#
# The reason the refusal gives is about a PAIR OF REFS, and a checkout can have
# more than one remote. A working copy with a forge and a mirror can have an
# `origin` that is a different repository with an unrelated history -- and then
# every reason above is reported accurately about refs the reader did not mean.
# "the histories are unrelated" is true of that origin and says nothing about
# the branch, and the advice that follows it ("rebase") is actionable and wrong.


def _two_remotes(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A checkout whose `origin` is an unrelated repository.

    `upstream` is the one the branch came from. This is the shape of a working
    copy that has both a forge and a public mirror, which is where this was
    found.
    """
    real = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, real, "two-remotes")
    _git("remote", "rename", "origin", "upstream", cwd=checkout)

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=unrelated)
    _commit(unrelated, "a root this branch never shared")
    bare_unrelated = tmp_path / "unrelated.git"
    assert (
        _git("clone", "--bare", "-q", str(unrelated), str(bare_unrelated), cwd=tmp_path).returncode
        == 0
    )
    _git("remote", "add", "origin", f"file://{bare_unrelated}", cwd=checkout)
    return checkout, real, bare_unrelated


def test_the_refusal_names_the_remote_and_the_ref_it_resolved(tmp_path):
    """Without this the reader cannot tell a stale branch from a wrong remote."""
    checkout, _, bare_unrelated = _two_remotes(tmp_path)

    result = _run(checkout)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "origin/main" in result.stderr, result.stderr
    assert "'origin'" in result.stderr, result.stderr
    assert str(bare_unrelated) in result.stderr, result.stderr
    assert "COMMIT_CHECK_REMOTE" in result.stderr, result.stderr


def test_naming_the_right_remote_resolves_the_same_checkout(tmp_path):
    """The other half: the refusal above is about the remote, not the branch.

    The same tree, the same branch, the same base name -- only the remote
    differs -- and the check reports clean. Without this pair, the test above
    would pass on a script that refused everything.
    """
    checkout, _, _ = _two_remotes(tmp_path)

    refused = _run(checkout)
    resolved = _run(checkout, remote="upstream")

    assert refused.returncode == 1, refused.stderr
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    assert "describe the code as it is" in resolved.stdout, resolved.stdout


def test_a_credential_in_the_remote_url_is_not_printed(tmp_path):
    """This URL goes into a job log. A push URL can carry a token in its
    userinfo, and the host and path are what identify a repository -- the
    userinfo never is."""
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "credentialed")
    _git(
        "remote",
        "set-url",
        "origin",
        "https://claude-1:s3cr3t-token@forge.example/org/repo.git",
        cwd=checkout,
    )

    result = _run(checkout)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "s3cr3t-token" not in result.stderr, result.stderr
    assert "claude-1" not in result.stderr, result.stderr
    assert "***@forge.example/org/repo.git" in result.stderr, result.stderr


def test_CONTROL_the_default_remote_is_still_origin(tmp_path):
    """CI has one remote called `origin` and must not need the variable."""
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "default-remote")

    result = _run(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "describe the code as it is" in result.stdout, result.stdout


def test_CONTROL_a_branch_name_with_a_slash_is_not_read_as_a_remote(tmp_path):
    """`release/9.9` contains a slash, so a split on `/` would look for a
    remote called `release`. The prefix stripped is the remote's own name."""
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "slashed-base")

    result = _run(checkout, base="release/9.9")

    assert result.returncode == 1, result.stderr
    assert "origin/release/9.9" in result.stderr, result.stderr
    assert "'origin'" in result.stderr, result.stderr


def test_CONTROL_a_remote_that_does_not_exist_says_so_rather_than_crashing(tmp_path):
    """A mistyped COMMIT_CHECK_REMOTE must report what it could not find."""
    remote = _remote(tmp_path)
    checkout = _depth_one_checkout(tmp_path, remote, "no-such-remote")

    result = _run(checkout, remote="typo")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "'typo'" in result.stderr, result.stderr
    assert "no such remote" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
