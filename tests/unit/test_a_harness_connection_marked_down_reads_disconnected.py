"""A `ServiceTestHarness` connection marked down reads as disconnected.

The broker state is read from the connection's flags, `is_connecting` and `is_reconnecting`
among them, each with a default of False. The harness's connection is a mock, on which a flag it
never set is an attribute that is truthy, so it has to set every flag the state is read from.
Otherwise a connection marked down reads as connecting, and a service's own lost-broker path
cannot be tested.
"""

from __future__ import annotations

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Quiet(CliffracerService):
    pass


async def test_a_harness_connection_marked_down_reads_disconnected():
    config = ServiceConfig(name="quiet", health_port=0, broker_probe_timeout=1.0)
    async with ServiceTestHarness(Quiet, config=config) as harness:
        harness.service.nc.is_connected = False
        health = await asyncio.wait_for(harness.service.health_check(), timeout=5)

    assert health["broker_state"] == "disconnected", health
    assert health["status"] == "disconnected", health
    assert health["nats_connected"] is False, health


async def test_CONTROL_a_harness_connection_marked_reconnecting_still_reads_connecting():
    """The flags are set, not hidden: a test that marks the connection reconnecting sees it."""
    config = ServiceConfig(name="quiet", health_port=0, broker_probe_timeout=1.0)
    async with ServiceTestHarness(Quiet, config=config) as harness:
        harness.service.nc.is_connected = False
        harness.service.nc.is_reconnecting = True
        health = await asyncio.wait_for(harness.service.health_check(), timeout=5)

    assert health["broker_state"] == "connecting", health
    assert health["status"] == "connecting", health
