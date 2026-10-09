"""A service under `ServiceTestHarness` reports what its connection says.

Readiness asks the broker for a round trip. The harness's in-memory connection is connected, and
it answers that round trip at once, as a broker that is up does. A connection that never answered
would make `health_check()` wait out `broker_probe_timeout` and report the service disconnected.

The other direction is what a test of a service's own degraded path relies on: with the
connection marked down, or a round trip that fails, the same service is not reported healthy.
"""

from __future__ import annotations

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Quiet(CliffracerService):
    pass


async def test_health_check_under_the_harness_reports_connected():
    config = ServiceConfig(name="quiet", health_port=0, broker_probe_timeout=1.0)
    async with ServiceTestHarness(Quiet, config=config) as harness:
        health = await asyncio.wait_for(harness.service.health_check(), timeout=5)

    assert health["status"] == "healthy", health
    assert health["nats_connected"] is True, health
    assert health["nats_rtt_ms"] is not None, health


async def _refused(future: asyncio.Future[object] | None = None) -> None:
    raise ConnectionError("the broker refused the round trip")


async def test_health_check_under_the_harness_with_its_connection_down_is_not_healthy():
    config = ServiceConfig(name="quiet", health_port=0, broker_probe_timeout=1.0)
    async with ServiceTestHarness(Quiet, config=config) as harness:
        harness.service.nc.is_connected = False
        health = await asyncio.wait_for(harness.service.health_check(), timeout=5)

    # Not pinned to "disconnected": the harness connection leaves `is_connecting` unset, and an
    # unset AsyncMock attribute is truthy, so a connection marked down reads as connecting.
    assert health["status"] != "healthy", health
    assert health["nats_connected"] is False, health


async def test_health_check_under_the_harness_with_a_failing_round_trip_is_not_healthy():
    config = ServiceConfig(name="quiet", health_port=0, broker_probe_timeout=1.0)
    async with ServiceTestHarness(Quiet, config=config) as harness:
        harness.service.nc._send_ping = _refused
        health = await asyncio.wait_for(harness.service.health_check(), timeout=5)

    assert health["status"] == "disconnected", health
    assert health["nats_connected"] is False, health
    assert health["nats_rtt_ms"] is None, health
