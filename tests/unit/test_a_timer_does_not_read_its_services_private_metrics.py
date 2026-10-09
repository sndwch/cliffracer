"""A service's own `_metrics` attribute is the service's, not a hook the timer calls into.

The timer used to read `service._metrics` and call `increment_counter` and `record_custom_metric`
on it when it was truthy. Nothing in the framework sets that attribute, and `_metrics` is a
likely name in application code: a service holding its own counters in a dict made the success
path raise, count an error, and put the timer into its error backoff after one firing.
"""

import pytest

from cliffracer.core.timer import Timer
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit


class Service:
    def __init__(self, metrics) -> None:
        self._metrics = metrics
        self.ran = 0

    async def tick(self) -> None:
        self.ran += 1
        self._metrics["orders_seen"] = self._metrics.get("orders_seen", 0) + 1


@pytest.mark.parametrize("metrics", [{"orders_seen": 0}, {"x": 1}], ids=["zero", "nonempty"])
async def test_a_service_with_its_own_metrics_dict_runs_without_an_error(metrics):
    service = Service(metrics)
    t = Timer(interval=0.1)
    t.method_name = "tick"
    t.service_instance = service

    await t._execute_method()

    assert (service.ran, t.error_count, t.last_error) == (1, 0, None)


async def test_the_loop_keeps_its_schedule_instead_of_backing_off():
    service = Service({"orders_seen": 0})
    clock = FakeClock()
    t = Timer(interval=0.05, eager=True, error_backoff=5.0, clock=clock)
    t.method_name = "tick"
    t.service_instance = service
    await t.start(service)
    clock.watch(t.task)
    try:
        # A firing that raised would put the loop in its 5 s backoff and stop the count at one.
        await clock.advance(0.1)
    finally:
        await t.stop()

    assert service.ran == 3, (service.ran, t.error_count, t.last_error)
    assert t.error_count == 0
