"""Order handlers used to exercise broker-enforced service roles."""

import asyncio

from pydantic import BaseModel

from cliffracer import CliffracerService, listener, rpc, validated_listener


class Order(BaseModel):
    quantity: int


class Orders(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        self.quantities = []
        self.arrived = asyncio.Event()

    @rpc
    async def reserve(self, quantity: int) -> int:
        self.quantities.append(quantity)
        self.arrived.set()
        return sum(self.quantities)

    @listener("orders.created", fanout=True, cross_namespace=True)
    async def created(self, quantity: int) -> None:
        self.quantities.append(quantity)
        self.arrived.set()

    @validated_listener("orders.returned", Order, fanout=True, cross_namespace=True)
    async def returned(self, message: Order) -> None:
        self.quantities.append(-message.quantity)
        self.arrived.set()


class Shipments(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        self.shipped = []
        self.arrived = asyncio.Event()

    @listener("shipments.packed", durable="packing")
    async def packed(self, quantity: int) -> None:
        self.shipped.append(quantity)
        self.arrived.set()

    @listener("shipments.sent", durable="dispatching", pull=True)
    async def sent(self, quantity: int) -> None:
        if quantity < 0:
            raise ValueError("a shipment quantity must be positive")
        self.shipped.append(quantity)
        self.arrived.set()


class ShipmentLedger(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        self.receipts = []
        self.arrived = asyncio.Event()

    @listener(">", durable="receipts", pull=True)
    async def received(self, quantity: int) -> None:
        self.receipts.append(quantity)
        self.arrived.set()
