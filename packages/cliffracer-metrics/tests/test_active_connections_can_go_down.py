"""`active_connections` is how many are open now: it goes up when one opens and down when one closes."""

import pytest
from cliffracer_metrics import PerformanceMetrics

pytestmark = pytest.mark.unit


def _active(metrics: PerformanceMetrics) -> int:
    return metrics.get_connection_stats()["active_connections"]


def test_it_rises_with_each_open_and_falls_with_each_close():
    metrics = PerformanceMetrics()

    for event, expected in [
        ("connection_opened", 1),
        ("connection_opened", 2),
        ("connection_closed", 1),
        ("connection_opened", 2),
        ("connection_closed", 1),
        ("connection_closed", 0),
    ]:
        metrics.record_connection_event(event)
        assert _active(metrics) == expected, event


def test_a_close_with_none_open_does_not_go_below_zero():
    metrics = PerformanceMetrics()

    metrics.record_connection_event("connection_closed")

    assert _active(metrics) == 0


def test_an_open_also_counts_toward_the_total_and_a_close_does_not_undo_it():
    metrics = PerformanceMetrics()

    for event in ["connection_opened", "connection_opened", "connection_closed"]:
        metrics.record_connection_event(event)

    stats = metrics.get_connection_stats()
    assert (stats["total_connections"], stats["active_connections"]) == (2, 1)


@pytest.mark.parametrize("event", ["total_connections", "failed_connections", "reconnections"])
def test_the_counters_add_one_to_themselves_only(event):
    metrics = PerformanceMetrics()
    before = metrics.get_connection_stats()

    metrics.record_connection_event(event)
    metrics.record_connection_event(event)

    after = metrics.get_connection_stats()
    assert after[event] == before[event] + 2
    assert {k: v for k, v in after.items() if k != event} == {
        k: v for k, v in before.items() if k != event
    }


def test_the_level_can_be_set_outright():
    metrics = PerformanceMetrics()
    metrics.record_connection_event("connection_opened")

    metrics.set_active_connections(7)
    assert _active(metrics) == 7
    metrics.set_active_connections(0)
    assert _active(metrics) == 0


@pytest.mark.parametrize("count", [-1, 1.5, "3", None, True])
def test_a_level_that_is_not_a_count_is_refused_and_leaves_the_last_one(count):
    metrics = PerformanceMetrics()
    metrics.set_active_connections(4)

    with pytest.raises(ValueError, match="active connections must be"):
        metrics.set_active_connections(count)

    assert _active(metrics) == 4


@pytest.mark.parametrize(
    "event", ["disconnected", "closed", "reconnection", "", "TOTAL_CONNECTIONS"]
)
def test_an_event_nobody_records_is_refused_not_dropped(event):
    metrics = PerformanceMetrics()

    with pytest.raises(ValueError, match="unknown connection event"):
        metrics.record_connection_event(event)

    assert metrics.get_connection_stats() == PerformanceMetrics().get_connection_stats()


def test_active_connections_is_not_an_event_and_the_refusal_says_what_to_use():
    metrics = PerformanceMetrics()

    with pytest.raises(ValueError) as caught:
        metrics.record_connection_event("active_connections")

    assert "connection_opened" in str(caught.value), caught.value
    assert "set_active_connections" in str(caught.value), caught.value
    assert _active(metrics) == 0


def test_reset_returns_the_level_to_zero():
    metrics = PerformanceMetrics()
    metrics.record_connection_event("connection_opened")

    metrics.reset_metrics()

    assert _active(metrics) == 0
