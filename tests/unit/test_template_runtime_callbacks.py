"""Host callbacks retain their original targets while runtime data is isolated."""

import asyncio

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.jetstream import StreamSpec
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import Shipments, shipment_template

pytestmark = pytest.mark.unit


class WarehouseStatus:
    def __init__(self):
        self.connected = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.errors = []

    def error(self, exception):
        self.errors.append(exception)


async def test_factory_runtime_preserves_bound_callbacks_and_isolates_mutable_fields():
    status = WarehouseStatus()
    runtime = ServiceConfig(
        name="warehouse",
        health_port=0,
        on_connect=status.connected.set,
        on_disconnect=status.disconnected.set,
        on_error=status.error,
        jetstream_streams=[StreamSpec(name="orders", subjects=["orders.>"])],
    )
    registered = TemplateCatalog().register(shipment_template())
    child = registered.construct(
        registered.normalize({"warehouse": "north", "destinations": ["retail"]}), runtime
    )
    child.config.on_connect()
    child.config.on_disconnect()
    error = RuntimeError("warehouse unavailable")
    child.config.on_error(error)
    assert status.connected.is_set()
    assert status.disconnected.is_set()
    assert status.errors == [error]
    child.config.jetstream_streams[0].subjects.append("shipments.>")
    assert runtime.jetstream_streams[0].subjects == ["orders.>"]


def test_factory_cannot_replace_host_callback():
    connected = asyncio.Event()

    def factory(settings, runtime):
        runtime.on_connect = lambda: None
        return Shipments(settings, runtime)

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    with pytest.raises(TemplateError, match="host-assigned"):
        registered.construct(
            registered.normalize({"warehouse": "north", "destinations": ["retail"]}),
            ServiceConfig(name="warehouse", health_port=0, on_connect=connected.set),
        )
