"""Inventory bucket declarations reach the broker without losing their options."""

import json
from copy import deepcopy
from dataclasses import fields
from datetime import timedelta
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import BucketConfig, KvExtension
from nats.js.api import KeyValueConfig, Placement, RePublish, StorageType
from nats.js.client import JetStreamContext

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration", ["dictionary", "dataclass"])
@pytest.mark.parametrize("direct", [False, True])
async def test_regional_inventory_options_reach_the_native_stream_request(declaration, direct):
    requests = []
    js = JetStreamContext(AsyncMock())

    async def request(subject, payload=b"", **kwargs):
        if ".STREAM.INFO." in subject:
            raise nats.js.errors.NotFoundError(code=404)
        config = json.loads(payload)
        requests.append((subject, config))
        return {
            "config": deepcopy(config),
            "state": {
                "messages": 0,
                "bytes": 0,
                "first_seq": 0,
                "last_seq": 0,
                "consumer_count": 0,
            },
        }

    js._api_request = AsyncMock(side_effect=request)
    original_add_stream = js.add_stream
    if declaration == "dictionary":
        options = {
            "name": "inventory",
            "storage": "memory",
            "placement": {"cluster": "central", "tags": ["retail"]},
            "republish": {"src": ">", "dest": "inventory.audit.>", "headers_only": False},
            "direct": direct,
        }
    else:
        options = BucketConfig(
            name="inventory",
            storage=StorageType.MEMORY,
            placement=Placement(cluster="central", tags=["retail"]),
            republish=RePublish(src=">", dest="inventory.audit.>", headers_only=False),
            direct=direct,
        )
    ext = KvExtension(buckets=[options], bucket_ttls={"inventory": timedelta(minutes=5)}, js=js)
    config = ServiceConfig(name="stock", subject_prefix="retail")
    await ext.setup(ExtensionSetupContext(config, config.nats_url, None))
    await ext.start()
    subject, stream = requests.pop()
    assert requests == []
    assert subject.endswith(".STREAM.CREATE.KV_retail_inventory")
    assert stream["subjects"] == ["$KV.retail_inventory.>"]
    assert stream["placement"] == {"cluster": "central", "tags": ["retail"]}
    assert stream["republish"] == {"src": ">", "dest": "inventory.audit.>", "headers_only": False}
    assert stream["allow_direct"] is direct
    assert stream["storage"] == "memory"
    assert stream["max_age"] == 300_000_000_000
    handle = await ext.get_bucket("inventory")
    assert handle._direct is direct
    assert js.add_stream == original_add_stream
    # Provisioning a different native bucket must not inherit inventory placement.
    await js.create_key_value(bucket="customers")
    assert "placement" not in requests[0][1]
    if declaration == "dataclass":
        assert options.ttl is None


@pytest.mark.asyncio
async def test_existing_inventory_is_opened_without_reconfiguring_it():
    js = AsyncMock()
    ext = KvExtension(
        buckets=[BucketConfig(name="inventory", placement=Placement(cluster="central"))], js=js
    )
    await ext.start()
    js.create_key_value.assert_not_awaited()
    js.update_stream.assert_not_awaited()


def test_bucket_options_cover_the_native_configuration_fields():
    native = {field.name for field in fields(KeyValueConfig)} - {"bucket"}
    exposed = {field.name for field in fields(BucketConfig)} - {"name"}
    assert native <= exposed, f"Native bucket options have no declaration: {native - exposed}"
