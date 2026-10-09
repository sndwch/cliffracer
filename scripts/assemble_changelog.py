"""Assemble the changelog fragments into a release section of CHANGELOG.md.

A change records its changelog entry as one file under `changelog.d/`, so two
pull requests adding entries touch different files and never conflict. This
script is run in the release-prep pull request, before a release is tagged:

    python scripts/assemble_changelog.py 1.1.0

It writes a `## 1.1.0` section directly under the `<!-- version list -->` flag,
holding every fragment's bullet, newest first, and deletes the fragments. The
release is then tagged from that pull request's merge. Nothing in CI writes
CHANGELOG.md: the release job only tags, builds, publishes and posts a note.

A `## Unreleased` block written before fragments existed is folded into the
new section, below the fragments, and its heading removed. So the first
assembly carries those entries into the release and every later one finds no
such block.

Order is newest first, read from git: a fragment's position is that of the
first-parent commit on the current branch that added it, so a fragment merged
later is listed earlier. Fragments added by the same commit, and fragments not
yet committed, are ordered by file name; uncommitted ones come first.

Exit status: 0 when the section was written (or printed, with `--dry-run`),
1 when a fragment is malformed, the version already has a section, the flag is
missing, or there is nothing to assemble.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog.d"
FLAG = "<!-- version list -->"
UNRELEASED = "## Unreleased"

# Lower-case words joined by hyphens. No digits, so an issue number cannot be
# the name, and no dots or underscores, so the name reads as a topic.
SLUG = re.compile(r"[a-z]+(?:-[a-z]+)*\.md")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?")


class AssemblyError(Exception):
    """Something the release-prep author has to fix before the section is written."""


def fragment_paths(repo: Path) -> list[Path]:
    """Every fragment, which is every `.md` directly in the directory but its README."""
    directory = repo / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.name != "README.md")


def fragment_problem(path: Path) -> str | None:
    """Why this file is not a fragment the assembly can use, or None."""
    return fragment_text_problem(path.name, path.read_text())


def fragment_text_problem(name: str, text: str) -> str | None:
    """Why a fragment with this name and text is not one the assembly can use, or None."""
    if not SLUG.fullmatch(name):
        return f"{name}: the name must be lower-case words joined by hyphens, no digits"
    if not text.startswith("- "):
        return f"{name}: a fragment is one bullet and starts with '- '"
    bullets = [line for line in text.splitlines() if line.startswith("- ")]
    if len(bullets) != 1:
        return f"{name}: a fragment holds exactly one bullet, found {len(bullets)}"
    return None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def added_positions(repo: Path, paths: list[Path]) -> dict[Path, int]:
    """For each fragment, the index of the first-parent commit that added it.

    0 is the newest commit on the branch. A fragment no first-parent commit
    added -- not yet committed -- is absent from the result.
    """
    if not (repo / ".git").exists() or not paths:
        return {}
    by_rel = {path.relative_to(repo).as_posix(): path for path in paths}
    positions = _positions(repo, list(by_rel), "HEAD")
    return {by_rel[rel]: index for rel, index in positions.items()}


def _positions(repo: Path, rels: list[str], ref: str) -> dict[str, int]:
    """For each path, the index on `ref`'s first-parent line of the commit that added it."""
    order = {
        sha: index
        for index, sha in enumerate(_git(repo, "rev-list", "--first-parent", ref).split())
    }
    positions: dict[str, int] = {}
    for rel in rels:
        added = _git(
            repo, "log", "--first-parent", "--diff-filter=A", "--format=%H", ref, "--", rel
        ).split()
        indexes = [order[sha] for sha in added if sha in order]
        if indexes:
            # The newest add. Assembly deletes every fragment at a release, so a
            # later change can reuse a name; ordering by the first add would put
            # that new entry where the previous release's one was.
            positions[rel] = min(indexes)
    return positions


def _order_key(positions: dict[str, int], rel: str) -> tuple[bool, int, str]:
    """Newest first; uncommitted before committed; ties by file name."""
    return (rel in positions, positions.get(rel, -1), rel.rsplit("/", 1)[-1])


