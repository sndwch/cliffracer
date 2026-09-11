"""Render a release note for a commit range: breaking changes first, then the list.

Run as: python3 scripts/release_note.py <range>
"""

from __future__ import annotations

import subprocess
import sys

# Printable, because a NUL cannot be passed in argv to git.
SEP = "<<<CFCOMMIT>>>"
FIELD = "<<<CFBODY>>>"

# Trailers that end a BREAKING CHANGE block. A footer runs until the next
# footer key or the end of the body.
_TRAILER_KEYS = ("Co-Authored-By:", "Claude-Session:", "Signed-off-by:", "Refs:", "Closes:")


def commits(rng: str, repo: str | None = None):
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


def breaking_blocks(body: str) -> list[str]:
    """Extract paragraph-initial BREAKING CHANGE: footers from a commit body."""
    blocks: list[str] = []
    current: list[str] | None = None
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("BREAKING CHANGE:") and (i == 0 or not lines[i - 1].strip()):
            if current is not None:
                blocks.append("\n".join(current).rstrip())
            current = [line[len("BREAKING CHANGE:") :].strip()]
            continue
        if current is None:
            continue
        if any(line.startswith(k) for k in _TRAILER_KEYS):
            blocks.append("\n".join(current).rstrip())
            current = None
            continue
        # End footer block at the first blank line.
        if not line.strip():
            blocks.append("\n".join(current).rstrip())
            current = None
            continue
        current.append(line.strip())
    if current is not None:
        blocks.append("\n".join(current).rstrip())
    return [b for b in blocks if b]


def render(rng: str, repo: str | None = None) -> str:
    subjects: list[str] = []
    breaking: list[tuple[str, str]] = []
    for subject, body in commits(rng, repo):
        # Exclude work-in-progress commits from commit list.
        if not subject.startswith("WIP:"):
            subjects.append(subject)
        for block in breaking_blocks(body):
            breaking.append((subject, block))

    parts: list[str] = []
    if breaking:
        # Omit breaking changes section when none are present.
        parts.append("## Breaking changes\n")
        for subject, block in breaking:
            parts.append(f"### {subject}\n\n{block}\n")
    if subjects:
        parts.append("## Commits\n")
        parts.extend(f"- {s}" for s in subjects)
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_note.py <git range>", file=sys.stderr)
        return 2
    sys.stdout.write(render(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
