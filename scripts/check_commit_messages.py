"""Fail a pull request whose commit messages narrate history.

A commit message says what the code does now. An issue number, a commit hash or
a sentence about why the author changed their mind belongs in the pull request,
where it is read once, rather than in the log, where it is read forever.

The patterns are the ones the documentation guard owns; this imports them so
there is no second copy to drift. Only the labels that apply to prose about
code are used, and `tests/repo/test_source_carries_no_history.py` asserts that
this selection and the source sweep's agree.

Runs on pull requests only. The gate is here rather than on the workflow step
so the step stays unconditional, which is what the CI-gate guard requires.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# How far to deepen before giving up looking for the merge base. CI checks out
# at depth 1, so the history has to be pulled down in steps.
#
# The side that needs deepening is the HEAD, not the base. A depth-1 checkout
# grafts HEAD in `.git/shallow`, which leaves it with no parents, so no commit
# can be its common ancestor -- while an unrestricted fetch of the base ref
# brings that ref's history down in full. So the deepen loop is pulling the
# head's ancestry toward a base that is already complete.
DEEPEN_STEP = 50
MAX_DEEPEN = 20

# A deepen fetch that fails is retried, because the reason is usually transient
# and the alternative is failing a pull request for a network blip. Retried
# immediately: nothing here waits on a rate limit, and a run that needs a
# backoff wants a human to look rather than a longer sleep.
DEEPEN_RETRIES = 3

# Why the commit range could not be determined. These are separate because the
# remedies are: a fetch failure is worth retrying, exhausted history wants a
# rebase, an unfetchable base is a permissions or naming problem, and unrelated
# histories mean the branch was cut from a different root. One sentinel for all
# four sent two sessions looking for a history-distance problem that was not
# there.
UNRESOLVED_FETCH_FAILED = "the deepen fetch failed"
UNRESOLVED_HISTORY_EXHAUSTED = "the history ran out"
UNRESOLVED_BASE_UNFETCHABLE = "the base ref could not be fetched"
UNRESOLVED_UNRELATED = "the histories are unrelated"
UNRESOLVED_HEAD_UNFETCHABLE = "the pull request's head commit could not be fetched"


def git_root() -> Path:
    """The tree git commands run against, overridable so this can be tested."""
    return Path(os.environ.get("COMMIT_CHECK_REPO", REPO))


def remote_name() -> str:
    """The remote the base ref is fetched from.

    `origin` in CI, where the checkout has one remote and it is this
    repository. A working copy can have several -- a forge and a mirror -- and
    then `origin` may be a different repository with a different history, which
    this cannot detect and must therefore be able to say.
    """
    return os.environ.get("COMMIT_CHECK_REMOTE", "origin")


def _redacted(url: str) -> str:
    """A remote URL with any credentials removed.

    A push URL can carry a token in the userinfo -- `https://user:token@host/x`
    or `https://token@host/x` -- and this URL is printed in a refusal that
    lands in a job log. The host and path are what identify the repository; the
    userinfo never is.
    """
    return re.sub(r"(?<=//)[^/@]+@", "***@", url)


def remote_url(remote: str) -> str:
    """The remote's URL, redacted, or a phrase saying it has none."""
    found = _git("remote", "get-url", remote)
    if found.returncode != 0 or not found.stdout.strip():
        return "no such remote in this checkout"
    return _redacted(found.stdout.strip())


def head_commit() -> str:
    """The commit the range ends at: `COMMIT_CHECK_HEAD`, else `HEAD`.

    A pull request's CI checks out a merge commit the forge made, which is not one of the pull
    request's commits. And a forge can make a new one and drop every ref to the old, after which
    it no longer deepens the old one, so its history cannot be reached at all. The workflows pass
    the pull request's head commit instead. That is a branch tip, so the forge always serves it,
    and `base..head` is exactly the pull request's commits.
    """
    return os.environ.get("COMMIT_CHECK_HEAD", "").strip() or "HEAD"


def base_ref_name(base: str) -> str:
    """The remote-tracking ref a base branch name resolves to.

    `removeprefix` on the remote's own name, so passing either `main` or
    `origin/main` names the same ref. Not a split on `/`: a branch may contain
    one (`release/9.9`), and splitting would turn that into a remote called
    `release`.
    """
    remote = remote_name()
    return f"{remote}/{base.removeprefix(f'{remote}/')}"


# The labels from the documentation pattern set that apply to a commit message.
HISTORY_LABELS = (
    "an issue or PR number",
    "a commit SHA",
    "a 1.x comparison",
    "release narration",
    "rationale narration",
)


class PatternsUnavailable(Exception):
    """The pattern set could not be imported, so no verdict is possible."""


