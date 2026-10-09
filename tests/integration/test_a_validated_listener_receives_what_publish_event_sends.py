"""A schema-validated listener receives its schema live, from either form of the event.

Runs against a throwaway local broker only. Never point these at the shared fleet broker.

`publish_event(topic, message=Schema(...))` sends the schema under the parameter's name and
`publish_event(topic, name="a", qty=2)` sends its fields. A `@validated_listener` gets the same schema
from both, on a core subscription and on a JetStream push consumer (the decorator takes no `pull`),
and a schema whose fields all have defaults gets the published values and not an empty model. A
schema that reads the parameter's name as a field's alias is read flat, as the publisher meant it.
"""

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, validated_listener

pytestmark = pytest.mark.integration


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    qty: int = 1


class Lenient(BaseModel):
    name: str = "default"
    qty: int = 1


class Aliased(BaseModel):
    thing: dict = Field(alias="message")


class Consumer(CliffracerService):
    received: list

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.received = []

    @validated_listener("itest.vl.core_strict", Strict, fanout=True)
    async def core_strict(self, message: Strict) -> None:
        self.received.append(("core_strict", message))

    @validated_listener("itest.vl.core_lenient", Lenient, fanout=True)
    async def core_lenient(self, message: Lenient) -> None:
        self.received.append(("core_lenient", message))

    @validated_listener("itest.vl.push_strict", Strict, durable="itest_vl_push_strict")
    async def push_strict(self, message: Strict) -> None:
        self.received.append(("push_strict", message))

    @validated_listener("itest.vl.push_lenient", Lenient, durable="itest_vl_push_lenient")
    async def push_lenient(self, message: Lenient) -> None:
        self.received.append(("push_lenient", message))

    @validated_listener("itest.vl.core_aliased", Aliased, fanout=True)
    async def core_aliased(self, message: Aliased) -> None:
        self.received.append(("core_aliased", message))

    @validated_listener("itest.vl.push_aliased", Aliased, durable="itest_vl_push_aliased")
    async def push_aliased(self, message: Aliased) -> None:
        self.received.append(("push_aliased", message))


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_both_forms_deliver_the_same_schema_to_a_validated_listener():
    service = Consumer(
        ServiceConfig(
            name="itest_validated",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="ITEST_VL", subjects=["itest.vl.*"]),
                StreamSpec(name="ITEST_VL_DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    await service.start()
    try:
        for kind in ("core", "push"):
            subject = f"itest.vl.{kind}_strict"
            await service.publish_event(subject, message=Strict(name="a", qty=2))
            await service.publish_event(subject, name="a", qty=2)
            await service.publish_event(
                f"itest.vl.{kind}_lenient", message=Lenient(name="b", qty=3)
            )
            await service.publish_event(f"itest.vl.{kind}_aliased", message={"a": 1})

        async with asyncio.timeout(15):
            while len(service.received) < 8:
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)

        assert sorted(map(repr, service.received)) == sorted(
            map(
                repr,
                [
                    (f"{kind}_{shape}", model)
                    for kind in ("core", "push")
                    for shape, model in (
                        ("strict", Strict(name="a", qty=2)),
                        ("strict", Strict(name="a", qty=2)),
                        ("lenient", Lenient(name="b", qty=3)),
                        ("aliased", Aliased(message={"a": 1})),
                    )
                ],
            )
        ), service.received
    finally:
        await service.stop()
