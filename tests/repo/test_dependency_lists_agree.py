"""Tests verifying consistency between [project.optional-dependencies].dev and [dependency-groups].dev."""

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _load():
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_the_two_dev_lists_are_identical():
    data = _load()
    extra = set(data["project"]["optional-dependencies"]["dev"])
    group = set(data["dependency-groups"]["dev"])

    only_in_extra = sorted(extra - group)
    only_in_group = sorted(group - extra)

    assert not only_in_extra and not only_in_group, (
        "The [dev] extra and the dev dependency-group have drifted. Change "
        "both together.\n"
        f"  only in [project.optional-dependencies].dev: {only_in_extra}\n"
        f"  only in [dependency-groups].dev:             {only_in_group}"
    )


def _names(requirements: list[str]) -> set[str]:
    """The distribution names in a requirement list, without versions or extras."""
    return {
        re.match(r"[A-Za-z0-9_.-]+", requirement).group(0).lower() for requirement in requirements
    }  # type: ignore[union-attr]


# What the project's own gates run (`uv run pytest`, `ruff`, `mypy`): a list that has lost any of
# them installs a contributor environment that cannot run the checks CI runs.
GATE_TOOLS = {"pytest", "ruff", "mypy"}


def test_both_dev_lists_carry_the_tools_the_gates_run():
    """The agreement test above holds for two empty lists; this is the floor under it.

    `pip install cliffracer[dev]` is the documented path for contributors, and an extra truncated
    to nothing, or to a list without the gates' tools, installs an environment that cannot run
    them while both lists still agree.
    """
    data = _load()
    extra = data["project"]["optional-dependencies"]["dev"]
    group = data["dependency-groups"]["dev"]

    for label, requirements in (
        ("[project.optional-dependencies].dev", extra),
        ("[dependency-groups].dev", group),
    ):
        missing = sorted(GATE_TOOLS - _names(requirements))
        assert not missing, f"{label} has no {missing}: {requirements}"
