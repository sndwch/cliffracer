"""Ensure dual CI workflows exist for Gitea Actions and GitHub Actions and stay in sync."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def test_dual_ci_workflows_exist_and_match():
    """Verify that both Gitea and GitHub CI workflows exist, parse as valid YAML, and run matching gates."""
    gitea_path = REPO / ".gitea" / "workflows" / "ci.yml"
    github_path = REPO / ".github" / "workflows" / "ci.yml"

    assert gitea_path.is_file(), f"Missing Gitea workflow at {gitea_path}"
    assert github_path.is_file(), f"Missing GitHub workflow at {github_path}"

    gitea_data = yaml.safe_load(gitea_path.read_text())
    github_data = yaml.safe_load(github_path.read_text())

    assert "test" in gitea_data.get("jobs", {}), "Gitea workflow missing 'test' job"
    assert "test" in github_data.get("jobs", {}), "GitHub workflow missing 'test' job"

    gitea_text = gitea_path.read_text()
    github_text = github_path.read_text()

    required_steps = [
        "uv run ruff check src/ packages/ tests/ examples/",
        "uv run ruff format --check src/ packages/ tests/ examples/",
        "uv run mypy src/ packages/*/src",
        "uv run pytest -p no:cacheprovider --tb=short",
    ]
    for step in required_steps:
        assert step in gitea_text, f"Step '{step}' missing from Gitea workflow"
        assert step in github_text, f"Step '{step}' missing from GitHub workflow"


def test_the_workflow_that_runs_is_still_there_and_lints_the_test_tree():
    """Verify that Gitea CI workflow exists and enforces linting and formatting."""
    workflow = (REPO / ".gitea" / "workflows" / "ci.yml").read_text()
    assert "ruff check src/ packages/ tests/ examples/" in workflow, workflow[:200]
    assert "ruff format --check src/ packages/ tests/ examples/" in workflow
