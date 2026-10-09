"""Integration tests for the correctness-gap fixes (real NATS)."""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.testing.waiting import wait_until

pytestmark = pytest.mark.integration


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_replicas_load_balance_via_queue_group():
    """Two replicas of the same service+namespace share a queue group, so each RPC
    is handled exactly once (not by both), and load is distributed across replicas."""
    counts = {"a": 0, "b": 0}

    def make(tag):
        class Worker(CliffracerService):
            @rpc
            async def work(self) -> dict[str, str]:
                counts[tag] += 1
                return {"tag": tag}

        return Worker(ServiceConfig(name="worker", namespace="ns1"))

    a = make("a")
    b = make("b")
    caller = CliffracerService(ServiceConfig(name="caller", namespace="ns1"))
    await a.start()
    await b.start()
    await caller.start()
    await asyncio.sleep(0.2)

    try:
        n = 20
        for _ in range(n):
            await caller.call_rpc("worker", "work")
        # each call handled exactly once (queue group, not fan-out double-handling)
        assert counts["a"] + counts["b"] == n
        # load distributed across both replicas
        assert counts["a"] > 0 and counts["b"] > 0
    finally:
        await asyncio.gather(a.stop(), b.stop(), caller.stop())


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_correlation_id_rides_in_nats_headers():
    """publish_event sets correlation_id in the NATS message headers, so a consumer
    that doesn't parse the JSON body can still read it: both header spellings carry the id the
    event was published under."""
    svc = CliffracerService(ServiceConfig(name="pub"))
    await svc.start()

    received = []

    async def _raw_cb(msg):
        received.append(msg.headers)

    await svc.nc.subscribe(HandlerDiscovery.with_namespace(svc.config, "evt.headers"), cb=_raw_cb)
    await svc.nc.flush()

    try:
        await svc.publish_event("evt.headers", n=1, correlation_id="trace-in-the-headers")
        await wait_until(lambda: received, within=10.0, reason="the event to reach the subscriber")
        assert len(received) == 1
        assert received[0] is not None
        assert received[0]["correlation_id"] == "trace-in-the-headers", received[0]
        assert received[0]["X-Correlation-ID"] == "trace-in-the-headers", received[0]
    finally:
        await svc.stop()
