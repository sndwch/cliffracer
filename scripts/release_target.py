"""The final tag a `release` dispatch cuts: the base version of the newest prerelease.

Run as: python3 scripts/release_target.py

Prints `<prerelease tag> <final tag>`, for example `v1.1.0-rc.1 v1.1.0`, read from the
repository's tags and nothing else. `release` promotes a prerelease, so the version it
cuts is the one the prerelease leads to, and neither the commits nor semantic-release get
a say. semantic-release computes the next version from every commit since the last FINAL
release, so with breaking footers in that range it computes a major from a `1.1.0` prerelease
and no bump flag brings it back: from `v1.1.0-rc.1` it computes `v2.0.0` with no flag,
`v1.1.1` with `--patch` and `v1.2.0` with `--minor`.

Exits 1, saying why on stderr, when there is no prerelease to promote, when the newest
prerelease is not an ancestor of HEAD, when its final tag already exists, or when a higher
final release is already tagged. Stdlib only: the release job runs it outside the project's
environment.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")

Version = tuple[int, int, int]


class Refusal(Exception):
    """The promotion cannot be decided, with the reason."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _name(version: Version, rc: int | None = None) -> str:
    base = "v{}.{}.{}".format(*version)
    return base if rc is None else f"{base}-rc.{rc}"


def target(repo: Path) -> tuple[str, str]:
    """(newest prerelease tag, the final tag it is promoted to), or raise `Refusal`."""
    listed = _git(repo, "tag", "--list", "v*")
    if listed.returncode != 0:
        raise Refusal(f"git could not list the tags: {listed.stderr.strip()}")
    finals: set[Version] = set()
    rcs: list[tuple[Version, int]] = []
    for name in listed.stdout.split():
        match = TAG.match(name)
        if match is None:
            continue
        version = (int(match[1]), int(match[2]), int(match[3]))
        if match[4] is None:
            finals.add(version)
        else:
            rcs.append((version, int(match[4])))

    if not rcs:
        raise Refusal("there is no prerelease tag (vX.Y.Z-rc.N) to promote")
    version, number = max(rcs)
    rc_tag, final_tag = _name(version, number), _name(version)

    if version in finals:
        raise Refusal(f"{final_tag} is already tagged, so {rc_tag} has nothing left to promote")
    higher = sorted(f for f in finals if f > version)
    if higher:
        raise Refusal(
            f"{_name(higher[-1])} is already tagged and is higher than {final_tag}; "
            f"promoting {rc_tag} would put a lower final release after it"
        )
    if _git(repo, "merge-base", "--is-ancestor", rc_tag, "HEAD").returncode != 0:
        raise Refusal(f"{rc_tag} is not an ancestor of HEAD, so HEAD is not what it was cut from")
    return rc_tag, final_tag


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository root")
    args = parser.parse_args(argv)
    try:
        rc_tag, final_tag = target(args.repo)
    except Refusal as refusal:
        print(f"release_target: {refusal}", file=sys.stderr)
        return 1
    print(f"{rc_tag} {final_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
