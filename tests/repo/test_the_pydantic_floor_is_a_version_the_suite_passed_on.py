"""The pydantic floor the workspace declares is a version the broker-free suite has passed on.

A floor is a promise that the library works on that version. `>=2.0.0` was declared while the
templates read `FieldInfo.init`, which pydantic 2.5 lacks, so `normalize()` of any template raised
`AttributeError` on a version the package said it supported. The floor is now the lowest version
the suite was run on and passed, and this guard holds it there: every declaration of pydantic in
the workspace names the same floor, the floor is written out below, and it is one of the versions
recorded as verified. Lowering it means running the suite on the new floor and recording that run
here.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]

# Written out, not read from pyproject.toml: a guard that reads the value it guards moves with it.
FLOOR = "2.11.0"

# What ran on each version that may be the floor. A version belongs here only after the broker-free
# suite (tests/unit, tests/transport, tests/repo and packages/*/tests, without nats_required and
# benchmark) passed on it with nothing failing that does not also fail on the locked version.
VERIFIED_ON = {
    "2.11.0": "2026-10-03, CPython 3.12.14: the same results as the locked 2.11.7 on the same host",
}


def _declarations() -> dict[str, Requirement]:
    """Every pydantic requirement in the workspace's pyproject files, by file."""
    found: dict[str, Requirement] = {}
    for path in [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]:
        project = tomllib.loads(path.read_text()).get("project", {})
        for line in project.get("dependencies", []):
            requirement = Requirement(line)
            if requirement.name == "pydantic":
                found[str(path.relative_to(ROOT))] = requirement
    return found


def _floor_of(requirement: Requirement) -> str | None:
    """The version a `>=` clause names, or None when there is no lower bound."""
    floors = [spec.version for spec in requirement.specifier if spec.operator == ">="]
    return floors[0] if len(floors) == 1 else None


def test_the_core_package_declares_pydantic():
    assert "pyproject.toml" in _declarations(), "the guard found no pydantic requirement to hold"


def test_every_declaration_of_pydantic_names_the_floor():
    declarations = _declarations()

    assert declarations
    floors = {path: _floor_of(requirement) for path, requirement in declarations.items()}
    assert floors == dict.fromkeys(declarations, FLOOR), floors


def test_the_floor_is_a_version_the_suite_was_recorded_passing_on():
    assert FLOOR in VERIFIED_ON, (
        f"pydantic {FLOOR} is declared as the floor but no run of the suite on it is recorded; "
        "run the broker-free suite on that version and add it to VERIFIED_ON"
    )


def test_the_locked_pydantic_is_at_or_above_the_floor():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = [p["version"] for p in lock["package"] if p["name"] == "pydantic"]

    assert len(locked) == 1, locked
    assert Version(locked[0]) >= Version(FLOOR), locked


def test_CONTROL_a_lower_floor_is_read_as_a_different_floor():
    assert _floor_of(Requirement("pydantic>=2.0.0,<3.0.0")) == "2.0.0" != FLOOR
    assert _floor_of(Requirement("pydantic<3.0.0")) is None
