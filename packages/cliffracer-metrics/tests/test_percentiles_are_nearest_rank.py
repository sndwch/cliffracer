"""A percentile of n samples is the sample at its nearest rank, not the one above it."""

import random

import pytest
from cliffracer_metrics import PerformanceMetrics

pytestmark = pytest.mark.unit


def _stats(latencies: list[float]) -> dict:
    metrics = PerformanceMetrics(history_size=max(len(latencies), 1))
    for value in latencies:
        metrics.record_latency(value)
    return metrics.get_latency_stats()


@pytest.mark.parametrize(
    ("count", "p95", "p99"),
    [
        (1, 1, 1),
        (2, 2, 2),
        (20, 19, 20),
        (50, 48, 50),
        (100, 95, 99),
        (101, 96, 100),
        (1000, 950, 990),
    ],
)
def test_p95_and_p99_of_one_to_n_are_their_nearest_ranks(count, p95, p99):
    stats = _stats([float(i) for i in range(1, count + 1)])

    assert (stats["p95_ms"], stats["p99_ms"]) == (p95, p99)


def test_one_outlier_among_twenty_does_not_set_p95():
    """With the rank one too high, p95 of 20 samples was the maximum."""
    stats = _stats([1.0] * 19 + [500.0])

    assert stats["p95_ms"] == 1.0
    assert stats["max_ms"] == 500.0
    assert stats["p99_ms"] == 500.0


def test_the_order_samples_arrive_in_does_not_matter():
    values = [float(i) for i in range(1, 101)]
    random.Random(7).shuffle(values)

    assert _stats(values)["p95_ms"] == 95.0


def test_equal_samples_have_that_value_at_every_percentile():
    stats = _stats([3.0] * 10)

    assert (stats["median_ms"], stats["p95_ms"], stats["p99_ms"]) == (3.0, 3.0, 3.0)


@pytest.mark.parametrize(
    ("percentile", "expected"), [(0.0, 1.0), (0.01, 1.0), (0.5, 50.0), (1.0, 100.0), (1.5, 100.0)]
)
def test_the_ends_of_the_range_stay_inside_the_samples(percentile, expected):
    assert (
        PerformanceMetrics()._percentile([float(i) for i in range(1, 101)], percentile) == expected
    )


def test_an_empty_list_has_a_percentile_of_zero():
    assert PerformanceMetrics()._percentile([], 0.95) == 0.0


def test_the_latency_target_is_judged_on_the_nearest_rank_p95():
    """1..100 ms against a 95 ms target: p95 is 95, so it passes; it read 96 and failed."""
    metrics = PerformanceMetrics(history_size=100)
    for value in range(1, 101):
        metrics.record_latency(float(value))
    metrics.targets["max_latency_ms"] = 95.0

    check = metrics.check_performance_targets()["latency"]

    assert check == {"target": 95.0, "actual": 95.0, "passing": True}
