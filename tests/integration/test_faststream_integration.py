"""Integration tests for FastStream hosted on Cliffracer over live NATS."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from cliffracer_faststream import FastStreamExtension
from faststream import Context
from faststream.nats import NatsRouter

from cliffracer import CliffracerService, ServiceConfig
from tests.conftest import broker_url


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_faststream_hosted_service_e2e_messaging() -> None:
    """A FastStream router mounted on Cliffracer receives events and accesses injected context."""
    received_events: list[dict[str, Any]] = []
    received_services: list[str] = []
    event_handled = asyncio.Event()

    router = NatsRouter()

    @router.subscriber("orders.incoming")
    async def handle_incoming(
        data: dict[str, Any],
        service: Any = Context("service"),
    ) -> None:
        received_events.append(data)
        received_services.append(service.config.name)
        event_handled.set()

    fs_ext = FastStreamExtension(router=router)

    class HostedService(CliffracerService):
        faststream = fs_ext

    cfg = ServiceConfig(
        name="faststream_host_svc",
        nats_url=broker_url(),
        health_port=0,
    )
    svc = HostedService(cfg)

    await svc.start()
    try:
        # Publish a message directly over the service's NATS connection
        assert svc.container.nc is not None
        await svc.container.nc.publish(
            "orders.incoming",
            b'{"order_id": 999, "status": "pending"}',
        )

        await asyncio.wait_for(event_handled.wait(), timeout=5.0)

        assert len(received_events) == 1
        assert received_events[0] == {"order_id": 999, "status": "pending"}
        assert received_services == ["faststream_host_svc"]

        # Check health details
        health = await svc.health_check()
        assert health["status"] == "healthy"
        fs_health = health["faststream"]
        assert fs_health["status"] == "running"
        assert "orders.incoming" in fs_health["active_routes"]
    finally:
        await svc.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_faststream_graceful_drain_during_service_shutdown() -> None:
    """In-flight FastStream worker completes cleanly before service shutdown finalizes."""
    handler_started = asyncio.Event()
    handler_finished = False

    router = NatsRouter()

    @router.subscriber("work.slow")
    async def slow_work(msg: dict[str, Any]) -> None:
        nonlocal handler_finished
        handler_started.set()
        await asyncio.sleep(0.3)
        handler_finished = True

    fs_ext = FastStreamExtension(router=router)

    class WorkService(CliffracerService):
        faststream = fs_ext

    cfg = ServiceConfig(
        name="work_drain_svc",
        nats_url=broker_url(),
        health_port=0,
        shutdown_timeout=5.0,
    )
    svc = WorkService(cfg)

    await svc.start()
    try:
        assert svc.container.nc is not None
        await svc.container.nc.publish("work.slow", b'{"task": 1}')
        await asyncio.wait_for(handler_started.wait(), timeout=3.0)

        # Stop service while work is still sleeping
        await svc.stop()
        assert handler_finished is True
    except Exception:
        await svc.stop()
        raise
