"""The description a running service answers is the one `describe` computes given its config.

The live answer is built with the service's `ServiceConfig`, which is where the declared streams
and each listener's effective subject come from. An offline `describe(cls, service=...,
version=...)` given no config leaves those two out; given the config it is the same bytes. These
ask a service's describe handler and compare its bytes with what the offline call gives with and
without the config.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, validated_listener
from cliffracer.core.jetstream import StreamSpec
from cliffracer.introspect import canonical, describe
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit


class Placed(BaseModel):
    order_id: str


class Orders(CliffracerService):
    @rpc
    async def place(self, sku: str, quantity: int = 1) -> dict[str, int]:
        """Place an order."""
        return {"quantity": quantity}

    @validated_listener("orders.placed", Placed, durable="placed_worker")
    async def on_placed(self, event: Placed) -> None:
        """A placed order."""

    @listener("audit.all", fanout=True, cross_namespace=True)
    async def on_audit(self, subject: str) -> None: ...


async def _live_answer(svc: CliffracerService) -> bytes:
    msg = MockMessage(f"{svc.config.name}.describe")
    await svc.container._handle_describe_request(msg)
    assert msg.responded_data is not None
    return msg.responded_data


def _service() -> CliffracerService:
    return Orders(
        ServiceConfig(
            name="orders",
            health_port=0,
            namespace="shop",
            version="3.1.0",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="ORDERS", subjects=["shop.orders.placed"])],
        )
    )


@pytest.mark.asyncio
async def test_the_live_answer_is_the_offline_answer_given_the_config() -> None:
    svc = _service()

    live = await _live_answer(svc)
    offline = describe(Orders, service="orders", version="3.1.0", config=svc.config)

    assert live == canonical(offline.to_dict()).encode()


@pytest.mark.asyncio
async def test_the_offline_answer_without_the_config_differs_in_the_streams_and_the_subjects() -> (
    None
):
    """The statement the docs now make, and nothing wider: what the config alone supplies."""
    svc = _service()
    live = json.loads(await _live_answer(svc))
    bare = describe(Orders, service="orders", version="3.1.0").to_dict()

    assert live != bare
    assert [s["name"] for s in live["streams"]] == ["ORDERS"] and bare["streams"] == []
    assert [item["effective_subject"] for item in live["listeners"]] == [
        "*.audit.all",
        "shop.orders.placed",
    ]
    assert [item["effective_subject"] for item in bare["listeners"]] == [None, None]

    for item in (*live["listeners"], *bare["listeners"]):
        item.pop("effective_subject")
    live["streams"] = bare["streams"] = []
    assert live == bare
