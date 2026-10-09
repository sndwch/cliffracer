"""The seeded property checks run within their budget.

A property check's cost grows with its case count, and with what its generator builds, and nothing
else in the suite notices when one grows. Every unit-test module that uses the property harness
(`tests.fixtures.properties`) is found by that import and run in a child `pytest` on its fixed seed
and case count, with `CLIFFRACER_PROPERTY_SEEDS` and `CLIFFRACER_PROPERTY_SCALE` removed, and the run
must spend no more than `BUDGET` seconds of CPU. A failure names the modules and the slowest tests.

The measure is the child's CPU time (user plus system, from `getrusage(RUSAGE_CHILDREN)` taken
around it), not wall time: what grows with a case count is the work done, and time a run spends
waiting for a core is not counted. CPU time still grows on a contended host, where cores and caches
are shared, so `BUDGET` is sized from runs on a busy runner. The price is sensitivity: from a quiet
run's CPU time the budget admits growth of almost four times, so it catches a case count or a
generator that grows several-fold, and not one module doubling.

The CONTROLs run planted modules through the same function: one that burns CPU fails a tight budget
and passes a loose one, and one that only sleeps passes the same tight budget, which a wall-clock
measure would fail. A module that fails, and one whose every check skips, fail the measurement.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

resource = pytest.importorskip("resource", reason="CPU time is read with getrusage, which is POSIX")

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

#: CPU seconds the property checks may spend together, on their fixed seeds. The seven modules
#: without the template property spent 25.6 on a quiet host and up to 68 at 1-minute loads of 12 to
#: 26 on the shared runner, 2.7 times as much. The eight spent 38 at loads of 5 to 6, which projects
#: to about 100 under the same contention. 140 is about a third above that projection and about 3.7
#: times a quiet run: growth below that, such as one module doubling, passes on a quiet host.
BUDGET = 140.0

#: The harness import that makes a module a property check.
HARNESS = "from tests.fixtures.properties"


def property_modules(root: Path = REPO) -> list[Path]:
    """The unit-test modules that use the property harness."""
    return sorted(
        path for path in (root / "tests" / "unit").glob("test_*.py") if HARNESS in path.read_text()
    )


def _fixed_seed_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CLIFFRACER_PROPERTY_SEEDS", None)
    env.pop("CLIFFRACER_PROPERTY_SCALE", None)
    return env


def _children_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def assert_within_budget(modules: list[Path], budget: float, root: Path = REPO) -> float:
    """Run `modules` in a child pytest from `root` and fail when it spends more than `budget`
    seconds of CPU; return the CPU seconds it spent. A run that fails, or passes nothing, fails
    too, and so does an empty list, which a child pytest would read as the whole suite."""
    assert modules, "no property module to measure"
    started = _children_cpu_seconds()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--durations=5",
            *map(str, modules),
        ],
        cwd=root,
        env=_fixed_seed_environment(),
        capture_output=True,
        text=True,
        timeout=budget * 20,
    )
    seconds = _children_cpu_seconds() - started
    out = result.stdout + result.stderr
    passed = re.search(r"(\d+) passed", out)
    assert result.returncode == 0 and passed and int(passed.group(1)) > 0, (
        f"the property checks did not pass on their fixed seeds:\n{out[-3000:]}"
    )
    slowest = out[out.find("slowest") :].split("\n\n")[0] if "slowest" in out else ""
    assert seconds <= budget, (
        f"the property checks spent {seconds:.1f}s of CPU, over their {budget:.0f}s budget "
        f"({', '.join(path.name for path in modules)}):\n{slowest}"
    )
    return seconds


def test_every_module_that_uses_the_property_harness_is_found():
    found = {path.name for path in property_modules()}

    assert "test_a_property_check_fails_on_what_no_known_limit_explains.py" in found, found
    assert len(found) >= 7, found


def test_the_measured_run_is_on_the_fixed_seeds_whatever_the_caller_set(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_PROPERTY_SEEDS", "100")
    monkeypatch.setenv("CLIFFRACER_PROPERTY_SCALE", "10")

    env = _fixed_seed_environment()

    assert "CLIFFRACER_PROPERTY_SEEDS" not in env and "CLIFFRACER_PROPERTY_SCALE" not in env


def test_the_property_checks_run_within_their_budget():
    assert_within_budget(property_modules(), BUDGET)


#: CPU seconds the planted modules burn or sleep: well above what a child pytest spends to start.
PLANTED = 3.0


def test_CONTROL_a_property_module_that_burns_cpu_over_the_budget_fails(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    busy = tmp_path / "test_a_busy_property.py"
    busy.write_text(
        "import time\n\n\ndef test_busy():\n"
        f"    end = time.process_time() + {PLANTED}\n"
        "    while time.process_time() < end:\n        pass\n"
    )

    with pytest.raises(AssertionError, match="over their 2s budget"):
        assert_within_budget([busy], budget=PLANTED - 1, root=tmp_path)
    assert assert_within_budget([busy], budget=30.0, root=tmp_path) >= PLANTED


def test_CONTROL_a_property_module_that_only_sleeps_spends_no_budget(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    sleepy = tmp_path / "test_a_sleepy_property.py"
    sleepy.write_text(f"import time\n\n\ndef test_sleepy():\n    time.sleep({PLANTED})\n")

    assert assert_within_budget([sleepy], budget=PLANTED - 1, root=tmp_path) < PLANTED - 1


def test_CONTROL_a_property_module_that_runs_nothing_fails_the_measurement(tmp_path):
    """A module whose every check skips exits 0 having passed nothing: it is not within budget."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    skipped = tmp_path / "test_a_skipped_property.py"
    skipped.write_text('import pytest\n\n\ndef test_skipped():\n    pytest.skip("not here")\n')

    with pytest.raises(AssertionError, match="did not pass on their fixed seeds"):
        assert_within_budget([skipped], budget=30.0, root=tmp_path)


def test_CONTROL_a_property_module_that_fails_fails_the_measurement(tmp_path):
    """A check that fails fast is not within budget, it is failing: the measurement says so."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    failing = tmp_path / "test_a_failing_property.py"
    failing.write_text("def test_fails():\n    assert False\n")

    with pytest.raises(AssertionError, match="did not pass on their fixed seeds"):
        assert_within_budget([failing], budget=30.0, root=tmp_path)