def patterns() -> dict[str, re.Pattern[str]]:
    """Return the documentation guard's pattern set.

    Imported rather than copied so there is no second definition to drift. That
    module imports pytest, which means this script only runs where the project's
    test dependencies are installed -- true of CI, which runs it under `uv run`,
    and not true of a bare `python scripts/check_commit_messages.py`. Raised as
    its own exception so that case reports what happened instead of a traceback:
    a gate that dies unexplained is indistinguishable from a broken gate.
    """
    sys.path.insert(0, str(REPO))
    try:
        from tests.repo.test_docs_carry_no_history import PATTERNS
    except ImportError as exc:
        raise PatternsUnavailable(
            f"could not import the documentation guard's patterns ({exc}). This script "
            f"needs the project's test dependencies; CI runs it under `uv run`."
        ) from exc

    return PATTERNS


def number_way_out() -> str:
    """The documentation guard's advice for a number the rule reads as an issue.

    Imported like the patterns, so every guard gives the same advice. Called
    only after `patterns()` has imported the module, so it cannot fail here.
    """
    from tests.repo.test_docs_carry_no_history import NUMBER_WAY_OUT

    return NUMBER_WAY_OUT


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(git_root()), *args], capture_output=True, text=True, check=check
    )


def fetch_base(base: str, head: str = "HEAD") -> tuple[str | None, str, str]:
    """Fetch the base branch and deepen until the merge base resolves.

    `actions/checkout` fetches only the head commit, at depth 1, and narrows the
    remote's fetch refspec to that ref. The base branch is not fetched at all,
    so this fetches it explicitly -- without `--depth`, which brings its full
    history -- and then deepens the shallow side until the two share an
    ancestor.

    Returns `(ref, code, detail)`. On success `code` is empty and `detail` is
    empty too when this run's fetch of the base succeeded, and otherwise says
    what git said when it did not: the ref that resolved is then whatever the
    working copy already had. Otherwise `ref` is None and `code` is one of the
    UNRESOLVED_* reasons, with `detail` carrying whatever git said. The caller
    reports which one, because they have different remedies and a single
    "cannot resolve" hides that.

    A `head` other than `HEAD` is fetched by its sha first, and the range is measured from it.
    """
    remote = remote_name()
    ref = base_ref_name(base)
    remote_branch = base.removeprefix(f"{remote}/")
    fetched = _git(
        "fetch",
        "--no-tags",
        "--quiet",
        remote,
        f"+refs/heads/{remote_branch}:refs/remotes/{ref}",
    )

    # Judged on whether the ref resolves, not on the fetch's exit code: a run
    # whose base is already local does not need this fetch to succeed.
    if _git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode != 0:
        return None, UNRESOLVED_BASE_UNFETCHABLE, (fetched.stderr or fetched.stdout).strip()

    # That is right in CI, which has no local ref to fall back on, and it leaves a gap in a
    # working copy that has one: a failed fetch leaves the ref as it was, and the range measured
    # is then a stale one. The range is still measured, and the caller says it was.
    stale = (
        ""
        if fetched.returncode == 0
        else _redacted((fetched.stderr or fetched.stdout).strip() or "the fetch exited non-zero")
    )

    if head != "HEAD":
        got = _git("fetch", "--no-tags", "--quiet", remote, head)
        if _git("rev-parse", "--verify", f"{head}^{{commit}}").returncode != 0:
            return None, UNRESOLVED_HEAD_UNFETCHABLE, (got.stderr or got.stdout).strip()

    for step in range(1, MAX_DEEPEN + 1):
        if _git("merge-base", ref, head).returncode == 0:
            return ref, "", stale

        if _git("rev-parse", "--is-shallow-repository").stdout.strip() != "true":
            # Nothing left to deepen and still no common ancestor: this branch
            # was not cut from the base's history.
            return None, UNRESOLVED_UNRELATED, f"{ref} and {head} share no commit"

        last = None
        for _attempt in range(1, DEEPEN_RETRIES + 1):
            last = _git("fetch", "--no-tags", "--quiet", f"--deepen={DEEPEN_STEP}", remote)
            if last.returncode == 0:
                break
        if last is None or last.returncode != 0:
            return (
                None,
                UNRESOLVED_FETCH_FAILED,
                f"{DEEPEN_RETRIES} attempt(s) at deepen step {step} of {MAX_DEEPEN} "
                f"all failed: {(last.stderr or last.stdout).strip() if last else 'no attempt'}",
            )

    return (
        None,
        UNRESOLVED_HISTORY_EXHAUSTED,
        f"deepened {MAX_DEEPEN} times by {DEEPEN_STEP} commits without reaching a "
        f"common ancestor of {ref} and {head}",
    )


