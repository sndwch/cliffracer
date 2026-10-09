"""What `PerformanceMetrics` decides and reports, read value by value.

The summary test asserted that four of its keys were present. The class makes decisions (each
target's verdict, the outcome split, the overall status) and reports collected numbers, and a
dropped section or a flipped comparison left that test green. A fake clock makes the per-second
throughput window deterministic.
"""

import pytest
from cliffracer_metrics.metrics import PerformanceMetrics

pytestmark = pytest.mark.unit


class FakeClock:
    """The `time` module `metrics.py` reads, with a clock the test moves."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("cliffracer_metrics.metrics.time", fake)
    return fake


SUMMARY_KEYS = {
    "timestamp",
    "latency",
    "throughput",
    "resources",
    "connections",
    "custom",
    "targets",
    "metrics_history_size",
}


def test_the_summary_has_exactly_these_sections_and_each_is_the_one_its_name_says(clock):
    m = PerformanceMetrics()
    m.record_latency(12.5)
    m.record_latency(7.5, success=False)
    m.record_memory_usage(120.0)
    m.record_cpu_usage(35.0)
    m.record_connection_event("connection_opened")
    m.increment_counter("jobs", 3)
    m.set_gauge("depth", 4.5)

    summary = m.get_performance_summary()

    assert set(summary) == SUMMARY_KEYS
    assert summary["timestamp"] == clock.now
    assert summary["latency"] == m.get_latency_stats()
    assert summary["throughput"] == m.get_throughput_stats()
    assert summary["resources"] == m.get_resource_stats()
    assert summary["connections"] == m.get_connection_stats()
    assert summary["custom"] == m.get_custom_metrics()
    assert summary["targets"] == m.check_performance_targets()
    assert summary["metrics_history_size"] == 2
    # And the sections hold what was recorded, not an empty shell of the right name.
    assert summary["latency"]["count"] == 2
    assert summary["resources"]["memory"]["current_mb"] == 120.0
    assert summary["connections"]["active_connections"] == 1
    assert summary["custom"]["counters"] == {"jobs": 3}


def test_the_latency_summary_values():
    m = PerformanceMetrics()
    for value in (7.5, 12.5, 0.5):
        m.record_latency(value)
    m.record_latency(20.0, success=False)

    stats = m.get_latency_stats()

    assert (stats["count"], stats["min_ms"], stats["max_ms"]) == (4, 0.5, 20.0)
    assert stats["mean_ms"] == pytest.approx(10.125)
    assert stats["median_ms"] == pytest.approx(10.0)
    assert stats["success_count"] == 3
    assert stats["success_rate_percent"] == 75.0
    assert stats["sub_millisecond_count"] == 1
    assert stats["sub_millisecond_percent"] == 25.0


def test_no_latency_data_is_said_so_rather_than_zeroed():
    assert PerformanceMetrics().get_latency_stats() == {"error": "No latency data available"}


# ---- the verdicts --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p95_ms", "passing"),
    [(9.0, True), (10.0, True), (10.5, False)],
    ids=["under", "at the target", "over"],
)
def test_the_latency_verdict_flips_at_the_target(p95_ms, passing):
    m = PerformanceMetrics()
    m.record_latency(p95_ms)

    assert m.check_performance_targets()["latency"] == {
        "target": 10.0,
        "actual": p95_ms,
        "passing": passing,
    }


@pytest.mark.parametrize(
    ("errors", "passing"),
    [(1, True), (2, False)],
    ids=["99% is the target and passes", "98% fails"],
)
def test_the_success_rate_verdict_flips_at_the_target(errors, passing):
    m = PerformanceMetrics()
    for _ in range(100 - errors):
        m.record_latency(1.0)
    for _ in range(errors):
        m.record_latency(1.0, success=False)

    check = m.check_performance_targets()["success_rate"]

    assert check["actual"] == 100.0 - errors
    assert check["target"] == 99.0
    assert check["passing"] is passing


@pytest.mark.parametrize(("current_mb", "passing"), [(499.0, True), (500.0, True), (501.0, False)])
def test_the_memory_verdict_flips_at_the_target(current_mb, passing):
    m = PerformanceMetrics()
    m.record_memory_usage(current_mb)

    assert m.check_performance_targets()["memory"] == {
        "target": 500.0,
        "actual": current_mb,
        "passing": passing,
    }


def test_the_memory_verdict_is_the_current_sample_not_the_peak():
    m = PerformanceMetrics()
    m.record_memory_usage(900.0)
    m.record_memory_usage(100.0)

    check = m.check_performance_targets()["memory"]

    assert (check["actual"], check["passing"]) == (100.0, True)


@pytest.mark.parametrize(("per_second", "passing"), [(150, True), (100, True), (50, False)])
def test_the_throughput_verdict_is_the_average_over_the_completed_seconds(
    clock, per_second, passing
):
    m = PerformanceMetrics()
    for second in range(3):
        clock.now = 1000.0 + second
        for _ in range(per_second):
            m.record_latency(1.0)
    clock.now = 1003.0
    m.record_latency(1.0)  # rolls the third second into the window

    stats = m.get_throughput_stats()
    check = m.check_performance_targets()["throughput"]

    assert stats["average_rps"] == per_second
    assert stats["max_rps"] == per_second
    assert check == {"target": 100.0, "actual": float(per_second), "passing": passing}


def test_the_throughput_verdict_is_the_average_not_the_busiest_second(clock):
    """One burst second does not make a sustained rate: 200, 20 and 20 requests average 80, under
    the 100 target, while the peak of 200 is over it."""
    m = PerformanceMetrics()
    for second, count in enumerate((200, 20, 20)):
        clock.now = 1000.0 + second
        for _ in range(count):
            m.record_latency(1.0)
    clock.now = 1003.0
    m.record_latency(1.0)

    stats = m.get_throughput_stats()

    assert (stats["average_rps"], stats["max_rps"]) == (80.0, 200)
    assert m.check_performance_targets()["throughput"]["passing"] is False


def test_the_overall_status_counts_what_passed_and_fails_if_any_check_does():
    m = PerformanceMetrics()
    m.record_latency(5.0)  # latency passes, success rate passes
    m.record_memory_usage(900.0)  # memory fails
    # no throughput history: the current second's count is the average, 1 request < 100

    checks = m.check_performance_targets()

    assert {name: c["passing"] for name, c in checks.items() if name != "overall"} == {
        "latency": True,
        "success_rate": True,
        "throughput": False,
        "memory": False,
    }
    assert checks["overall"] == {"passing": False, "checks_passed": 2, "total_checks": 4}


def test_a_fresh_collector_has_one_check_and_it_fails():
    """Throughput is always checked, so the overall status is never `all([])` and never a
    default pass: with nothing recorded it is one check, against 100 requests a second."""
    checks = PerformanceMetrics().check_performance_targets()

    assert set(checks) == {"throughput", "overall"}
    assert checks["overall"] == {"passing": False, "checks_passed": 0, "total_checks": 1}


# ---- outcomes ------------------------------------------------------------------------------


def test_the_outcomes_are_split_into_success_error_and_timeout():
    m = PerformanceMetrics()
    m.record_latency(1.0)
    m.record_latency(1.0)
    m.record_latency(1.0, success=False)
    m.record_latency(1.0, success=False, timeout=True)
    # A timeout is counted as one even when the caller also passed success.
    m.record_latency(1.0, success=True, timeout=True)

    stats = m.get_throughput_stats()

    assert (stats["success_requests"], stats["error_requests"], stats["timeout_requests"]) == (
        2,
        1,
        2,
    )
    assert stats["total_requests"] == 5
    assert stats["success_rate_percent"] == 40.0


def test_no_requests_is_a_zero_rate_not_a_division():
    assert PerformanceMetrics().get_throughput_stats()["success_rate_percent"] == 0


# ---- resources, counters, gauges, reset ----------------------------------------------------


def test_the_resource_stats_are_the_samples_summarised():
    m = PerformanceMetrics()
    for mb in (100.0, 300.0, 200.0):
        m.record_memory_usage(mb)
    for pct in (10.0, 50.0):
        m.record_cpu_usage(pct)

    stats = m.get_resource_stats()

    assert stats["memory"] == {
        "current_mb": 200.0,
        "average_mb": 200.0,
        "max_mb": 300.0,
        "min_mb": 100.0,
    }
    assert stats["cpu"] == {
        "current_percent": 50.0,
        "average_percent": 30.0,
        "max_percent": 50.0,
        "min_percent": 10.0,
    }
    assert PerformanceMetrics().get_resource_stats() == {
        "memory": {"error": "No memory data"},
        "cpu": {"error": "No CPU data"},
    }


def test_counters_add_up_and_a_gauge_keeps_the_last_value_with_its_time(clock):
    m = PerformanceMetrics()
    m.increment_counter("jobs")
    m.increment_counter("jobs", 4)
    m.increment_counter("retries", 2)
    m.set_gauge("depth", 3.0)
    clock.now += 5
    m.set_gauge("depth", 7.5)

    custom = m.get_custom_metrics()

    assert custom["counters"] == {"jobs": 5, "retries": 2}
    assert custom["gauges"] == {"depth": {"value": 7.5, "timestamp": 1005.0}}


def test_the_custom_metrics_are_a_copy_a_caller_cannot_write_through():
    m = PerformanceMetrics()
    m.increment_counter("jobs")
    m.set_gauge("depth", 1.0)

    custom = m.get_custom_metrics()
    custom["counters"]["jobs"] = 99
    custom["gauges"]["depth"] = {"value": 99.0}
    m.get_connection_stats()["total_connections"] = 99

    assert m.get_custom_metrics()["counters"] == {"jobs": 1}
    assert m.get_custom_metrics()["gauges"]["depth"]["value"] == 1.0
    assert m.get_connection_stats()["total_connections"] == 0


def test_reset_returns_every_section_to_its_empty_state(clock):
    m = PerformanceMetrics()
    for _ in range(3):
        m.record_latency(5.0, success=False)
    clock.now += 1
    m.record_latency(5.0)
    m.record_memory_usage(10.0)
    m.record_cpu_usage(10.0)
    m.record_connection_event("connection_opened")
    m.increment_counter("jobs")
    m.set_gauge("depth", 1.0)

    m.reset_metrics()
    summary = m.get_performance_summary()

    assert summary["latency"] == {"error": "No latency data available"}
    assert summary["throughput"] == {
        "current_rps": 0,
        "average_rps": 0.0,
        "max_rps": 0,
        "total_requests": 0,
        "success_requests": 0,
        "error_requests": 0,
        "timeout_requests": 0,
        "success_rate_percent": 0,
    }
    assert summary["resources"] == {
        "memory": {"error": "No memory data"},
        "cpu": {"error": "No CPU data"},
    }
    assert summary["connections"] == {
        "total_connections": 0,
        "active_connections": 0,
        "failed_connections": 0,
        "reconnections": 0,
    }
    assert summary["custom"] == {"gauges": {}, "counters": {}}
    assert summary["metrics_history_size"] == 0
