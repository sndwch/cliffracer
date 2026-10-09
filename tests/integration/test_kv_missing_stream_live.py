"""Cached storage handles surface backing streams deleted by an operator."""

import uuid

import nats
import nats.js.errors
import pytest
from cliffracer_kv import KvExtension

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.mark.asyncio
async def test_deleted_backing_streams_are_not_reported_as_missing_business_data():
    marker = uuid.uuid4().hex
    inventory_name = f"inventory_{marker}"
    receipts_name = f"receipts_{marker}"
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    extension = KvExtension(buckets=[inventory_name], object_stores=[receipts_name], js=js)
    config = ServiceConfig(name="warehouse")
    await extension.setup(ExtensionSetupContext(config, config.nats_url, None))
    await extension.start()
    inventory = await extension.get_bucket(inventory_name)
    receipts = await extension.get_object_store(receipts_name)
    await extension.put(inventory_name, "sku-123", {"available": 7})
    await extension.put_object(receipts_name, "invoice-123", b"paid")
    await js.delete_stream(inventory._stream)
    await js.delete_stream(receipts._stream)
    try:
        operations = (
            lambda: extension.get(inventory_name, "sku-404", default="unlisted"),
            lambda: extension.keys(inventory_name),
            lambda: extension.history(inventory_name, "sku-123"),
            lambda: extension.get_object(receipts_name, "invoice-404"),
            lambda: extension.list_objects(receipts_name),
            lambda: extension.delete_object(receipts_name, "invoice-404"),
        )
        for operation in operations:
            with pytest.raises(nats.js.errors.NotFoundError):
                await operation()
    finally:
        await extension.stop()
        await nc.close()
