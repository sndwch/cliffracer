"""A local shipment host that must keep giving its event loop back.

`python -m tests.fixtures.supervisor_loop_process SCENARIO` prints `DONE SCENARIO` once the
scenario finishes. A supervisor that spins without yielding never finishes, and nothing inside the
frozen loop can time it out, so the test that runs this bounds it from outside the process.
"""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cliffracer import ServiceConfig
from cliffracer.introspect import describe
from cliffracer.runners import LocalSupervisor, SupervisorLimits
from cliffracer.runners.supervisor import ActivationTerminated
from tests.fixtures.shipment_templates import Shipments, shipment_template

SETTINGS = {"warehouse": "north", "destinations": ["retail"]}


class HeldStop(Shipments):
    gate: asyncio.Event | None = None
    stopping = None

    async def on_shutdown(self):
        if HeldStop.gate is not None:
            HeldStop.stopping.set()
            await HeldStop.gate.wait()
        await super().on_shutdown()


async def host():
    supervisor = LocalSupervisor(
        ServiceConfig(name="shipping_host", health_listener=False),
        limits=SupervisorLimits(startup_timeout=1, cleanup_timeout=1, wait_timeout=2),
    )
    supervisor.register(shipment_template(service_class=HeldStop, factory=HeldStop))
    await supervisor.start()
    return supervisor, await supervisor.open_owner("retail")


async def ensure(supervisor, owner):
    return await supervisor.ensure(owner, "shipments", "batch-a", SETTINGS, revision="warehouse-a")


async def monitor():
    """The monitor sleeps between passes, so the host's own work runs."""
    supervisor, owner = await host()
    await ensure(supervisor, owner)
    await asyncio.sleep(0.05)
    await supervisor.close()


async def stopping_waiter():
    """A caller waiting on a stopping activation yields to the cleanup it waits for."""
    HeldStop.gate, HeldStop.stopping = asyncio.Event(), asyncio.Event()
    supervisor, owner = await host()
    reference = await ensure(supervisor, owner)
    stopping = asyncio.create_task(supervisor.stop(reference))
    await HeldStop.stopping.wait()
    waiter = asyncio.create_task(ensure(supervisor, owner))
    await asyncio.sleep(0.02)
    HeldStop.gate.set()
    try:
        await waiter
    except ActivationTerminated:
        pass
    else:
        raise AssertionError("a stopped activation was handed back as ready")
    await stopping
    await supervisor.close()


SCENARIOS = {"monitor": monitor, "stopping-waiter": stopping_waiter}


def execute(scenario):
    broker = AsyncMock()
    broker.is_closed = broker.is_draining = broker.is_connecting = broker.is_reconnecting = False
    broker.is_connected = True
    broker.request.return_value = SimpleNamespace(
        data=json.dumps(describe(HeldStop).to_dict()).encode()
    )

    async def close():
        broker.is_closed = True
        broker.is_connected = False

    broker.close = close
    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=broker)):
        asyncio.run(SCENARIOS[scenario]())
    print(f"DONE {scenario}", flush=True)


if __name__ == "__main__":
    execute(sys.argv[1])
