#!/usr/bin/env python3
"""
Validated event listener example.

A publisher sends order events; the consumer validates them against a pydantic
schema. Valid orders are processed; invalid ones are dead-lettered to dlq.order_consumer.

Run (requires NATS at nats://localhost:4222):
    python examples/validation/validated_listener_example.py
"""

import asyncio

from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener


class OrderCreated(BaseModel):
    order_id: str
    amount: float


class OrderConsumer(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="order_consumer"))
        self.processed: list[OrderCreated] = []

    @validated_listener("orders.created", OrderCreated, fanout=True)
    async def on_order(self, message: OrderCreated):
        self.processed.append(message)
        self.logger.info(f"processed order {message.order_id} (${message.amount})")


async def main():
    consumer = OrderConsumer()
    await consumer.start()

    # capture dead-letters. The callback must be a COROUTINE: nats-py refuses a
    # plain function with "nats: must use coroutine for subscriptions".
    dlq = []

    async def collect(msg):
        dlq.append(msg.data.decode())

    await consumer.nc.subscribe("dlq.order_consumer", cb=collect)
    await asyncio.sleep(0.1)

    await consumer.publish_event("orders.created", order_id="A1", amount=42.0)  # valid
    await consumer.publish_event("orders.created", order_id="B2")  # invalid: no amount
    await asyncio.sleep(0.3)

    print(f"processed: {[o.order_id for o in consumer.processed]}")
    print(f"dead-lettered: {len(dlq)} message(s)")
    await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
