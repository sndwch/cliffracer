"""One `uv build --all-packages` per tree a session builds, keyed on what the tree holds, and the
build every packaging guard runs (`uv_build`).

The packaging guards read the same wheels and sdists. A build is reused only while the tree's key
is unchanged: the bytes of every file git tracks or would track, and `git describe`, from which
the version is derived. An edit, an added file or a new commit makes the next call build again.

`uv build` reads the build backend (`hatchling`, `hatch-vcs`) from the package index, or from
uv's cache when it holds them. A run with no network, the namespaced one a broker-free check uses,
fails at the index, and that failure is not the packaging defect "uv build failed" reads as. So a
build that fails reaching the index is tried again offline, from the cache; one the cache cannot
serve either fails naming both, with what to do.
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


#: uv's own words, measured with uv 0.12.8: a request to the index that never connected...
UNREACHABLE = ("dns error", "failed to lookup address information", "client error (Connect)")
#: ...and an offline resolve the cache could not serve.
NOT_CACHED = ("the network was disabled", "was not found in the cache")


def uv_build(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """`uv build *args` in `cwd`, which must succeed; offline from uv's cache when the index is
    unreachable. A failure says which it is: the index unreachable and the cache without the
    backend, or the build itself."""
    proc = subprocess.run(["uv", "build", *args], cwd=cwd, capture_output=True, text=True)
    said = proc.stdout + proc.stderr
    if proc.returncode != 0 and any(sign in said for sign in UNREACHABLE):
        proc = subprocess.run(
            ["uv", "build", "--offline", *args], cwd=cwd, capture_output=True, text=True
        )
        said = proc.stdout + proc.stderr
    if proc.returncode != 0 and any(sign in said for sign in NOT_CACHED):
        raise AssertionError(
            "uv build could not reach the package index for the build backend (hatchling, "
            "hatch-vcs), and uv's cache does not hold it. This is the network, not the "
            "packaging: run once with the index reachable to fill the cache, then a run without "
            f"network builds from it.\n{proc.stdout}\n{proc.stderr}"
        )
    assert proc.returncode == 0, f"uv build failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def build_all(root: Path, out: Path) -> list[Path]:
    """The wheels and sdists of every member of `root`, built into `out` unless already built.

    A reused build keeps the paths of the call that made it, which may be in another directory.
    """
    key = (str(root), tree_key(root))
    if key not in _BUILT:
        uv_build(["--all-packages", "--out-dir", str(out)], root)
        # uv writes a .gitignore into --out-dir; artefacts are picked by suffix.
        _BUILT[key] = sorted(f for f in out.iterdir() if f.name.endswith((".whl", ".tar.gz")))
    return _BUILT[key]
