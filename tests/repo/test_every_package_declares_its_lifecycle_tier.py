"""Every package under `packages/` declares its lifecycle tier, in the file CI can read without installing it.

ADR-0018 gives an extension one of three tiers and says the tier is declared in package metadata.
The pairwise matrix it describes is meant to cover the *Supported* packages, so the set of them has
to come from the declaration that decides support: a matrix that listed its packages by hand would
shrink silently the day a package was added and nobody remembered to add it.

The declaration is `[tool.cliffracer] lifecycle = "<tier>"` in the package's own `pyproject.toml`.
This fails when a package has none, when the value is not one of the three tiers (a typo reads as
"not Supported" and would drop the package from a matrix without a word), and when a `packages/`
directory has no `pyproject.toml` at all.
"""

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
PACKAGES = REPO / "packages"

TIERS = frozenset({"incubating", "supported", "deprecated"})

# A recorded count, not a floor: a package that is added or removed is a change worth reading, and
# the declaration it carries is part of that change.
PACKAGE_COUNT = 9


def declared_tiers(packages: Path) -> dict[str, str | None]:
    """Each package directory under `packages`, and its declared tier or None if it has none."""
    found: dict[str, str | None] = {}
    for directory in sorted(path for path in packages.iterdir() if path.is_dir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            found[directory.name] = None
            continue
        table = tomllib.loads(pyproject.read_text()).get("tool", {}).get("cliffracer", {})
        tier = table.get("lifecycle")
        found[directory.name] = tier if isinstance(tier, str) else None
    return found


def problems(packages: Path) -> list[str]:
    """What is wrong with the declarations under `packages`, one line each."""
    out = []
    for name, tier in declared_tiers(packages).items():
        if tier is None:
            out.append(f"{name}: no [tool.cliffracer] lifecycle in its pyproject.toml")
        elif tier not in TIERS:
            out.append(f"{name}: lifecycle {tier!r} is not one of {sorted(TIERS)}")
    return out


def test_every_package_declares_one_of_the_three_tiers():
    found = declared_tiers(PACKAGES)

    assert len(found) == PACKAGE_COUNT, (
        f"packages/ holds {len(found)} packages, this guard records {PACKAGE_COUNT}: "
        "update PACKAGE_COUNT in the change that adds or removes one"
    )
    assert problems(PACKAGES) == []


def _package(root: Path, name: str, pyproject: str | None) -> None:
    (root / name).mkdir()
    if pyproject is not None:
        (root / name / "pyproject.toml").write_text(pyproject)


def test_CONTROL_a_declared_tier_is_read_back(tmp_path):
    _package(tmp_path, "a", '[tool.cliffracer]\nlifecycle = "incubating"\n')
    _package(tmp_path, "b", '[tool.cliffracer]\nlifecycle = "deprecated"\n')

    assert declared_tiers(tmp_path) == {"a": "incubating", "b": "deprecated"}
    assert problems(tmp_path) == []


def test_CONTROL_a_package_with_no_table_is_named(tmp_path):
    _package(tmp_path, "bare", '[project]\nname = "bare"\n')

    assert problems(tmp_path) == ["bare: no [tool.cliffracer] lifecycle in its pyproject.toml"]


def test_CONTROL_a_table_without_the_key_is_named(tmp_path):
    _package(tmp_path, "empty", "[tool.cliffracer]\n")

    assert len(problems(tmp_path)) == 1


def test_CONTROL_a_misspelt_tier_is_named_not_read_as_unsupported(tmp_path):
    _package(tmp_path, "typo", '[tool.cliffracer]\nlifecycle = "suported"\n')

    (line,) = problems(tmp_path)
    assert line.startswith("typo: lifecycle 'suported' is not one of")


def test_CONTROL_a_tier_that_is_not_a_string_is_named(tmp_path):
    _package(tmp_path, "number", "[tool.cliffracer]\nlifecycle = 1\n")

    assert len(problems(tmp_path)) == 1


def test_CONTROL_a_package_directory_without_a_pyproject_is_named(tmp_path):
    _package(tmp_path, "nothing", None)

    assert problems(tmp_path) == ["nothing: no [tool.cliffracer] lifecycle in its pyproject.toml"]


def test_CONTROL_a_tier_given_as_a_list_is_named_not_a_crash(tmp_path):
    _package(tmp_path, "listed", '[tool.cliffracer]\nlifecycle = ["supported"]\n')

    assert problems(tmp_path) == ["listed: no [tool.cliffracer] lifecycle in its pyproject.toml"]
