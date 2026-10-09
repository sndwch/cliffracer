"""Two order parents request typed shipment children after serving begins."""

import asyncio
import json
import os
import uuid

from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.extension import SharedDependency
from cliffracer.core.loop_host import run
from cliffracer.runners import LocalSupervisor, ServiceOwner, SupervisorLimits
from cliffracer.runners.contracts import (
    ActivationCapacityError,
    ActivationConflict,
    LogicalIdentity,
)
from examples.virtual_services.shipment_client import ShipmentsClient
from examples.virtual_services.shipments import Parcel, template


class DispatchResult(BaseModel):
    status: str
    warehouse: str | None = None
    total: int | None = None
    detail: str | None = None


def order_service(supervisor: LocalSupervisor):
    class Orders(CliffracerService):
        children = ServiceOwner(SharedDependency(supervisor), scope="retail")

        @rpc
        async def dispatch(
            self, batch: str, warehouse: str, quantity: int
        ) -> DispatchResult:
            try:
                reference = await self.children.ensure(
                    "shipments",
                    batch,
                    {
                        "warehouse": warehouse,
                        "batch": batch,
                    },
                    revision="warehouse-a",
                )
            except ActivationCapacityError:
                return DispatchResult(
                    status="capacity",
                    detail="Finish an active batch before requesting another.",
                )
            except ActivationConflict:
                return DispatchResult(
                    status="conflict",
                    detail="This batch belongs to different settings or another parent.",
                )
            client = reference.bind(ShipmentsClient, nc=self.nc)
            receipt = await client.ship(Parcel(sku="bolts", quantity=quantity))
            return DispatchResult(
                status="shipped", warehouse=receipt.warehouse, total=receipt.total
            )

    return Orders


async def demonstrate(nats_url: str | None = None) -> dict:
    suffix = uuid.uuid4().hex[:10]
    runtime = ServiceConfig(
        name="shipping_host", namespace="retail_" + suffix, health_port=0
    )
    if nats_url is not None:
        runtime.nats_url = nats_url
    supervisor = LocalSupervisor(
        runtime,
        limits=SupervisorLimits(
            max_active=2,
            max_records=8,
            max_owners=4,
            startup_timeout=5,
            cleanup_timeout=2,
            wait_timeout=8,
        ),
    )
    supervisor.register(template())
    Orders = order_service(supervisor)
    north = Orders(runtime.model_copy(update={"name": "orders_north_" + suffix}))
    south = Orders(runtime.model_copy(update={"name": "orders_south_" + suffix}))
    caller = CliffracerService(
        runtime.model_copy(update={"name": "orders_caller_" + suffix})
    )
    progress = []
    four_updates = asyncio.Event()

    async def observe(message):
        progress.append(json.loads(message.data))
        if len(progress) >= 4:
            four_updates.set()

    async def dispatch(parent, batch, warehouse, quantity):
        return await caller.call_rpc(
            parent.config.name,
            "dispatch",
            batch=batch,
            warehouse=warehouse,
            quantity=quantity,
        )

    async with supervisor:
        try:
            await north.start()
            await south.start()
            await caller.start()
            subject = HandlerDiscovery.with_namespace(runtime, "shipments.progress.>")
            observer = await caller.nc.subscribe(subject, cb=observe)
            await caller.nc.flush()
            first = await dispatch(north, "batch-a", "north", 2)
            repeat = await dispatch(north, "batch-a", "north", 3)
            other = await dispatch(south, "batch-b", "south", 4)
            full = await dispatch(north, "batch-c", "north", 1)
            conflict = await dispatch(north, "batch-a", "changed", 1)
            await north.stop()
            north_snapshot = await supervisor.inspect(
                LogicalIdentity("retail", "shipments", "batch-a")
            )
            surviving = await dispatch(south, "batch-b", "south", 1)
            await asyncio.wait_for(four_updates.wait(), timeout=2)
            await south.stop()
            await observer.unsubscribe()
            return {
                "north_totals": [first["total"], repeat["total"]],
                "south_totals": [other["total"], surviving["total"]],
                "capacity": full["status"],
                "conflict": conflict["status"],
                "north_state": north_snapshot.state.value,
                "north_closed": north.children.cleanup_report.complete,
                "south_closed": south.children.cleanup_report.complete,
                "progress_updates": len(progress),
            }
        finally:
            await north.stop()
            await south.stop()
            await caller.stop()


async def main():
    result = await demonstrate(os.environ.get("CLIFFRACER_NATS_URL"))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    run(main())
