"""Discovery shared by the workflow guards, so they cannot disagree about scope.

The repository publishes to two hosts: Gitea runs CI here, and GitHub runs it on
the upstream this tree is merged into. Both workflow trees are wanted, so every
guard over a workflow is parametrized over whatever is found here rather than
naming one file. A guard that reads a single hardcoded path is green about a
platform it never opened.
"""

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]

PLATFORM_DIRS: dict[str, Path] = {
    "gitea": REPO / ".gitea" / "workflows",
    "github": REPO / ".github" / "workflows",
}


def workflow_files(platform: str) -> list[Path]:
    """Return every workflow file the platform's directory holds."""
    directory = PLATFORM_DIRS[platform]
    return sorted(p for p in directory.glob("*.y*ml") if p.is_file())


def workflow_paths() -> list[tuple[str, Path]]:
    """Return (platform, path) for every workflow file in the repository.

    Every file, CI or not. The history and private-term scans use this: what a
    workflow is for does not change what it may not contain.
    """
    return [(platform, p) for platform in PLATFORM_DIRS for p in workflow_files(platform)]


# A workflow is a CI pipeline when it runs on a change to the code. Anything
# else -- a nightly soak, a dispatch-only maintenance job -- is not gating a
# merge, so the pipeline rules do not fit it: it has no test job, no gates, and
# no reason to match the other platform's file step for step.
#
# DERIVED FROM THE TRIGGERS, not a list of filenames. A name list would let a
# file opt out of the CI rules by being called something else, and the only
# thing that actually decides whether a workflow gates a change is its `on:`.
# The consequence is deliberate: add a push or pull_request trigger to a
# scheduled workflow and it becomes CI, with every pipeline rule applying at
# once.
CI_TRIGGERS = frozenset({"push", "pull_request"})


def trigger_names(workflow_data: dict[str, Any]) -> set[str]:
    """The event names in a workflow's `on:` block.

    `yaml.safe_load` returns the `on:` key as the boolean True under YAML 1.1,
    so the triggers live at `data[True]`; `data["on"]` is checked too in case a
    future parser disagrees. The block may be a mapping, a list, or a bare
    string, and all three appear in the wild.
    """
    raw = workflow_data.get(True, workflow_data.get("on"))
    if isinstance(raw, dict):
        return {str(k) for k in raw}
    if isinstance(raw, list):
        return {str(k) for k in raw}
    if isinstance(raw, str):
        return {raw}
    return set()


def is_ci_pipeline(workflow_data: dict[str, Any]) -> bool:
    """Whether this workflow gates a change to the code.

    Takes parsed data so a control can ask the question of a fixture without
    writing a file; `is_ci_workflow` is the same question asked of a path.
    """
    return bool(trigger_names(workflow_data) & CI_TRIGGERS)


def is_ci_workflow(path: Path) -> bool:
    """Whether the workflow at this path is a CI pipeline."""
    return is_ci_pipeline(load(path))


def ci_workflow_files(platform: str, directory: Path | None = None) -> list[Path]:
    """The platform's CI pipelines.

    `directory` is overridable so a control can ask the same question of a
    fixture tree rather than restating the rule.
    """
    if directory is None:
        candidates = workflow_files(platform)
    else:
        candidates = sorted(p for p in directory.glob("*.y*ml") if p.is_file())
    return [p for p in candidates if is_ci_workflow(p)]


def ci_workflow_paths() -> list[tuple[str, Path]]:
    """(platform, path) for every CI pipeline. What the pipeline rules apply to."""
    return [(platform, p) for platform in PLATFORM_DIRS for p in ci_workflow_files(platform)]


def ci_workflow_ids() -> list[str]:
    return [f"{platform}:{path.name}" for platform, path in ci_workflow_paths()]


def non_ci_workflow_paths() -> list[tuple[str, Path]]:
    """(platform, path) for every workflow that is not a CI pipeline."""
    ci = {p for _, p in ci_workflow_paths()}
    return [(platform, p) for platform, p in workflow_paths() if p not in ci]


def non_ci_workflow_ids() -> list[str]:
    return [f"{platform}:{path.name}" for platform, path in non_ci_workflow_paths()]


def load(path: Path) -> dict[str, Any]:
    """Parse a workflow file and check it is a mapping.

    Note `yaml.safe_load` returns the `on:` key as the boolean True under YAML
    1.1, not the string "on"; read triggers with `data[True]`.
    """
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path} must parse as a mapping"
    return data


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()
