"""The two Python blocks in `docs/local-supervisor.md` run as printed, against a live broker.

`test_docs_code_blocks_resolve.py` parses a block and resolves its imports; it does not call it. A
block that binds every name and then misuses them passes there. These execute the block's own
source, so a renamed argument, a removed parameter or a changed return value in the supervisor
fails here, in the document's own words.

The only substitution is the broker address: the host block builds its runtime from
`ServiceConfig(...)` with the default URL, and the suite's broker is not at the default.
"""

import re
from pathlib import Path

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners import LocalSupervisor
from tests.conftest import broker_url
from tests.fixtures.shipment_templates import (
    Parcel,
    shipment_client_class,
    shipment_template,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

DOC = Path(__file__).resolve().parents[2] / "docs" / "local-supervisor.md"
FENCE = re.compile(r"^```python\n(?P<source>.*?)^```$", re.S | re.M)


def blocks() -> list[str]:
    found = [m["source"] for m in FENCE.finditer(DOC.read_text())]
    assert len(found) == 2, "the document's Python blocks changed; read what this module runs"
    return found


def run(source: str) -> dict:
    namespace: dict = {}
    exec(compile(source, str(DOC), "exec"), namespace)

    def on_the_suites_broker(**fields):
        return ServiceConfig(nats_url=broker_url(), **fields)

    namespace["ServiceConfig"] = on_the_suites_broker
    return namespace


async def test_the_host_wiring_block_ships_a_batch_and_closes_complete():
    namespace = run(blocks()[0])

    receipt, report = await namespace["ship_batch"](
        shipment_template(), shipment_client_class(), Parcel(sku="bolts", quantity=2)
    )

    assert (receipt.warehouse, receipt.destination, receipt.quantity) == ("north", "retail", 2)
    assert report.complete
    assert len(report.activations) == 1


async def test_the_parent_block_declares_an_owner_that_a_parent_service_starts_and_stops():
    namespace = run(blocks()[1])
    supervisor = LocalSupervisor(ServiceConfig(name="shipping_host", nats_url=broker_url()))
    supervisor.register(shipment_template())
    async with supervisor:
        orders = namespace["orders_class"](supervisor)
        assert issubclass(orders, CliffracerService)
        parent = orders(ServiceConfig(name="orders", nats_url=broker_url(), health_port=0))
        await parent.start()
        try:
            reference = await parent.children.ensure(
                "shipments",
                "batch-a",
                {"warehouse": "north", "destinations": ["retail"]},
                revision="warehouse-a",
            )
        finally:
            await parent.stop()

    assert parent.children.cleanup_report.complete
    assert [item.reference for item in parent.children.cleanup_report.activations] == [reference]
