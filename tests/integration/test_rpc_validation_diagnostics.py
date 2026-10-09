"""Sensitive order validation diagnostics across a live broker."""

import asyncio
import json

import pytest
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.validation import deserialize_payload, serialize_payload
from tests.fixtures.rpc_validation_diagnostics import (
    CANARY,
    OrderService,
    diagnostic_canaries,
    invalid_orders,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.mark.parametrize("policy", ["full", "redacted"])
@pytest.mark.parametrize("format", ["json", "msgpack"])
@pytest.mark.parametrize("kind", ["rpc", "async"])
async def test_order_validation_policy_reaches_broker_consumers(
    nats_connection, policy, format, kind
):
    service = OrderService(
        ServiceConfig(name="orders_validation", health_port=0, rpc_validation_errors=policy)
    )
    service.accepted = []
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), format="{message}")
    try:
        await service.start()
        subject = HandlerDiscovery.with_namespace(service.config, f"orders_validation.{kind}.place")
        for case, payload in invalid_orders():
            service.diagnostics.ready.clear()
            data, content_type = serialize_payload(payload, format=format)
            headers = {"Content-Type": content_type, "X-Correlation-ID": "orders-validation"}
            if kind == "rpc":
                reply = await nats_connection.request(subject, data, headers=headers, timeout=3)
                response = deserialize_payload(
                    reply.data, content_type=reply.headers["Content-Type"]
                )
                assert response["code"] == "validation_failed", case
                assert response["correlation_id"] == "orders-validation"
                assert diagnostic_canaries(response) == ({CANARY} if policy == "full" else set())
            else:
                await nats_connection.publish(subject, data, headers=headers)
            await asyncio.wait_for(service.diagnostics.ready.wait(), timeout=3)
            assert diagnostic_canaries(service.diagnostics.records[-1]) == (
                {CANARY} if policy == "full" else set()
            ), case
            assert service.accepted == []
        assert len(service.diagnostics.records) == len(invalid_orders())
        if policy == "redacted":
            assert diagnostic_canaries(logs) == set()
        elif kind == "async":
            assert diagnostic_canaries(logs) == {CANARY}

        service.diagnostics.ready.clear()
        reply = await nats_connection.request(
            HandlerDiscovery.with_namespace(service.config, "orders_validation.rpc.place"),
            json.dumps({"order": {"units": 7, "authorization": CANARY}}).encode(),
            timeout=3,
        )
        assert json.loads(reply.data)["result"] == 7
        assert service.accepted == [7]
    finally:
        await service.stop()
        logger.remove(sink)
