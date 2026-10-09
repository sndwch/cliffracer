"""One `uv build --all-packages` per tree a session builds, keyed on what the tree holds.

The packaging guards read the same wheels and sdists. A build is reused only while the tree's key
is unchanged: the bytes of every file git tracks or would track, and `git describe`, from which
the version is derived. An edit, an added file or a new commit makes the next call build again.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

_BUILT: dict[tuple[str, str], list[Path]] = {}


def tree_key(root: Path) -> str:
    """A digest of what a build of `root` reads: its files' bytes and its VCS description."""
    digest = hashlib.sha256()
    describe = subprocess.run(
        ["git", "describe", "--tags", "--long", "--dirty", "--always"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    digest.update(describe.encode())
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    for name in sorted(set(listed.split(b"\0")) - {b""}):
        path = root / name.decode()
        digest.update(name + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
        digest.update(b"\0")
    return digest.hexdigest()


def build_all(root: Path, out: Path) -> list[Path]:
    """The wheels and sdists of every member of `root`, built into `out` unless already built.

    A reused build keeps the paths of the call that made it, which may be in another directory.
    """
    key = (str(root), tree_key(root))
    if key not in _BUILT:
        proc = subprocess.run(
            ["uv", "build", "--all-packages", "--out-dir", str(out)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"uv build failed:\n{proc.stdout}\n{proc.stderr}"
        # uv writes a .gitignore into --out-dir; artefacts are picked by suffix.
        _BUILT[key] = sorted(f for f in out.iterdir() if f.name.endswith((".whl", ".tar.gz")))
    return _BUILT[key]
