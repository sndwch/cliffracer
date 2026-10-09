"""Inventory changes reach their audit subscribers with the declared storage."""

import json
import uuid

import nats
import pytest
from cliffracer_kv import BucketConfig, KvExtension
from nats.js.api import RePublish, StorageType

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext
from cliffracer.core.jetstream import all_streams
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "storage,direct,headers_only",
    [(StorageType.MEMORY, False, False), (StorageType.FILE, True, True)],
)
async def test_inventory_changes_republish_to_audit_and_keep_the_declared_read_mode(
    storage, direct, headers_only
):
    marker = uuid.uuid4().hex
    bucket = f"inventory_{marker}"
    nc = await nats.connect(broker_url())
    observer = None
    ext = None
    stream_name = None
    try:
        observer = await nats.connect(broker_url())
        subscription = await observer.subscribe(f"audit.{marker}.>")
        await observer.flush()
        ext = KvExtension(
            buckets=[
                BucketConfig(
                    name=bucket,
                    storage=storage,
                    direct=direct,
                    republish=RePublish(
                        src=">", dest=f"audit.{marker}.>", headers_only=headers_only
                    ),
                )
            ],
            js=nc.jetstream(),
        )
        config = ServiceConfig(name="inventory")
        await ext.setup(ExtensionSetupContext(config, config.nats_url, None))
        await ext.start()
        status = await ext.status(bucket)
        stream_name = status.stream_info.config.name
        assert status.stream_info.config.storage == storage
        assert status.stream_info.config.allow_direct is direct
        revision = await ext.put(bucket, "stock.sku1", {"quantity": 9})
        audit = await subscription.next_msg(timeout=3)
        assert int(audit.headers["Nats-Sequence"]) == revision
        if headers_only:
            assert audit.data == b""
        else:
            assert json.loads(audit.data) == {"quantity": 9}
        assert await ext.get(bucket, "stock.sku1") == {"quantity": 9}
        await subscription.unsubscribe()
    finally:
        try:
            if ext is not None:
                await ext.stop()
            if stream_name is not None:
                await nc.jetstream().delete_stream(stream_name)
            if observer is not None:
                leftovers = [
                    info.config.name
                    for info in await all_streams(observer.jetstream())
                    if info.config.name.split("_")[-1] == marker
                ]
                assert leftovers == [], f"Inventory test left broker streams behind: {leftovers}"
        finally:
            if observer is not None:
                await observer.close()
            await nc.close()
