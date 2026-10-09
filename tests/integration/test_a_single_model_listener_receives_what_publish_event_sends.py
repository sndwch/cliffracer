"""A listener whose one parameter is a model receives it live, from either form of the event.

Runs against a throwaway local broker only. Never point these at the shared fleet broker.

`publish_event(topic, item=Item(...))` sends the model under its name and `publish_event(topic,
name="a", qty=2)` sends its fields; the listener `on(self, item: Item)` gets the same `Item` from
both, and a model whose fields all have defaults gets the published values and not an empty model.
"""

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from cliffracer import CliffracerService, ServiceConfig, listener

pytestmark = pytest.mark.integration


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    qty: int = 1


class Lenient(BaseModel):
    name: str = "default"
    qty: int = 1


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_both_forms_of_an_event_deliver_the_same_model_to_a_single_model_listener():
    received: list[BaseModel] = []

    class Consumer(CliffracerService):
        @listener("itest.items.created", fanout=True)
        async def on_item(self, item: Item) -> None:
            received.append(item)

        @listener("itest.lenient.created", fanout=True)
        async def on_lenient(self, item: Lenient) -> None:
            received.append(item)

    service = Consumer(ServiceConfig(name="itest_single_model"))
    await service.start()
    try:
        await service.publish_event("itest.items.created", item=Item(name="a", qty=2))
        await service.publish_event("itest.items.created", name="a", qty=2)
        await service.publish_event("itest.lenient.created", item=Lenient(name="b", qty=3))

        async with asyncio.timeout(10):
            while len(received) < 3:
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)

        assert sorted(map(repr, received)) == sorted(
            map(
                repr,
                [Item(name="a", qty=2), Item(name="a", qty=2), Lenient(name="b", qty=3)],
            )
        ), received
    finally:
        await service.stop()
