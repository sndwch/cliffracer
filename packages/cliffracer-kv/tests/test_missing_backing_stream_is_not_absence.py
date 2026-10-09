"""Lost storage is distinct from an absent inventory record or receipt."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import KvExtension

pytestmark = pytest.mark.unit


def missing_stream() -> nats.js.errors.NotFoundError:
    return nats.js.errors.NotFoundError(code=404, err_code=10059, description="stream not found")


async def inventory_extension() -> tuple[KvExtension, AsyncMock, AsyncMock]:
    js = AsyncMock()
    inventory = AsyncMock()
    inventory._js = js
    inventory._stream = "KV_inventory"
    inventory._pre = "$KV.inventory."
    inventory._direct = False
    js.key_value.return_value = inventory
    extension = KvExtension(buckets=["inventory"], js=js)
    await extension.start()
    return extension, inventory, js


async def receipt_extension() -> tuple[KvExtension, AsyncMock, AsyncMock]:
    js = AsyncMock()
    receipts = AsyncMock()
    receipts._js = js
    receipts._stream = "OBJ_receipts"
    js.object_store.return_value = receipts
    extension = KvExtension(object_stores=["receipts"], js=js)
    await extension.start()
    return extension, receipts, js


async def test_a_missing_item_is_still_an_ordinary_inventory_miss():
    extension, inventory, js = await inventory_extension()
    inventory.get.side_effect = nats.js.errors.KeyNotFoundError()
    js.stream_info.return_value = SimpleNamespace()

    assert await extension.get("inventory", "sku-123", default="unlisted") == "unlisted"


async def test_a_lost_inventory_stream_is_not_reported_as_an_absent_item():
    extension, inventory, js = await inventory_extension()
    inventory.get.side_effect = nats.js.errors.KeyNotFoundError()
    js.stream_info.side_effect = missing_stream()

    with pytest.raises(nats.js.errors.NotFoundError, match="stream not found"):
        await extension.get("inventory", "sku-123", default="unlisted")


async def test_a_lost_inventory_stream_is_not_reported_as_an_empty_catalog():
    extension, inventory, js = await inventory_extension()
    watcher = AsyncMock()
    watcher.updates.return_value = None
    inventory.watch.return_value = watcher
    js.stream_info.side_effect = missing_stream()

    with pytest.raises(nats.js.errors.NotFoundError, match="stream not found"):
        await extension.keys("inventory")

    watcher.stop.assert_awaited_once()


@pytest.mark.parametrize("method", ["get_object", "delete_object", "list_objects"])
async def test_a_lost_receipt_store_is_not_reported_as_an_absent_receipt(method: str):
    extension, receipts, js = await receipt_extension()
    native_method = {"get_object": "get", "delete_object": "delete"}.get(method, "list")
    failure = (
        nats.js.errors.NotFoundError()
        if method == "list_objects"
        else nats.js.errors.ObjectNotFoundError()
    )
    getattr(receipts, native_method).side_effect = failure
    js.stream_info.side_effect = missing_stream()
    arguments = () if method == "list_objects" else ("invoice-123",)

    with pytest.raises(nats.js.errors.NotFoundError, match="stream not found"):
        await getattr(extension, method)("receipts", *arguments)
