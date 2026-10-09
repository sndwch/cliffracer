"""Integration tests verifying running services respond with Description on {service}.describe."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.introspect import canonical, describe

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


class Item(BaseModel):
    sku: str


class Shop(CliffracerService):
    @rpc
    async def add(self, item: Item) -> Item:
        return item


async def test_the_live_description_equals_the_class_description(nats_connection):
    svc = Shop(ServiceConfig(name="shop_describe", version="2.0.0"))
    await svc.start()
    try:
        reply = await nats_connection.request(
            HandlerDiscovery.with_namespace(svc.config, "shop_describe.describe"),
            b"",
            timeout=2,
        )
        expected = canonical(describe(Shop, service="shop_describe", version="2.0.0").to_dict())
        assert reply.data.decode() == expected

        # The equality above holds whatever `describe` produces, so it only shows the
        # service answers with describe()'s own output. What the description says is
        # asserted here, from facts this test owns.
        body = json.loads(reply.data)
        assert (body["service"], body["version"]) == ("shop_describe", "2.0.0")
        (method,) = body["methods"]
        assert method["name"] == "add"
        (param,) = method["params"]
        assert param["name"] == "item"
        assert param["type"]["kind"] == "model"
        assert param["type"]["module"] == Item.__module__
        assert param["type"]["qualname"] == "Item"
        assert method["returns"] == param["type"]
        schema = body["components"][param["type"]["schema_hash"]]
        assert schema["properties"] == {"sku": {"title": "Sku", "type": "string"}}
        assert schema["required"] == ["sku"]
        assert body["listeners"] == []
    finally:
        await svc.stop()


async def test_a_namespaced_service_answers_under_its_namespace(nats_connection):
    svc = Shop(ServiceConfig(name="shop_ns", version="1", namespace="tenant1"))
    await svc.start()
    try:
        reply = await nats_connection.request(
            HandlerDiscovery.with_namespace(svc.config, "shop_ns.describe"), b"", timeout=2
        )
        assert json.loads(reply.data)["service"] == "shop_ns"
    finally:
        await svc.stop()
