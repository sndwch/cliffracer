"""Every transport contract case is collected against both backends.

The contract exists to stop the in-memory transport drifting from a real NATS
client. It can only do that while both legs run the same cases: a case that
reaches one backend and not the other proves nothing about agreement, and is
the shape the contract was written to end.

This reads what pytest actually collects, for both tiers, rather than reading
the modules and inferring what they would collect.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.contract.transport_cases import CASE_NAMES

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

MEMORY_LEG = "tests/transport/test_contract.py"
REAL_LEG = "tests/integration/test_transport_contract.py"

# The count is a literal so that a case added to the shared list is a number a
# person changes here, having checked both legs pick it up, rather than one that
# follows the list wherever it goes.
EXPECTED_CASES = 16


def collected_ids(target: str) -> list[str]:
    """The case ids pytest collects for a leg, read from a real collection."""
    proc = subprocess.run(
        # No -q here: pyproject's addopts already supplies one, and a second
        # collapses the listing to a per-file count with no node ids in it.
        [sys.executable, "-m", "pytest", target, "--collect-only", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 5), (
        f"collecting {target} failed ({proc.returncode}):\n{proc.stdout[-2000:]}{proc.stderr[-2000:]}"
    )
    ids = []
    for line in proc.stdout.splitlines():
        if "::" in line and "[" in line and line.endswith("]"):
            ids.append(line[line.rindex("[") + 1 : -1])
    return ids


def test_the_shared_list_holds_the_recorded_number_of_cases():
    """The literal count and the shared list agree."""
    assert len(CASE_NAMES) == EXPECTED_CASES, (
        f"the shared list has {len(CASE_NAMES)} cases and the recorded count is "
        f"{EXPECTED_CASES}: {list(CASE_NAMES)}"
    )


def test_every_case_is_collected_against_the_in_memory_transport():
    """The in-memory leg collects one test per shared case."""
    collected = collected_ids(MEMORY_LEG)
    assert sorted(collected) == sorted(CASE_NAMES), (
        f"{MEMORY_LEG} collects {sorted(collected)}, the shared list is {sorted(CASE_NAMES)}"
    )


def test_every_case_is_collected_against_a_real_client():
    """The real-backend leg collects one test per shared case.

    Collection does not need a broker; the leg skips at run time when
    $CLIFFRACER_TEST_NATS_URL is unset. So this holds on a machine with no NATS,
    which is what makes it a guard rather than another thing needing the window.
    """
    collected = collected_ids(REAL_LEG)
    assert sorted(collected) == sorted(CASE_NAMES), (
        f"{REAL_LEG} collects {sorted(collected)}, the shared list is {sorted(CASE_NAMES)}"
    )


def test_the_two_legs_collect_the_same_cases():
    """Neither leg carries a case the other does not."""
    memory = set(collected_ids(MEMORY_LEG))
    real = set(collected_ids(REAL_LEG))
    assert memory == real, (
        f"the legs disagree; only in memory: {sorted(memory - real)}; "
        f"only against a real client: {sorted(real - memory)}"
    )


def test_CONTROL_the_reader_finds_ids_at_all():
    """A collection that returned nothing would satisfy the comparisons above."""
    assert collected_ids(MEMORY_LEG), (
        "the id reader found nothing, so the equality checks above would compare "
        "two empty lists and pass without reading a single case"
    )
