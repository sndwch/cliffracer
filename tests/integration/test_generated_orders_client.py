"""A generated Orders client retains the real service's RPC contract."""

import importlib.util
import sys

import pytest

from cliffracer import ServiceConfig
from cliffracer.client import RpcValidationError
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import describe
from tests.fixtures.orders_client import OrderReceipt, OrderRequest, Orders

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def test_generated_orders_client_round_trip(nats_connection, tmp_path):
    service = Orders(
        ServiceConfig(
            name="generated_orders",
            version="1",
            nats_url=nats_connection.connected_url.geturl(),
            health_listener=False,
        )
    )
    path = tmp_path / "orders_client.py"
    path.write_text(emit(describe(Orders, service=service.config.name, version="1")))
    spec = importlib.util.spec_from_file_location("generated_orders_client", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        await service.start()
        client = module.GeneratedOrdersClient(nats_connection)
        receipt = await client.create("retail", OrderRequest(sku="widget", quantity=2))
        assert receipt == OrderReceipt(order_id="retail:widget", quantity=2)
        assert await client.find(receipt.order_id) == OrderReceipt(
            order_id="retail:widget", quantity=1
        )
        assert await client.find("missing") is None
        assert await client.list_orders() == [receipt]
        assert await client.label(str=42) == "Order 42"
        assert await client.cancel(receipt.order_id) is None
        with pytest.raises(RpcValidationError, match="refused before sending"):
            await client.create("retail", {"sku": "widget", "quantity": 0})
    finally:
        await service.stop()
        sys.modules.pop(spec.name, None)
