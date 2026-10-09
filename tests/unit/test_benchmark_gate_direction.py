"""Verify the regression gate scores each metric in the direction its subject runs.

`is_higher_better_metric` reads the metric's NAME: anything containing
`ops_sec` counts more-per-second as better. A password KDF is deliberately
slow, so more hashes per second means a lower work factor -- the regression
itself. Under the name rule alone, gutting PBKDF2 scored as an improvement and
made the gate greener.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"
BASELINE = REPO / "benchmark_baseline.json"

sys.path.insert(0, str(REPO / "scripts"))
from check_benchmark_regression import (  # noqa: E402
    EXPLICIT_DIRECTIONS,
    is_higher_better_metric,
)

# Measured with pbkdf2_iterations dropped from 100_000 to 100.
WEAKENED_KDF_OPS_SEC = 30198.25


def _run_checker(current: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the checker the way CI does, as a process with an exit status."""
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(BASELINE),
            "--current",
            str(current),
            "--threshold",
            "0.15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def _baseline_with(metric_path: tuple[str, ...], value: float, tmp_path: Path) -> Path:
    data = json.loads(BASELINE.read_text())
    node = data["metrics"]
    for key in metric_path[:-1]:
        node = node[key]
    node[metric_path[-1]] = value
    out = tmp_path / "current.json"
    out.write_text(json.dumps(data))
    return out


def test_a_password_kdf_is_not_scored_as_throughput():
    assert not is_higher_better_metric("auth.modern_hash_ops_sec")


def test_the_name_rule_still_applies_to_everything_else():
    """The override must not have flipped the ordinary cases."""
    assert is_higher_better_metric("auth.token_validation_ops_sec")
    assert is_higher_better_metric("rpc.concurrency_10.throughput_msgs_sec")
    assert not is_higher_better_metric("rpc.concurrency_10.p50_latency_ms")


def test_every_override_states_why():
    empty = [name for name, (_, reason) in EXPLICIT_DIRECTIONS.items() if not reason.strip()]
    assert empty == [], f"overrides without a stated reason: {empty}"


def test_the_gate_fails_a_weakened_password_kdf(tmp_path: Path):
    """The end that matters: a process exit status, not a helper's return value.

    The value is what the benchmark records with the KDF's work factor cut by
    a thousand. Before the direction was stated, this run passed the gate.
    """
    current = _baseline_with(("auth", "modern_hash_ops_sec"), WEAKENED_KDF_OPS_SEC, tmp_path)
    result = _run_checker(current)
    assert result.returncode != 0, (
        f"a weakened KDF passed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert "auth.modern_hash_ops_sec" in result.stdout


def test_CONTROL_an_unchanged_run_passes(tmp_path: Path):
    """So the test above is not passing because the checker fails on everything."""
    current = tmp_path / "current.json"
    current.write_text(BASELINE.read_text())
    result = _run_checker(current)
    assert result.returncode == 0, f"the baseline does not pass against itself:\n{result.stdout}"


def test_CONTROL_a_slower_kdf_still_passes(tmp_path: Path):
    """Lower is better here, so a KDF that got slower is not a regression."""
    data = json.loads(BASELINE.read_text())
    slower = data["metrics"]["auth"]["modern_hash_ops_sec"] * 0.5
    current = _baseline_with(("auth", "modern_hash_ops_sec"), slower, tmp_path)
    result = _run_checker(current)
    assert result.returncode == 0, f"a slower KDF was reported as a regression:\n{result.stdout}"