def ordered(repo: Path, paths: list[Path]) -> list[Path]:
    """Newest first; uncommitted before committed; ties by file name."""
    positions = {p.relative_to(repo).as_posix(): i for p, i in added_positions(repo, paths).items()}
    return sorted(paths, key=lambda p: _order_key(positions, p.relative_to(repo).as_posix()))


def fragments_at(repo: Path, ref: str) -> list[tuple[str, str]]:
    """(name, text) of each fragment in the directory at `ref`, in `ordered`'s order.

    Read from git at `ref`, never from the working tree, so a release note shows
    what the tag holds whatever the checkout has since.
    """
    listing = _git(repo, "ls-tree", "--name-only", ref, f"{FRAGMENT_DIR}/").split("\n")
    rels = [
        rel
        for rel in listing
        if rel.endswith(".md") and rel.count("/") == 1 and not rel.endswith("/README.md")
    ]
    positions = _positions(repo, rels, ref)
    rels.sort(key=lambda rel: _order_key(positions, rel))
    return [(rel.rsplit("/", 1)[-1], _git(repo, "show", f"{ref}:{rel}")) for rel in rels]


def changelog_section_at(repo: Path, ref: str, version: str) -> str | None:
    """The body of CHANGELOG.md's `## version` section at `ref`, or None."""
    try:
        text = _git(repo, "show", f"{ref}:CHANGELOG.md")
    except subprocess.CalledProcessError:
        return None
    match = re.search(rf"^## {re.escape(version)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None or not match.group(1).strip():
        return None
    return match.group(1).strip("\n")


def assemble(text: str, version: str, bullets: list[str]) -> str:
    """CHANGELOG text with a `## version` section of `bullets` under the flag."""
    if FLAG not in text:
        raise AssemblyError(f"CHANGELOG.md has no {FLAG!r} line to insert the release under")
    if re.search(rf"^## {re.escape(version)}\s*$", text, re.M):
        raise AssemblyError(f"CHANGELOG.md already has a '## {version}' section")

    head, _, rest = text.partition(FLAG)
    rest = rest.lstrip("\n")
    folded: list[str] = []
    if rest.startswith(UNRELEASED + "\n"):
        body = rest[len(UNRELEASED) + 1 :]
        next_heading = re.search(r"^## ", body, re.M)
        unreleased = body[: next_heading.start()] if next_heading else body
        rest = body[next_heading.start() :] if next_heading else ""
        if unreleased.strip():
            folded = [unreleased.strip("\n")]

    entries = [b.strip("\n") for b in bullets] + folded
    if not entries:
        raise AssemblyError("there are no fragments and no Unreleased entries to assemble")
    section = f"## {version}\n" + "\n".join(entries) + "\n"
    return head + FLAG + "\n\n" + section + ("\n" + rest if rest else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("version", help="the release version, e.g. 1.1.0")
    parser.add_argument("--repo", type=Path, default=REPO, help="repository root")
    parser.add_argument("--dry-run", action="store_true", help="print the result, change nothing")
    args = parser.parse_args(argv)

    repo: Path = args.repo
    try:
        if not VERSION.fullmatch(args.version):
            raise AssemblyError(f"{args.version!r} is not a version such as 1.1.0")
        paths = fragment_paths(repo)
        problems = [p for p in (fragment_problem(path) for path in paths) if p]
        if problems:
            raise AssemblyError("malformed fragments:\n  " + "\n  ".join(problems))
        paths = ordered(repo, paths)
        changelog = repo / "CHANGELOG.md"
        result = assemble(changelog.read_text(), args.version, [p.read_text() for p in paths])
    except AssemblyError as exc:
        print(f"assemble_changelog: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        sys.stdout.write(result)
        return 0
    changelog.write_text(result)
    for path in paths:
        path.unlink()
    print(f"wrote ## {args.version} from {len(paths)} fragment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