def commit_messages(base: str, head: str) -> list[tuple[str, str]]:
    """Return (short sha, message) for each commit in `base..head`.

    Merges are excluded with --no-merges. A forge writes its own merge subject,
    and it names the pull request by number: flagging that would fail every
    branch that has a merge in its range for text no author typed.
    """
    out = _git("log", "--no-merges", "--format=%H%x00%B%x1e", f"{base}..{head}", check=True).stdout
    found = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, body = record.partition("\x00")
        found.append((sha[:9], body))
    return found


def offending_lines(messages: list[tuple[str, str]]) -> list[str]:
    """Return `sha: [label] line` for every message line that narrates history."""
    pats = patterns()
    bad = []
    for sha, body in messages:
        for line in body.splitlines():
            if line.startswith(("Co-Authored-By:", "Claude-Session:", "BREAKING CHANGE:")):
                continue
            for label in HISTORY_LABELS:
                if pats[label].search(line):
                    bad.append(f"{sha}: [{label}] {line.strip()[:100]}")
    return bad


def how_the_range_was_measured(stale: str) -> str:
    """The remote the base came through and whether this run's fetch of it worked.

    A success line that names neither lets a stale range read as a current one.
    """
    remote = remote_name()
    through = f"remote {remote!r} ({remote_url(remote)})"
    if not stale:
        return f"{through}; base fetched"
    first = stale.splitlines()[0]
    return f"{through}; the fetch FAILED, so the base is the local ref as it was: {first}"


def main(argv: list[str]) -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "pull_request":
        print(f"Not a pull request (event {event!r}); nothing to check.")
        return 0

    base = argv[1] if len(argv) > 1 else os.environ.get("GITHUB_BASE_REF", "")
    if not base:
        print("No base ref given; nothing to check.")
        return 0

    head = head_commit()
    base_ref, unresolved, detail = fetch_base(base, head)
    if base_ref is None:
        remote = remote_name()
        print(
            f"Cannot determine the commits in this pull request, so the messages are "
            f"not being reported as clean. {unresolved}: {detail}",
            file=sys.stderr,
        )
        # WHICH remote and WHICH ref, on every refusal. A checkout with more
        # than one remote can have an `origin` that is a different repository
        # altogether, and then every reason above is reported accurately about
        # the wrong pair of refs -- "the histories are unrelated" is true of
        # that origin and says nothing about the branch. The reader cannot see
        # this from the reason, so it is stated rather than left to be guessed.
        print(
            f"Resolved the base {base!r} as {base_ref_name(base)}, from remote "
            f"{remote!r} ({remote_url(remote)}). Set COMMIT_CHECK_REMOTE to name "
            f"a different remote.",
            file=sys.stderr,
        )
        if unresolved == UNRESOLVED_FETCH_FAILED:
            print(
                "This is usually transient. Re-run the job; if it repeats, the runner "
                "cannot reach the remote and no rebase will help.",
                file=sys.stderr,
            )
        elif unresolved == UNRESOLVED_HISTORY_EXHAUSTED:
            print(f"Rebase this branch onto {base!r} and push again.", file=sys.stderr)
        elif unresolved == UNRESOLVED_HEAD_UNFETCHABLE:
            print(
                f"Check that COMMIT_CHECK_HEAD ({head!r}) is the pull request's head commit and "
                f"that the job's token can read it.",
                file=sys.stderr,
            )
        elif unresolved == UNRESOLVED_BASE_UNFETCHABLE:
            print(
                f"Check that {base!r} exists on the remote and that the job's token can read it.",
                file=sys.stderr,
            )
        else:
            print(
                f"Rebase or recreate this branch on top of {base!r}; it does not "
                f"descend from it. If it should, check that the remote named above "
                f"is the repository you mean.",
                file=sys.stderr,
            )
        return 1

    try:
        messages = commit_messages(base_ref, head)
    except subprocess.CalledProcessError as exc:
        print(f"Could not read {base_ref}..{head}: {exc.stderr}", file=sys.stderr)
        return 1

    measured = how_the_range_was_measured(detail)
    if detail:
        print(
            f"Warning: the fetch of {base_ref} failed, so {base_ref}..{head} is measured against "
            f"the local ref as it was and may include commits already on the base.",
            file=sys.stderr,
        )

    if not messages:
        print(f"No commits in {base_ref}..{head} ({measured}).")
        return 0

    try:
        bad = offending_lines(messages)
    except PatternsUnavailable as exc:
        print(f"Not reporting the messages as clean: {exc}", file=sys.stderr)
        return 1

    if bad:
        print(f"{len(bad)} commit message line(s) narrate history rather than describe the code:")
        for line in bad:
            print(f"  {line}")
        print("\nMove an issue number and the reasoning to the pull request body.")
        print(number_way_out())
        return 1

    print(
        f"{len(messages)} commit message(s) in {base_ref}..{head} describe the code as it is ({measured})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
