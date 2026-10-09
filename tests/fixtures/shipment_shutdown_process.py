"""A local shipment host reports unfinished packing and closes its event loop."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cliffracer import ServiceConfig
from cliffracer.core.loop_host import run
from cliffracer.introspect import describe
from cliffracer.runners import LocalSupervisor, SupervisorLimits
from tests.fixtures.shipment_templates import Shipments, shipment_template


class Warehouse(Shipments):
    async def on_startup(self):
        self.container.lifecycle.spawn_supervised_task(self.pack(), name="unfinished-packing")

    async def pack(self):
        print("PACKING STARTED", flush=True)
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                print("PACKING STILL RUNNING", flush=True)


async def main():
    host = LocalSupervisor(
        ServiceConfig(name="shipping_host", health_listener=False),
        limits=SupervisorLimits(cleanup_timeout=0.02),
    )
    host.register(shipment_template(service_class=Warehouse, factory=Warehouse))
    await host.start()
    owner = await host.open_owner("retail")
    await host.ensure(
        owner,
        "shipments",
        "batch-a",
        {"warehouse": "north", "destinations": ["retail"]},
        revision="warehouse-a",
    )
    report = await host.close()
    assert not report.complete
    assert host.unfinished_tasks
    print("UNFINISHED SHIPMENT REPORTED", flush=True)


def execute():
    broker = AsyncMock()
    broker.is_closed = broker.is_draining = broker.is_connecting = broker.is_reconnecting = False
    broker.is_connected = True
    broker.request.return_value = SimpleNamespace(
        data=json.dumps(describe(Warehouse).to_dict()).encode()
    )
    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=broker)):
        run(main(), teardown_timeout=60)
    print("HOST EXITED", flush=True)


if __name__ == "__main__":
    execute()
