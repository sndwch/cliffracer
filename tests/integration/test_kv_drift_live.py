"""An existing bucket that differs from its declaration is reported, on a real broker.

The unit tests build the stream configuration the extension reads; this one reads what the
broker itself returns for a bucket that was created with other settings.
"""

import uuid

import nats
import pytest
from cliffracer_kv import BucketConfig, KvExtension, ObjectStoreConfig
from loguru import logger

from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def _start_capturing(ext: KvExtension) -> list[str]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        await ext.start()
    finally:
        logger.remove(sink)
    return lines


@pytest.mark.asyncio
async def test_a_bucket_the_broker_holds_with_other_settings_is_named_and_left_alone():
    name = f"drift_{uuid.uuid4().hex[:10]}"
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        await js.create_key_value(bucket=name)  # no ttl, history 1, file storage

        declared = BucketConfig(name=name, ttl=300, history=10, storage="memory")
        lines = await _start_capturing(KvExtension(buckets=[declared], js=js))

        said = [line for line in lines if f"Bucket '{name}' already exists" in line]
        assert len(said) == 1, lines
        for fragment in (
            "ttl: declared 300.0, on the broker 0.0",
            "history: declared 10, on the broker 1",
            "storage: declared 'memory', on the broker 'file'",
        ):
            assert fragment in said[0], said[0]

        # The bucket keeps what it had: nothing was applied.
        status = await js.stream_info(f"KV_{name}")
        assert (status.config.max_age, status.config.max_msgs_per_subject) == (0, 1)
        assert status.config.storage == "file"

        # And a declaration that agrees with it says nothing.
        agreeing = BucketConfig(name=name, history=1)
        assert await _start_capturing(KvExtension(buckets=[agreeing], js=js)) == []
    finally:
        await js.delete_key_value(name)
        await nc.close()


@pytest.mark.asyncio
async def test_an_object_store_the_broker_holds_with_other_settings_is_named():
    name = f"drift_{uuid.uuid4().hex[:10]}"
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        await js.create_object_store(bucket=name)

        declared = ObjectStoreConfig(name=name, ttl=300, storage="memory")
        lines = await _start_capturing(KvExtension(object_stores=[declared], js=js))

        said = [line for line in lines if f"Object store '{name}' already exists" in line]
        assert len(said) == 1, lines
        assert "ttl: declared 300.0, on the broker 0.0" in said[0], said[0]
        assert "storage: declared 'memory', on the broker 'file'" in said[0], said[0]
    finally:
        await js.delete_object_store(name)
        await nc.close()
