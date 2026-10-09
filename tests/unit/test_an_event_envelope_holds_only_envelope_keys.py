"""An event envelope's top level holds the envelope's keys and nothing else.

The payload travels under `data`. `publish_event` used to repeat every payload
key at the top level too, where a key sharing an envelope name was silently
replaced by the envelope's value; `broadcast_message` never did. Both
publishers are asserted, with a payload that has an ordinary field and one
named like an envelope key, so the collision cannot come back unseen.
"""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit

ENVELOPE_KEYS = {"data", "source_service", "timestamp", "correlation_id"}


@pytest.mark.parametrize("publisher", ["publish_event", "broadcast_message"])
@pytest.mark.asyncio
async def test_the_top_level_is_the_envelope_and_data_is_the_payload(publisher):
    service = CliffracerService(ServiceConfig(name="orders"))
    service.nc = AsyncMock()

    await getattr(service, publisher)("orders.created", order_id="o1", timestamp="from-payload")

    wire = json.loads(service.nc.publish.await_args.args[1])
    assert set(wire) == ENVELOPE_KEYS, wire
    assert wire["data"] == {"order_id": "o1", "timestamp": "from-payload"}, wire
    assert wire["source_service"] == "orders", wire
