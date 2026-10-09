"""Whether this checkout is the GitHub mirror, which omits the files only Gitea reads.

The mirror is published without `.gitea/`, `AGENTS.md`, `CLAUDE.md` and `changelog.d/`. A guard
that reads one of them is marked `gitea_checkout`, and is skipped on the mirror with
`MIRROR_REASON`. A module that reads one while it is collected calls `skip_module_on_mirror()`
first.

The one signal is that the root holds no `.gitea/`. A guard never skips because the file it reads
is missing: on a Gitea checkout a deleted file still fails by name. And Gitea CI asserts it is not
the mirror (`test_gitea_ci_is_never_the_mirror`), so a `.gitea/` that vanished fails the run
instead of skipping every guard that reads it.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

MIRROR_REASON = (
    "this checkout is the GitHub mirror (no .gitea/ at its root), which omits .gitea/, AGENTS.md, "
    "CLAUDE.md and changelog.d/; this guard reads one of them"
)


def is_mirror(root: Path = REPO) -> bool:
    """Whether the checkout at `root` is the GitHub mirror: it holds no `.gitea/` directory."""
    return not (root / ".gitea").is_dir()


def skip_module_on_mirror() -> None:
    """Skip the calling module on the mirror, before it reads a file the mirror omits."""
    if is_mirror():
        pytest.skip(MIRROR_REASON, allow_module_level=True)


def gitea_ci_on_the_mirror(env: dict[str, str], root: Path = REPO) -> str | None:
    """Why a run under Gitea Actions (`GITEA_ACTIONS` set) is broken because its checkout reads as
    the mirror, or None when it is not: a Gitea checkout without `.gitea/` would skip every guard
    that reads it."""
    if env.get("GITEA_ACTIONS") and is_mirror(root):
        return (
            f"this run is on Gitea Actions (GITEA_ACTIONS={env['GITEA_ACTIONS']!r}) but {root} holds "
            "no .gitea/, so every guard marked gitea_checkout is skipped as if this were the GitHub "
            "mirror; restore .gitea/"
        )
    return None
