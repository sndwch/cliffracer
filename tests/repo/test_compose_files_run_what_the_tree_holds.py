"""Every compose file in the tree starts from published images and mounts only paths that exist.

A compose file that builds an image, or mounts a file from beside itself, depends
on the tree around it. Nothing in CI starts these stacks, so when that tree moves
on the file stops working without anything failing: Dockerfiles copying sources
long gone, services running scripts that were deleted, dashboards mounted from
directories that never existed. A compose file here runs published images, and
any relative host path it mounts is in the repository.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def compose_files() -> list[Path]:
    """Tracked `compose*.yml` / `docker-compose*.yml` files, from git's own list."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    names = [
        line
        for line in out.splitlines()
        if Path(line).name.startswith(("compose", "docker-compose"))
        and Path(line).suffix in (".yml", ".yaml")
    ]
    return [REPO / name for name in sorted(names)]


def problems(path: Path) -> list[str]:
    """What in this compose file depends on the tree and is not satisfied by it."""
    doc = yaml.safe_load(path.read_text()) or {}
    found = []
    for name, service in (doc.get("services") or {}).items():
        if "build" in service:
            found.append(f"{name}: builds an image")
        for volume in service.get("volumes") or []:
            source = volume.get("source") if isinstance(volume, dict) else volume.split(":", 1)[0]
            if not str(source).startswith("."):
                # A named volume, or an absolute host path such as /etc/localtime.
                continue
            if not (path.parent / source).exists():
                found.append(f"{name}: mounts {source}, which does not exist")
    return found


def test_the_sweep_finds_a_compose_file():
    """An empty list and a clean tree look alike."""
    assert compose_files(), "no compose file found; the sweep reads nothing"


@pytest.mark.parametrize("path", compose_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_the_compose_file_needs_nothing_the_tree_does_not_hold(path: Path):
    assert problems(path) == [], f"{path.relative_to(REPO)}: {problems(path)}"


def test_CONTROL_a_build_and_a_missing_mount_are_reported(tmp_path: Path):
    (tmp_path / "present.conf").write_text("")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  built:\n"
        "    build: .\n"
        "  mounted:\n"
        "    image: nats:alpine\n"
        "    volumes:\n"
        "      - ./present.conf:/etc/present.conf\n"
        "      - ./absent/dir:/etc/absent\n"
        "      - named_data:/data\n"
        "      - /etc/localtime:/etc/localtime:ro\n"
        "      - type: bind\n"
        "        source: ./also-absent\n"
        "        target: /x\n"
    )

    assert problems(compose) == [
        "built: builds an image",
        "mounted: mounts ./absent/dir, which does not exist",
        "mounted: mounts ./also-absent, which does not exist",
    ]
