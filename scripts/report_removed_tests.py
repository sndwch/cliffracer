"""Name the tests this branch removes, in the job log.

A change that removes a fix AND the test that guards it is green, because the
only thing that would have objected left in the same commit. The suite reports
the same "all passed" as a tree that still has the test. The count moves and
nobody reads the count.

NOT A GATE, and the exit code is always 0. Removing a test is often right: one
whose subject is gone should go with it. What this buys is that the removal
arrives as a LINE rather than as an absence, and an absence has no place in a
diff where the eye naturally goes.

Two decisions that decide whether it is worth reading:

COLLECTED FROM THE MERGE BASE, never the base branch tip. A two-dot comparison
against a moving `main` renders every test that landed after the branch was cut
as "removed" -- the artefact behind the false alarm that prompted this -- and a
report that cries wolf is ignored within a week. The merge base is resolved by
`check_commit_messages.fetch_base`, which already knows how to deepen a depth-1
CI checkout until the two sides share an ancestor; a second copy of that would
be a second thing to be wrong.

RENAMES ARE FOLDED when the body is unchanged. This suite renames tests often,
and a rename counted as a removal is exactly the noise that gets a report
skipped. Matching is on the body rather than the name, so a rename that also
edits the test reads as a removal plus an addition -- which is the honest
answer, because there is no longer anything saying the old assertion survived.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_commit_messages import fetch_base  # noqa: E402

#: How many removals to name before summarising. Long output is how a report
#: stops being read, and the count is always printed whether or not the names
#: are.
MAX_NAMED = 40


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def test_files(repo: Path, ref: str) -> list[str]:
    """Paths at *ref* that pytest would collect from."""
    listed = _git(repo, "ls-tree", "-r", "--name-only", ref)
    if listed.returncode != 0:
        return []
    return [
        p
        for p in listed.stdout.splitlines()
        if p.endswith(".py") and Path(p).name.startswith("test_")
    ]


def tests_at(repo: Path, ref: str) -> dict[tuple[str, str], str]:
    """`(path, node id) -> body hash` for every test function at *ref*.

    Read by AST from the object store rather than by checking the ref out and
    collecting with pytest. Two reasons: a checkout in the middle of a CI job
    is a side effect on a tree other steps are using, and the BODY is what
    makes a rename recognisable -- a node id alone cannot tell a renamed test
    from a deleted one.

    A file that does not parse contributes nothing rather than stopping the
    run: this is a report, and a syntax error is already somebody else's red.
    """
    found: dict[tuple[str, str], str] = {}
    for path in test_files(repo, ref):
        shown = _git(repo, "show", f"{ref}:{path}")
        if shown.returncode != 0:
            continue
        try:
            tree = ast.parse(shown.stdout)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in node.body)
            found[(path, node.name)] = hashlib.sha256(body.encode()).hexdigest()
    return found


def classify(
    base: dict[tuple[str, str], str], head: dict[tuple[str, str], str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """`(removed, renamed)` -- node ids gone, and old/new pairs that only moved.

    A removal whose body hash matches something ADDED is a rename. Matching on
    the body means a rename that also edits the test is reported as a removal,
    which is the honest answer: nothing is left asserting what the old one did.
    """
    gone = sorted(set(base) - set(head))
    added = sorted(set(head) - set(base))
    by_body: dict[str, list[tuple[str, str]]] = {}
    for key in added:
        by_body.setdefault(head[key], []).append(key)

    removed: list[str] = []
    renamed: list[tuple[str, str]] = []
    for path, name in gone:
        candidates = by_body.get(base[(path, name)])
        if candidates:
            new_path, new_name = candidates.pop(0)
            renamed.append((f"{path}::{name}", f"{new_path}::{new_name}"))
        else:
            removed.append(f"{path}::{name}")
    return removed, renamed


def render(removed: list[str], renamed: list[tuple[str, str]]) -> str:
    """The report, including the zero line.

    The zero line is printed on purpose. A report that says nothing when there
    is nothing to say is indistinguishable from a step that did not run, and
    the whole value here is that a reviewer knows the question was asked.
    """
    plural = "" if len(removed) == 1 else "s"
    lines = [f"this branch removes {len(removed)} test{plural}"]
    for node_id in removed[:MAX_NAMED]:
        lines.append(f"  {node_id}")
    if len(removed) > MAX_NAMED:
        lines.append(f"  ... and {len(removed) - MAX_NAMED} more")
    if renamed:
        lines.append(f"{len(renamed)} renamed, body unchanged:")
        for old, new in renamed[:MAX_NAMED]:
            lines.append(f"  {old} -> {new}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base branch, e.g. main")
    parser.add_argument("--repo", default=".", help="repository root")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    # A base that already resolves needs no fetch. That is the local case --
    # a reviewer running this by hand, and the tests below -- and it is also
    # what `fetch_base` itself says: a run whose base is already present does
    # not need its fetch to succeed. Going through the network path anyway
    # would make this script unusable anywhere without a remote.
    ref: str | None = args.base
    if _git(repo, "rev-parse", "--verify", f"{args.base}^{{commit}}").returncode != 0:
        # `COMMIT_CHECK_REPO`, not `chdir`. `check_commit_messages._git` runs
        # `git -C git_root()`, and `git_root()` reads this variable or falls
        # back to a constant for the SCRIPT's own repository -- so it never
        # sees `--repo`, and `-C` overrides the working directory, which makes
        # changing it inert. This is the seam that helper documents as
        # overridable, and the one its own tests use.
        os.environ["COMMIT_CHECK_REPO"] = str(repo)
        ref, code, detail = fetch_base(args.base)
        if ref is None:
            # Reported, not raised. A report that cannot answer says so;
            # failing here would turn a missing comparison into a broken
            # build, which is the opposite of this not being a gate.
            print(f"cannot report removed tests: {code}: {detail}".rstrip(": "))
            return 0

    merge_base = _git(repo, "merge-base", ref, "HEAD")
    if merge_base.returncode != 0:
        print(f"cannot report removed tests: no merge base with {ref}")
        return 0

    removed, renamed = classify(tests_at(repo, merge_base.stdout.strip()), tests_at(repo, "HEAD"))
    print(render(removed, renamed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
