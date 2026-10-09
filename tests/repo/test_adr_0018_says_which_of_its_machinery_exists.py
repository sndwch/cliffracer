"""ADR-0018 says its composition-testing machinery is not built; this fails when that stops being true.

The record's decision describes a pairwise CI matrix, a nightly higher-order job that prints a
`--composition-seed`, and lifecycle tiers in package metadata. None exists, and a decision that
reads as if it did sends a reader looking for a job that is not there. The record now says so in
its Implementation line. The line is a claim about the repository, so it is checked against it:
when one of the pieces appears the line is false, and this names the one to update.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def adr_0018(text: str) -> str:
    match = re.search(r"^## ADR-0018\b.*?(?=^## ADR-|\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "ADR-0018 is not in decisions.md"
    return match.group(0)


def implementation_line(section: str) -> str:
    match = re.search(r"^- \*\*Implementation\*\*:.*$", section, flags=re.MULTILINE)
    assert match, "ADR-0018 has no Implementation line"
    return match.group(0)


def option_exists(root: Path) -> bool:
    """Whether any conftest in the repository adds a `--composition-seed` option."""
    return any(
        "composition-seed" in path.read_text()
        for path in root.rglob("conftest.py")
        if ".venv" not in path.parts
    )


def schedules_composition_tests(root: Path) -> bool:
    """Whether a scheduled workflow other than the chaos soak exists."""
    scheduled = []
    for workflow in (root / ".gitea" / "workflows").glob("*.yml"):
        if re.search(r"^\s*schedule:", workflow.read_text(), flags=re.MULTILINE):
            scheduled.append(workflow.name)
    return scheduled != ["chaos-soak.yml"]


def reads_the_tier(root: Path) -> list[str]:
    """Source files under `src/` and `packages/*/src` that read a package's lifecycle tier.

    A reader of the tier has to reach `pyproject.toml` (through a TOML parser) or an installed
    distribution's metadata (entry points) and name the tier it is after.
    """
    trees = [root / "src", *sorted((root / "packages").glob("*/src"))]
    found = []
    for tree in trees:
        for path in tree.rglob("*.py"):
            text = path.read_text()
            reaches = any(
                token in text for token in ("tomllib", "entry_points", "importlib.metadata")
            )
            if reaches and re.search(r"lifecycle|tool\.cliffracer|cliffracer\.extensions", text):
                found.append(str(path.relative_to(root)))
    return found


def clause_four(section: str) -> str:
    match = re.search(
        r"^  4\. \*\*Lifecycle Tiers\*\*.*?(?=^- \*\*Consequences)", section, re.M | re.S
    )
    assert match, "ADR-0018 has no clause 4"
    return match.group(0)


def _line() -> str:
    return implementation_line(adr_0018((REPO / "docs" / "decisions.md").read_text()))


def test_the_record_says_there_is_no_seed_option_exactly_while_there_is_none():
    assert not option_exists(REPO), (
        "a conftest now adds --composition-seed: update ADR-0018's Implementation line"
    )
    assert "no `--composition-seed` option" in _line()


def test_the_record_names_the_chaos_soak_as_the_only_scheduled_workflow_while_it_is():
    assert not schedules_composition_tests(REPO), (
        "a scheduled workflow other than the chaos soak exists: update ADR-0018's Implementation line"
    )
    assert "overnight chaos soak" in _line()


def test_CONTROL_a_conftest_that_adds_the_option_is_seen(tmp_path):
    (tmp_path / "conftest.py").write_text(
        'def pytest_addoption(p): p.addoption("--composition-seed")'
    )

    assert option_exists(tmp_path)


def test_CONTROL_a_second_scheduled_workflow_is_seen(tmp_path):
    workflows = tmp_path / ".gitea" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "chaos-soak.yml").write_text("on:\n  schedule:\n    - cron: '0 2 * * *'\n")
    (workflows / "nightly-fuzz.yml").write_text("on:\n  schedule:\n    - cron: '0 3 * * *'\n")

    assert schedules_composition_tests(tmp_path)


def test_CONTROL_the_soak_alone_is_not_a_composition_job(tmp_path):
    workflows = tmp_path / ".gitea" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "chaos-soak.yml").write_text("on:\n  schedule:\n    - cron: '0 2 * * *'\n")

    assert not schedules_composition_tests(tmp_path)


def test_nothing_in_the_source_reads_a_tier_while_the_record_says_nothing_installed_can():
    assert reads_the_tier(REPO) == [], (
        "source now reads a lifecycle tier: update ADR-0018's clause 4 and its Implementation line"
    )
    assert "nothing installed can read the tier" in _line()


def test_clause_four_says_the_import_warning_is_not_built():
    clause = clause_four(adr_0018((REPO / "docs" / "decisions.md").read_text()))

    assert "emits a warning on import" not in clause
    assert "emitting warnings on import" not in clause
    assert clause.count("not built") == 2


def test_CONTROL_a_source_file_that_parses_the_tier_is_seen(tmp_path):
    source = tmp_path / "packages" / "p" / "src" / "p"
    source.mkdir(parents=True)
    (source / "tier.py").write_text(
        "import tomllib\n\ndef tier(path):\n    return tomllib.load(open(path))['tool']['cliffracer']['lifecycle']\n"
    )
    (tmp_path / "src").mkdir()

    assert reads_the_tier(tmp_path) == ["packages/p/src/p/tier.py"]


def test_CONTROL_a_source_file_that_reads_installed_entry_points_for_a_tier_is_seen(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tier.py").write_text(
        "from importlib.metadata import entry_points\n\n"
        "def tier():\n    return entry_points(group='cliffracer.extensions.incubating')\n"
    )

    assert reads_the_tier(tmp_path) == ["src/tier.py"]


def test_CONTROL_a_source_file_that_mentions_neither_is_not_one(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "plain.py").write_text("import tomllib\n\nconfig = tomllib.loads('')\n")

    assert reads_the_tier(tmp_path) == []
