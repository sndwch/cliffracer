"""Render a release note for a commit range: the changelog, its breaking changes, then the list.

Run as: python3 scripts/release_note.py <range> [--fragments-at <tag>]
        python3 scripts/release_note.py --count-breaking < note

With `--fragments-at`, the note opens with the changelog as it stood at that
tag: every fragment pending under `changelog.d/`, newest first as assembly
orders them, or, once they have been assembled, CHANGELOG.md's section for the
tag's version. A release-candidate tag says its entries are pending since the
last final release. A prerelease tag has no section of its own: its entries are
assembled under the version it leads to, so `v1.1.0-rc.1` reads `## 1.1.0`, whole,
with every Breaking entry in it. Below the changelog, `## Breaking changes` lists
its **Breaking** entries again, so a reader sees what an upgrade must act on
first. Commit footers are not read: a `BREAKING CHANGE:` footer decides
nothing in this repository, and the Breaking entries are where a breaking change
is recorded. Without `--fragments-at` the note is the commits alone.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

# The release job runs this under the runner's Python, outside the project's
# environment. The sibling is stdlib-only too, which the release-note tests check.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assemble_changelog import (  # noqa: E402
    changelog_section_at,
    fragment_text_problem,
    fragments_at,
)

RC_NOTE = "These are the changes pending since the last final release."

# Printable, because a NUL cannot be passed in argv to git.
SEP = "<<<CFCOMMIT>>>"
FIELD = "<<<CFBODY>>>"


def commits(rng: str, repo: str | None = None) -> Iterator[tuple[str, str]]:
    out = subprocess.run(
        ["git", "log", "--no-merges", f"--pretty=format:{SEP}%s{FIELD}%b", rng],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
    ).stdout
    for chunk in out.split(SEP):
        if not chunk.strip():
            continue
        subject, _, body = chunk.partition(FIELD)
        yield subject.strip(), body


def base_version(version: str) -> str:
    """The version a prerelease leads to: `1.1.0-rc.1` is `1.1.0`, and `1.1.0` is itself."""
    return version.partition("-")[0]


def changelog_entries(ref: str, repo: str | None = None) -> str | None:
    """The changelog entries at tag `ref`, as text, or None when there are none to carry.

    The fragments pending at the tag, newest first, or once they have been assembled, the section
    of the version the tag leads to. A fragment that is not well formed is still carried, as it is
    written, with a warning naming it: at a tag the text is what was merged, and a note missing an
    entry is worse than one showing it unformatted.
    """
    root = Path(repo) if repo else Path.cwd()
    version = ref[1:] if ref.startswith("v") else ref
    entries = fragments_at(root, ref)
    if entries:
        for name, text in entries:
            problem = fragment_text_problem(name, text)
            if problem:
                print(f"::warning::release note: {problem}; carried as written", file=sys.stderr)
        return "\n".join(text.strip("\n") for _, text in entries)
    return changelog_section_at(root, ref, base_version(version))


def changelog_block(ref: str, repo: str | None = None) -> str | None:
    """The `## Changelog` section for tag `ref`, or None when it has nothing to carry."""
    body = changelog_entries(ref, repo)
    if body is None:
        return None
    version = ref[1:] if ref.startswith("v") else ref
    lead = f"{RC_NOTE}\n\n" if "-rc." in version else ""
    return f"## Changelog\n\n{lead}{body}\n"


def bullets(text: str) -> list[str]:
    """Each top-level `- ` bullet of `text`, with its indented continuation lines."""
    found: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("- ") or not found:
            found.append([line])
        else:
            found[-1].append(line)
    return ["\n".join(lines).rstrip() for lines in found]


def breaking_entries(ref: str, repo: str | None = None) -> list[str]:
    """The **Breaking** entries among the changelog entries at tag `ref`, in their order."""
    body = changelog_entries(ref, repo)
    if body is None:
        return []
    return [entry for entry in bullets(body) if entry.startswith("- **Breaking**")]


def breaking_count(note: str) -> int:
    """How many Breaking entries the note's `## Breaking changes` section lists."""
    count = 0
    section = ""
    for line in note.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif section == "Breaking changes" and line.startswith("- **Breaking**"):
            count += 1
    return count


def render(rng: str, repo: str | None = None, fragments_at: str | None = None) -> str:
    subjects = [
        subject
        for subject, _ in commits(rng, repo)
        # Exclude work-in-progress commits from commit list.
        if not subject.startswith("WIP:")
    ]

    parts: list[str] = []
    if fragments_at is not None:
        block = changelog_block(fragments_at, repo)
        if block is not None:
            parts.append(block)
        breaking = breaking_entries(fragments_at, repo)
        if breaking:
            # Omitted when the release has no Breaking entry: a heading over nothing reads as
            # "we did not write it down".
            parts.append("## Breaking changes\n")
            parts.append("\n".join(breaking) + "\n")
    if subjects:
        parts.append("## Commits\n")
        parts.extend(f"- {s}" for s in subjects)
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args == ["--count-breaking"]:
        print(breaking_count(sys.stdin.read()))
        return 0
    ref = None
    if len(args) == 3 and args[1] == "--fragments-at":
        args, ref = args[:1], args[2]
    if len(args) != 1:
        print("usage: release_note.py <git range> [--fragments-at <tag>]", file=sys.stderr)
        return 2
    sys.stdout.write(render(args[0], fragments_at=ref))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
