"""A benchmark run on a different machine from the baseline is not scored.

Its numbers compared to the baseline report the machine as a change in the code.
That is not a regression, and it is not a pass: the gate refuses with exit 2,
names each field that differs, and prints no comparison. A kernel or interpreter
patch counts. Failing closed turns such an update on the benchmark host into a
red job until the baseline is recorded there again, instead of a gate that
passes every regression without saying so.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"
BASELINE = REPO / "benchmark_baseline.json"

EXIT_NOT_SCORED = 2


def _baseline() -> dict:
    return json.loads(BASELINE.read_text())


def _run(tmp_path: Path, *currents: dict) -> subprocess.CompletedProcess[str]:
    paths = []
    for index, data in enumerate(currents):
        path = tmp_path / f"current_{index}.json"
        path.write_text(json.dumps(data))
        paths.append(str(path))
    return subprocess.run(
        [sys.executable, str(CHECKER), "--baseline", str(BASELINE), "--current", *paths],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def _regress(data: dict) -> dict:
    for level in ("concurrency_10", "concurrency_100", "concurrency_1000"):
        data["metrics"]["rpc"][level]["throughput_msgs_sec"] *= 0.2
    return data


def _with(data: dict, path: str, value) -> dict:
    target = data
    *parents, leaf = path.split(".")
    for key in parents:
        target = target[key]
    target[leaf] = value
    return data


PATCH_LEVEL_CHANGES = [
    ("kernel", "environment.runner.os", "Linux 7.0.0-31-generic"),
    ("interpreter", "environment.runner.python_version", "3.13.3"),
    ("platform_python", "platform.python", "3.13.3"),
    ("cpu_arch", "environment.runner.cpu_arch", "aarch64"),
]


def test_the_fixture_values_differ_from_the_baseline():
    """Otherwise the cases below would compare the baseline with itself."""
    base = _baseline()
    for _, path, value in PATCH_LEVEL_CHANGES:
        current = base
        for key in path.split("."):
            current = current[key]
        assert current != value, path


@pytest.mark.parametrize(
    ("path", "value"),
    [c[1:] for c in PATCH_LEVEL_CHANGES],
    ids=[c[0] for c in PATCH_LEVEL_CHANGES],
)
def test_a_machine_difference_alone_is_not_scored(tmp_path, path, value):
    """No regression at all: the refusal is about the machine, not the numbers."""
    result = _run(tmp_path, _with(_baseline(), path, value))

    assert result.returncode == EXIT_NOT_SCORED, result.stdout + result.stderr
    assert "NOT SCORED" in result.stderr
    assert f"{path}: baseline " in result.stderr, result.stderr
    assert repr(value) in result.stderr, result.stderr
    assert "SUCCESS" not in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("path", "value"),
    [c[1:] for c in PATCH_LEVEL_CHANGES],
    ids=[c[0] for c in PATCH_LEVEL_CHANGES],
)
def test_a_regression_on_a_different_machine_is_not_passed(tmp_path, path, value):
    """Each of these used to exit 0 with the regressions listed and ignored."""
    result = _run(tmp_path, _with(_regress(_baseline()), path, value))

    assert result.returncode == EXIT_NOT_SCORED, result.stdout + result.stderr
    assert "regressed beyond" not in result.stdout, result.stdout


def test_any_of_several_runs_on_a_different_machine_is_not_scored(tmp_path):
    same = _baseline()
    other = _with(_baseline(), "environment.runner.os", "Linux 7.0.0-31-generic")

    result = _run(tmp_path, same, other)

    assert result.returncode == EXIT_NOT_SCORED, result.stdout + result.stderr
    assert "run 2 was measured on a different machine" in result.stderr, result.stderr


def test_CONTROL_a_regression_on_the_same_machine_fails(tmp_path):
    result = _run(tmp_path, _regress(_baseline()))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "regressed beyond" in result.stdout
    assert "NOT SCORED" not in result.stderr


def test_CONTROL_the_same_machine_without_a_regression_passes(tmp_path):
    result = _run(tmp_path, _baseline())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS" in result.stdout


def test_CONTROL_a_run_that_records_no_runner_block_is_compared(tmp_path):
    """An absent reading says nothing about the machine, so it is not a difference."""
    data = _baseline()
    del data["environment"]["runner"]
    del data["platform"]

    result = _run(tmp_path, data)

    assert result.returncode == 0, result.stdout + result.stderr
