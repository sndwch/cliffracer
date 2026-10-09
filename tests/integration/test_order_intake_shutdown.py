"""A closing warehouse finishes accepted work while refusing new orders."""

import asyncio
import json
import uuid

import nats.errors
import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def test_closing_warehouse_finishes_accepted_order_with_intake_closed(nats_connection):
    accepted = asyncio.Event()
    release = asyncio.Event()
    intake_closed = asyncio.Event()
    packed = []

    class Warehouse(CliffracerService):
        @rpc
        async def pack(self, order: str) -> str:
            if order == "accepted":
                accepted.set()
                await release.wait()
            packed.append(order)
            return order

    service = Warehouse(ServiceConfig(name="warehouse_" + uuid.uuid4().hex, health_port=0))
    subject = HandlerDiscovery.outbound_subject(service.config, service.config.name, "rpc", "pack")
    drain_tasks = service.container.lifecycle.drain_active_tasks

    async def finish_orders(*, timeout):
        await service.nc.flush()
        intake_closed.set()
        await drain_tasks(timeout=timeout)

    service.container.lifecycle.drain_active_tasks = finish_orders
    tasks = []
    try:
        await service.start()
        first = asyncio.create_task(
            nats_connection.request(subject, b'{"order":"accepted"}', timeout=5)
        )
        tasks.append(first)
        await asyncio.wait_for(accepted.wait(), timeout=2)
        stopping = asyncio.create_task(service.stop())
        tasks.append(stopping)
        await asyncio.wait_for(intake_closed.wait(), timeout=2)
        assert not first.done()
        assert not stopping.done()
        with pytest.raises(nats.errors.NoRespondersError):
            await nats_connection.request(subject, b'{"order":"late"}', timeout=2)
        release.set()
        reply = await first
        assert json.loads(reply.data)["result"] == "accepted"
        await asyncio.wait_for(stopping, timeout=2)
        assert packed == ["accepted"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.stop()
