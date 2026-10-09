"""`PerformanceMetrics` counts a request one way and follows the clock.

A timeout recorded with the default `success=True` was a timeout in the lifetime counts and a
success in the latency window, so the two `success_rate_percent` keys disagreed and the verdict
graded the service on the one that ignored timeouts. The throughput window rotated only on a
write, so `current_rps` stayed at the last busy second for ever, idle seconds were dropped from
the average, and the construction second added a zero sample.
"""

import pytest
from cliffracer_metrics.metrics import PerformanceMetrics

pytestmark = pytest.mark.unit


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("cliffracer_metrics.metrics.time", fake)
    return fake


@pytest.mark.parametrize(
    ("success", "timeout", "outcome"),
    [
        (True, False, "success"),
        (False, False, "error"),
        (True, True, "timeout"),
        (False, True, "timeout"),
    ],
)
def test_a_request_has_one_outcome_in_every_count(clock, success, timeout, outcome):
    m = PerformanceMetrics()
    m.record_latency(5.0, success=success, timeout=timeout)

    counts = m.get_throughput_stats()
    latency = m.get_latency_stats()

    assert counts[f"{outcome}_requests"] == 1
    assert sum(counts[k] for k in ("success_requests", "error_requests", "timeout_requests")) == 1
    assert latency["success_count"] == (1 if outcome == "success" else 0)
    assert latency["success_rate_percent"] == counts["success_rate_percent"]


def test_a_timeout_recorded_with_the_default_success_is_not_a_success_in_either_rate(clock):
    m = PerformanceMetrics()
    m.record_latency(50.0, timeout=True)
    m.record_latency(1.0)

    assert m.get_latency_stats()["success_rate_percent"] == 50.0
    assert m.get_throughput_stats()["success_rate_percent"] == 50.0
    assert m.check_performance_targets()["success_rate"] == {
        "target": 99.0,
        "actual": 50.0,
        "passing": False,
    }


def test_the_latency_rate_covers_the_window_and_the_throughput_rate_every_request(clock):
    m = PerformanceMetrics(history_size=2)
    m.record_latency(1.0, success=False)
    m.record_latency(1.0)
    m.record_latency(1.0)

    assert m.get_latency_stats()["success_rate_percent"] == 100.0
    assert m.get_throughput_stats()["success_rate_percent"] == pytest.approx(200 / 3)


def test_current_rps_falls_to_zero_once_traffic_stops(clock):
    m = PerformanceMetrics()
    for _ in range(100):
        m.record_latency(1.0)
    clock.now += 3600

    stats = m.get_throughput_stats()

    assert (stats["current_rps"], stats["average_rps"], stats["max_rps"]) == (0, 0.0, 0)


def test_idle_seconds_after_traffic_count_as_zeros_in_the_average(clock):
    m = PerformanceMetrics()
    for _ in range(100):
        m.record_latency(1.0)
    clock.now += 3

    stats = m.get_throughput_stats()

    assert stats["current_rps"] == 0
    assert stats["max_rps"] == 100
    assert stats["average_rps"] == pytest.approx(100 / 3)


def test_a_burst_and_a_late_request_do_not_average_to_half_the_burst(clock):
    m = PerformanceMetrics()
    for _ in range(100):
        m.record_latency(1.0)
    clock.now += 59
    m.record_latency(1.0)

    stats = m.get_throughput_stats()

    assert stats["average_rps"] == pytest.approx(100 / 59)


def test_current_rps_is_the_last_completed_second_not_the_one_in_progress(clock):
    m = PerformanceMetrics()
    for _ in range(5):
        m.record_latency(1.0)
    clock.now += 1
    for _ in range(2):
        m.record_latency(1.0)

    stats = m.get_throughput_stats()

    assert (stats["current_rps"], stats["average_rps"]) == (5, 5.0)


def test_the_idle_time_before_the_first_request_is_not_a_zero_sample(clock):
    m = PerformanceMetrics()
    clock.now += 10
    m.record_latency(1.0)
    clock.now += 1
    m.record_latency(1.0)

    assert m.get_throughput_stats()["average_rps"] == 1.0


def test_a_reset_does_not_leave_a_stale_second_behind(clock):
    m = PerformanceMetrics()
    m.record_latency(1.0)
    clock.now += 100
    m.reset_metrics()
    m.record_latency(1.0)
    clock.now += 1
    m.record_latency(1.0)

    assert m.get_throughput_stats()["average_rps"] == 1.0


def test_a_read_before_any_request_reports_zero_and_a_clock_that_goes_back_does_not_break_it(clock):
    m = PerformanceMetrics()
    assert m.get_throughput_stats()["current_rps"] == 0
    m.record_latency(1.0)
    clock.now -= 5

    m.record_latency(1.0)

    assert m.get_throughput_stats()["total_requests"] == 2


def test_record_custom_metric_reads_back_as_a_gauge(clock):
    m = PerformanceMetrics()

    m.record_custom_metric("depth", 3.0)
    m.record_custom_metric("depth", 5.0)

    assert m.get_custom_metrics()["gauges"]["depth"]["value"] == 5.0
