"""Pre-generated shipment clients address independently constructed live workers."""

import json
import uuid

import pytest

from cliffracer import ServiceConfig
from cliffracer.introspect import Description
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import (
    ActivationAddress,
    ActivationReference,
    LogicalIdentity,
    TemplateError,
)
from tests.fixtures.shipment_templates import (
    Parcel,
    Shipments,
    ShipmentSettings,
    shipment_client_class,
    shipment_template,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


def reference(registered, config, key):
    return ActivationReference(
        LogicalIdentity("retail", "shipments", key),
        "host",
        1,
        "warehouse-a",
        registered.contract,
        ActivationAddress(config.name, config.namespace, config.subject_prefix),
    )


async def test_one_generated_contract_serves_two_warehouses_at_pinned_addresses(
    nats_connection, monkeypatch
):
    client_class = shipment_client_class()
    registered = TemplateCatalog().register(shipment_template())
    suffix = uuid.uuid4().hex
    north_config = ServiceConfig(name="north_" + suffix, subject_prefix=None, health_port=0)
    south_config = ServiceConfig(
        name="south_" + suffix,
        namespace="wholesale",
        subject_prefix="freight_" + suffix,
        health_port=0,
    )
    north = registered.construct(
        registered.normalize(ShipmentSettings(warehouse="north", destinations=["retail"])),
        north_config,
    )
    south = registered.construct(
        registered.normalize(ShipmentSettings(warehouse="south", destinations=["wholesale"])),
        south_config,
    )
    north_client = reference(registered, north_config, "batch-a").bind(
        client_class, nc=nats_connection
    )
    south_client = reference(registered, south_config, "batch-b").bind(
        client_class, nc=nats_connection
    )
    try:
        await north.start()
        await south.start()
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "unrelated_" + suffix)
        first = await north_client.ship(Parcel(sku="bolts", quantity=2))
        second = await south_client.ship(Parcel(sku="bolts", quantity=5))
        third = await north_client.ship(Parcel(sku="nuts", quantity=3))
        assert (first.warehouse, first.destination, first.quantity) == ("north", "retail", 2)
        assert (second.warehouse, second.destination, second.quantity) == ("south", "wholesale", 5)
        assert third.quantity == 5
        assert north.health_listener.port != south.health_listener.port
        for client in (north_client, south_client):
            reply = await nats_connection.request(client._subject("describe"), b"", timeout=2)
            registered.contract.verify(Description.from_dict(json.loads(reply.data)))
    finally:
        await north_client.close()
        await south_client.close()
        await north.stop()
        await south.stop()
    assert north.stops == south.stops == 1


@pytest.mark.parametrize("owned", [False, True])
async def test_rejecting_a_live_factory_result_preserves_its_existing_shipments(
    nats_connection, owned
):
    registered = TemplateCatalog().register(shipment_template())
    settings = ShipmentSettings(warehouse="north", destinations=["retail"])
    config = ServiceConfig(name="shipment_" + uuid.uuid4().hex, health_port=0)
    service = (
        registered.construct(registered.normalize(settings), config)
        if owned
        else Shipments(settings, config)
    )
    client = reference(registered, config, "batch-a").bind(
        shipment_client_class(), nc=nats_connection
    )
    try:
        await service.start()
        assert (await client.ship(Parcel(sku="bolts", quantity=2))).quantity == 2
        other = TemplateCatalog().register(
            shipment_template(factory=lambda settings, assigned: service)
        )
        with pytest.raises(TemplateError, match="already used or owned"):
            other.construct(other.normalize(settings), config)
        assert service.stops == 0
        assert (await client.ship(Parcel(sku="bolts", quantity=3))).quantity == 5
    finally:
        await client.close()
        await service.stop()
