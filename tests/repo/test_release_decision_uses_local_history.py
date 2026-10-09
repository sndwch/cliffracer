"""A release decision reads local history without contacting the forge."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def release_repository(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.name", "Release check")
    git("config", "user.email", "release@example.invalid")
    git("remote", "add", "origin", "https://forge.example.invalid/warehouse/orders.git")
    (tmp_path / "pyproject.toml").write_bytes((REPO / "pyproject.toml").read_bytes())
    git("add", "pyproject.toml")
    git("commit", "-m", "The order service has a release baseline")
    git("tag", "v1.0.0-rc.4")
    (tmp_path / "orders.txt").write_text("Orders are ready for delivery.\n")
    git("add", "orders.txt")
    git("commit", "-m", "Orders are ready for delivery")
    return tmp_path


def run_decision(repo: Path, level: str, server_scheme: str, api_scheme: str):
    workflow = yaml.safe_load((REPO / ".gitea/workflows/ci.yml").read_text())
    step = next(step for step in workflow["jobs"]["release"]["steps"] if step.get("id") == "decide")
    output = repo / "decision.txt"
    output.write_text("")
    env = dict(
        os.environ,
        LEVEL=level,
        GITHUB_OUTPUT=str(output),
        GITHUB_SERVER_URL=f"{server_scheme}://forge.example.invalid",
        GITHUB_API_URL=f"{api_scheme}://forge.example.invalid/api/v1",
        UV_PROJECT_ENVIRONMENT=sys.prefix,
        UV_NO_SYNC="true",
    )
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN"):
        env.pop(name, None)
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, output.read_text().splitlines()


@pytest.mark.parametrize(
    "server_scheme,api_scheme", [("http", "https"), ("https", "http"), ("https", "https")]
)
@pytest.mark.parametrize(
    "level,expected",
    [
        ("none", ["released=false"]),
        ("patch", ["released=true", "tag=v1.0.1-rc.1"]),
        ("minor", ["released=true", "tag=v1.1.0-rc.1"]),
    ],
)
def test_a_release_decision_works_with_the_runners_forge_urls(
    release_repository: Path, level: str, expected: list[str], server_scheme: str, api_scheme: str
):
    def refs() -> str:
        return subprocess.check_output(["git", "show-ref"], cwd=release_repository, text=True)

    before = refs()
    result, outputs = run_decision(release_repository, level, server_scheme, api_scheme)

    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == expected, "The requested release level did not produce its decision."
    assert refs() == before, "Deciding a version changed a branch or created a release tag."


def test_a_broken_release_configuration_still_refuses_to_decide(release_repository: Path):
    (release_repository / "pyproject.toml").write_text("[tool.semantic_release\n")

    result, outputs = run_decision(release_repository, "patch", "http", "http")

    assert result.returncode != 0, "A broken release configuration was accepted."
    assert "THE RELEASE TOOL FAILED" in result.stdout
    assert outputs == [], "The failed release computation still produced a decision."
