"""Fail a pull request that changes behaviour without a changelog fragment.

A change to `src/` or `packages/*/src/` is something an upgrader may need to
know, so the pull request adds a fragment under `changelog.d/` for it, in the
same pull request. A change that no upgrader needs to hear about -- a refactor,
a comment, an internal rename -- says so with a trailer on one of its commits:

    Changelog: none -- the rename is internal

The trailer is read as git reads trailers, from the last paragraph of a
commit message, so the same words written in a message's body do not count.

The range is the pull request's own: the commits from the merge base with the
base branch to HEAD, resolved exactly as `check_commit_messages.py` resolves it.
A fragment the base branch gained and a merge brought in is not in that range
and does not count for this pull request.

Runs on pull requests only. The gate is here rather than on the workflow step,
so the step stays unconditional, as the CI-gate guard requires.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_commit_messages import _git, base_ref_name, fetch_base, remote_name  # noqa: E402

FRAGMENT_DIR = "changelog.d"
BEHAVIOUR = re.compile(r"^(?:src/|packages/[^/]+/src/)")
OPT_OUT = re.compile(r"^none\b", re.I)


def changed_files(base: str) -> list[str]:
    """Every path this pull request changes: merge base with `base` to HEAD."""
    out = _git("diff", "--name-only", f"{base}...HEAD", check=True).stdout
    return [line for line in out.splitlines() if line]


def added_fragments(base: str) -> list[str]:
    """Fragments this pull request adds. The directory's README is not one."""
    out = _git(
        "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD", "--", FRAGMENT_DIR, check=True
    ).stdout
    return [line for line in out.splitlines() if line and not line.endswith("/README.md")]


def opt_out_commits(base: str) -> list[str]:
    """Short SHAs of the non-merge commits in range carrying `Changelog: none`."""
    merge_base = _git("merge-base", base, "HEAD", check=True).stdout.strip()
    out = _git(
        "log",
        "--no-merges",
        "--format=%H%x00%(trailers:key=Changelog,valueonly,separator=%x1f)%x1e",
        f"{merge_base}..HEAD",
        check=True,
    ).stdout
    found = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, values = record.partition("\x00")
        if any(OPT_OUT.match(value.strip()) for value in values.split("\x1f")):
            found.append(sha[:9])
    return found


def main(argv: list[str]) -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "pull_request":
        print(f"Not a pull request (event {event!r}); nothing to check.")
        return 0

    base = argv[1] if len(argv) > 1 else os.environ.get("GITHUB_BASE_REF", "")
    if not base:
        print("No base ref given; nothing to check.")
        return 0

    base_ref, unresolved, detail = fetch_base(base)
    if base_ref is None:
        print(
            f"Cannot determine what this pull request changes, so it is not being "
            f"reported as needing no changelog fragment. {unresolved}: {detail}. "
            f"Resolved the base {base!r} as {base_ref_name(base)} from remote "
            f"{remote_name()!r}; set COMMIT_CHECK_REMOTE to name a different remote.",
            file=sys.stderr,
        )
        return 1

    behaviour = [path for path in changed_files(base_ref) if BEHAVIOUR.match(path)]
    if not behaviour:
        print("No change under src/ or packages/*/src/; no changelog fragment is needed.")
        return 0

    fragments = added_fragments(base_ref)
    if fragments:
        print(f"Changelog fragment(s) added: {', '.join(fragments)}.")
        return 0

    opted_out = opt_out_commits(base_ref)
    if opted_out:
        print(f"No fragment, and commit(s) {', '.join(opted_out)} carry 'Changelog: none'.")
        return 0

    shown = behaviour[:5] + ([f"... and {len(behaviour) - 5} more"] if len(behaviour) > 5 else [])
    print(
        f"This pull request changes {len(behaviour)} file(s) under src/ or "
        f"packages/*/src/ and adds no changelog fragment:",
        file=sys.stderr,
    )
    for path in shown:
        print(f"  {path}", file=sys.stderr)
    print(
        f"\nAdd one file under {FRAGMENT_DIR}/ as {FRAGMENT_DIR}/README.md describes. If no "
        f"upgrader needs to know about this change, end a commit message with the trailer\n"
        f"  Changelog: none -- <why>\n"
        f"A trailer is read from the message's last paragraph only, so put it in the same "
        f"paragraph as any Co-Authored-By line, with no blank line between them.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
