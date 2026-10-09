"""Several benchmark runs aggregate into one result without hiding a failed run.

CI runs the battery three times and scores the aggregate. The aggregate is the
last run's record with each metric's median over the runs. A component that
crashed in an earlier run must still fail the regression checker: its
`failures` are carried, and a metric some run did not produce is left out, so
the checker's missing-metric refusal names it.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"
BASELINE = REPO / "benchmark_baseline.json"


def _load_run_benchmarks():
    spec = importlib.util.spec_from_file_location(
        "run_benchmarks_under_test", REPO / "scripts" / "run_benchmarks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_benchmarks = _load_run_benchmarks()


def _clean_run() -> dict:
    data = json.loads(BASELINE.read_text())
    data.pop("failures", None)
    return data


def _check(tmp_path: Path, aggregate: dict) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "aggregate.json"
    path.write_text(json.dumps(aggregate))
    return subprocess.run(
        [sys.executable, str(CHECKER), "--baseline", str(BASELINE), "--current", str(path)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_a_failure_in_an_earlier_run_is_carried(tmp_path):
    runs = [_clean_run() for _ in range(3)]
    del runs[0]["metrics"]["auth"]
    runs[0]["failures"] = {"auth": "RuntimeError: extension crashed"}

    aggregate = run_benchmarks.aggregate_runs(runs)

    assert aggregate["failures"] == {"run 1: auth": "RuntimeError: extension crashed"}
    result = _check(tmp_path, aggregate)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "run 1: auth" in result.stderr


def test_a_metric_one_run_did_not_produce_is_left_out_and_refused(tmp_path):
    """No `failures` recorded, so only the missing metric can fail it."""
    runs = [_clean_run() for _ in range(3)]
    del runs[1]["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"]

    aggregate = run_benchmarks.aggregate_runs(runs)

    assert "failures" not in aggregate
    assert "throughput_msgs_sec" not in aggregate["metrics"]["rpc"]["concurrency_10"]
    result = _check(tmp_path, aggregate)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "rpc.concurrency_10.throughput_msgs_sec" in result.stderr


def test_CONTROL_clean_runs_aggregate_to_the_median_and_pass(tmp_path):
    runs = [_clean_run() for _ in range(3)]
    base = runs[0]["metrics"]["kv"]["bulk_put_ops_sec"]
    for run, factor in zip(runs, (0.99, 1.01, 1.0), strict=True):
        run["metrics"]["kv"]["bulk_put_ops_sec"] = base * factor

    aggregate = run_benchmarks.aggregate_runs(runs)

    assert "failures" not in aggregate
    assert aggregate["runs_aggregated"] == 3
    assert aggregate["metrics"]["kv"]["bulk_put_ops_sec"] == pytest.approx(base, rel=1e-5)
    assert _check(tmp_path, aggregate).returncode == 0


def test_CONTROL_the_last_run_s_record_is_kept():
    runs = [_clean_run() for _ in range(2)]
    runs[-1]["environment"]["runner"]["runner_name"] = "the-last-one"

    aggregate = run_benchmarks.aggregate_runs(copy.deepcopy(runs))

    assert aggregate["environment"]["runner"]["runner_name"] == "the-last-one"
