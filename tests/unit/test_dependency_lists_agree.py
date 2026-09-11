"""Tests verifying consistency between [project.optional-dependencies].dev and [dependency-groups].dev."""

import tomllib
from pathlib import Path

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


def test_the_dev_extra_still_exists():
    """Verify [project.optional-dependencies].dev entry remains present."""
    assert "dev" in _load()["project"]["optional-dependencies"]
