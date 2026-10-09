"""A listener the service paused on a down dependency reads 1 in the metrics, and 0 once resumed.

The service runs `on_listener_paused` and `on_listener_resumed` through the extension pipeline when
it stops and starts consuming a listener declared with `pause_when_down`. The metrics extension
keeps a gauge per subject from them, reported under `listener_paused` in its `/health` details.
"""

import pytest
from cliffracer_metrics import MetricsExtension

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    metrics = MetricsExtension()


async def test_the_gauge_follows_the_pause_and_resume_hooks():
    async with ServiceTestHarness(Svc, config=ServiceConfig(name="svc", health_port=0)) as h:
        pipeline = h.container.extension_pipeline
        assert "listener_paused" not in (h.service.metrics.health_details() or {})

        await pipeline.run_listener_hook("on_listener_paused", "orders.created", ("db",))
        await pipeline.run_listener_hook("on_listener_paused", "carts.updated", ("cache",))
        assert h.service.metrics.health_details()["listener_paused"] == {
            "carts.updated": 1,
            "orders.created": 1,
        }

        await pipeline.run_listener_hook("on_listener_resumed", "orders.created", ("db",))
        assert h.service.metrics.health_details()["listener_paused"] == {
            "carts.updated": 1,
            "orders.created": 0,
        }


async def test_a_hook_before_setup_records_nothing():
    ext = MetricsExtension()

    await ext.on_listener_paused("orders.created", ("db",))

    assert ext.health_details() is None
