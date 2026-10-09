"""Every recorded benchmark metric says which way it is supposed to move.

The gate scores a metric by comparing it against the baseline, so it has to
know whether a larger number is better. Inferring that from the name works for
throughput and latency and fails silently for anything else: a counter of work
completed, read as lower-is-better, scores a run that did half the work as a
50% improvement.

So a metric with no declared direction is a failure of the gate rather than a
value scored by a default. This holds the committed baseline to that: every
metric in it is decided by a keyword rule, or listed with the reason it is not.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "benchmark_baseline.json"
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"

sys.path.insert(0, str(REPO / "scripts"))

from check_benchmark_regression import (  # noqa: E402
    EXPLICIT_DIRECTIONS,
    KEY_METRICS,
    NOT_A_PERFORMANCE_SIGNAL,
    UnknownMetricDirection,
    flatten_metrics,
    metric_direction,
    undeclared_metrics,
)


def baseline_metrics() -> dict[str, float]:
    return flatten_metrics(json.loads(BASELINE.read_text())["metrics"])


# Written out rather than read from KEY_METRICS: a control that derives its
# expectation from the set it guards passes whatever that set becomes, and the
# point here is that adding or removing a gated metric is a deliberate act.
GATED_METRICS = {
    "rpc.concurrency_10.throughput_msgs_sec",
    "rpc.concurrency_10.p50_latency_ms",
    "rpc.concurrency_100.throughput_msgs_sec",
    "rpc.concurrency_100.p50_latency_ms",
    "rpc.concurrency_1000.throughput_msgs_sec",
    "rpc.concurrency_1000.p50_latency_ms",
    "jetstream.throughput_msgs_sec",
    "jetstream.p50_batch_latency_ms",
    "auth.token_validation_ops_sec",
    "auth.modern_hash_ops_sec",
    "kv.bulk_put_ops_sec",
    "kv.bulk_get_ops_sec",
    "serialization.1KB.msgpack_ser_speedup",
    "serialization.1KB.msgpack_deser_speedup",
    "serialization.100KB.msgpack_ser_speedup",
    "serialization.100KB.msgpack_deser_speedup",
}

REPORTED_NOT_GATED = "auth.legacy_hash_ops_sec"


def test_the_gated_metrics_are_the_ones_named_here():
    assert KEY_METRICS == GATED_METRICS


def test_a_throughput_that_is_recorded_is_not_necessarily_gated():
    """A number whose subject can be deleted to improve it does not belong in the gate.

    The legacy password hash rate is recorded so a reader sees it move, but it
    is not a service level: nothing here promises it, and a threshold on it
    would fail the build for a legacy path nobody is meant to keep fast.
    """
    assert REPORTED_NOT_GATED not in KEY_METRICS
    assert REPORTED_NOT_GATED in baseline_metrics(), (
        "the number is still recorded; it is the gating that is absent"
    )


def test_the_gate_does_not_fail_on_the_ungated_metric(tmp_path: Path):
    """End to end, against the checker's exit status.

    Halving the recorded-but-ungated throughput must not fail the build, while
    halving a gated metric in the same run must -- otherwise this passes because the
    checker fails on nothing at all.
    """
    baseline = json.loads(BASELINE.read_text())

    ungated = json.loads(json.dumps(baseline))
    ungated["metrics"]["auth"]["legacy_hash_ops_sec"] *= 0.5
    assert _checker_exit(tmp_path, "ungated.json", baseline, ungated) == 0

    gated = json.loads(json.dumps(baseline))
    gated["metrics"]["kv"]["bulk_put_ops_sec"] *= 0.5
    assert _checker_exit(tmp_path, "gated.json", baseline, gated) == 1


SERIALIZATION_RATIOS_GATED = [
    ("1KB", "msgpack_ser_speedup"),
    ("1KB", "msgpack_deser_speedup"),
    ("100KB", "msgpack_ser_speedup"),
    ("100KB", "msgpack_deser_speedup"),
]


def _with_ratio_scaled(baseline: dict, size: str, ratio: str, factor: float) -> dict:
    scaled = json.loads(json.dumps(baseline))
    scaled["metrics"]["serialization"][size][ratio] *= factor
    return scaled


@pytest.mark.parametrize(("size", "ratio"), SERIALIZATION_RATIOS_GATED)
def test_a_serialization_speedup_20_percent_below_the_baseline_fails_the_gate(
    tmp_path: Path, size: str, ratio: str
):
    baseline = json.loads(BASELINE.read_text())

    regressed = _with_ratio_scaled(baseline, size, ratio, 0.80)

    assert _checker_exit(tmp_path, "regressed.json", baseline, regressed) == 1


@pytest.mark.parametrize(("size", "ratio"), SERIALIZATION_RATIOS_GATED)
def test_a_serialization_speedup_10_percent_below_the_baseline_passes(
    tmp_path: Path, size: str, ratio: str
):
    """Inside the 15% threshold, so ordinary noise does not fail the build."""
    baseline = json.loads(BASELINE.read_text())

    within_noise = _with_ratio_scaled(baseline, size, ratio, 0.90)

    assert _checker_exit(tmp_path, "noise.json", baseline, within_noise) == 0


@pytest.mark.parametrize(("size", "ratio"), SERIALIZATION_RATIOS_GATED)
def test_a_serialization_speedup_that_rises_passes(tmp_path: Path, size: str, ratio: str):
    baseline = json.loads(BASELINE.read_text())

    improved = _with_ratio_scaled(baseline, size, ratio, 1.5)

    assert _checker_exit(tmp_path, "improved.json", baseline, improved) == 0


@pytest.mark.parametrize("ratio", ["msgpack_ser_speedup", "msgpack_deser_speedup"])
def test_the_one_megabyte_speedups_are_recorded_and_not_gated(tmp_path: Path, ratio: str):
    """The noisiest size, left out on purpose: halving it does not fail the build."""
    baseline = json.loads(BASELINE.read_text())
    assert f"serialization.1MB.{ratio}" in baseline_metrics()
    assert f"serialization.1MB.{ratio}" not in KEY_METRICS

    halved = _with_ratio_scaled(baseline, "1MB", ratio, 0.5)

    assert _checker_exit(tmp_path, "one_megabyte.json", baseline, halved) == 0


def _checker_exit(tmp_path: Path, name: str, baseline: dict, current: dict) -> int:
    base_path = tmp_path / f"baseline_{name}"
    curr_path = tmp_path / name
    base_path.write_text(json.dumps(baseline))
    curr_path.write_text(json.dumps(current))
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(base_path),
            "--current",
            str(curr_path),
            "--threshold",
            "0.15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    ).returncode


def test_every_recorded_metric_declares_a_direction():
    """No metric in the baseline may fall through to a default."""
    metrics = baseline_metrics()
    assert len(metrics) > 20, f"only {len(metrics)} metrics read; the sweep is not reading"

    undeclared = undeclared_metrics(metrics)
    assert undeclared == [], (
        f"{len(undeclared)} baseline metric(s) declare no direction: {undeclared}. "
        "Add each to EXPLICIT_DIRECTIONS with the reason, or to "
        "NOT_A_PERFORMANCE_SIGNAL if a percentage against the baseline says "
        "nothing about it."
    )


def test_every_declared_metric_is_in_the_baseline_and_carries_a_reason():
    """A declaration for a metric nobody records is an exemption nobody watches."""
    recorded = set(baseline_metrics())
    declared = {name: reason for name, (_, reason) in EXPLICIT_DIRECTIONS.items()}
    declared |= dict(NOT_A_PERFORMANCE_SIGNAL)

    assert declared, "nothing is declared; delete the mappings and the branch that reads them"
    for name, reason in sorted(declared.items()):
        assert isinstance(reason, str) and reason.strip(), f"no reason recorded for {name}"
        assert name in recorded, (
            f"{name} is declared but is not in the baseline; remove the entry or "
            "re-record the baseline"
        )


def test_the_keyword_rules_still_decide_the_metrics_they_are_for():
    """The explicit lists are the exception, not the mechanism.

    If the keyword rules stopped matching, every metric would need listing and
    the lists would quietly become the whole rule.
    """
    by_keyword = [
        name
        for name in baseline_metrics()
        if name not in EXPLICIT_DIRECTIONS and name not in NOT_A_PERFORMANCE_SIGNAL
    ]
    assert len(by_keyword) > len(EXPLICIT_DIRECTIONS), (
        f"only {len(by_keyword)} metrics are decided by keyword against "
        f"{len(EXPLICIT_DIRECTIONS)} listed individually; the rules have stopped working"
    )
    for name in by_keyword:
        metric_direction(name)


def test_a_counter_of_completed_work_is_higher_is_better():
    """The case this exists for, asserted by name rather than left to a keyword."""
    for name in (
        "jetstream.messages_acked",
        "jetstream.messages_consumed",
        "kv.items_processed",
    ):
        assert metric_direction(name) is True, f"{name} is scored as lower-is-better"


def test_a_deliberately_slow_metric_is_lower_is_better():
    """And the converse, so the rule is not simply inverted."""
    assert metric_direction("auth.modern_hash_ops_sec") is False
    assert metric_direction("auth.token_validation_ops_sec") is True


def test_CONTROL_an_unclassified_metric_raises():
    """A name nothing decides must not resolve to a direction."""
    with pytest.raises(UnknownMetricDirection):
        metric_direction("zqx.some_unclassified_counter")

    assert undeclared_metrics(["zqx.some_unclassified_counter"]) == [
        "zqx.some_unclassified_counter"
    ]


def test_CONTROL_the_gate_refuses_a_baseline_carrying_an_undeclared_metric(tmp_path: Path):
    """End to end: the checker's exit status, not a helper's return.

    A baseline with a metric nothing classifies must fail the gate rather than
    score it by a default.
    """
    baseline = json.loads(BASELINE.read_text())
    baseline["metrics"]["zqx"] = {"some_unclassified_counter": 100.0}
    doctored = tmp_path / "baseline.json"
    doctored.write_text(json.dumps(baseline))

    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(doctored),
            "--current",
            str(doctored),
            "--threshold",
            "0.15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "declare no direction" in result.stderr, result.stderr
    assert "zqx.some_unclassified_counter" in result.stderr, result.stderr


def test_CONTROL_an_unchanged_run_still_passes(tmp_path: Path):
    """Without this, a gate that failed everything would satisfy the test above."""
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(BASELINE),
            "--current",
            str(BASELINE),
            "--threshold",
            "0.15",
            "--all-metrics",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stdout + result.stderr
