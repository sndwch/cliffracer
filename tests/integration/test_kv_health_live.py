"""The KV extension's health follows a real connection and real buckets.

A deleted bucket and a closed client are what the unit tests stand in for; here the broker
and the client do them.
"""

import uuid

import nats
import pytest
from cliffracer_kv import KvExtension

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dependencies import check_dependencies, failed_dependencies
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.mark.asyncio
async def test_a_deleted_bucket_and_then_a_closed_client_fail_the_probe_and_the_flag():
    bucket = f"health_{uuid.uuid4().hex[:10]}"
    nc = await nats.connect(broker_url())
    js = nc.jetstream()

    class Shop(CliffracerService):
        kv = KvExtension(buckets=[bucket], js=js)

    service = Shop(ServiceConfig(name="shop", health_port=0))
    try:
        await service.container._setup_extensions()
        await service.kv.start()

        async def failing() -> list[str]:
            return failed_dependencies(await check_dependencies(service._dependencies))

        stream = (await service.kv.status(bucket)).stream_info.config.name
        assert service.kv.health_details()["connected"] is True
        assert await failing() == []

        await js.delete_stream(stream)
        assert await failing() == ["kv"]

        await nc.close()
        assert service.kv.health_details()["connected"] is False
        assert await failing() == ["kv"]
    finally:
        await nc.close()
