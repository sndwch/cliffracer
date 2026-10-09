"""An existing bucket keeps its configuration, so a declaration that differs is said out loud.

The stream configurations here are nats-py's own types, so the extension reads what a broker
returns; the log is read through a sink, because the warning is the whole of the behaviour.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import BucketConfig, KvExtension, ObjectStoreConfig
from loguru import logger
from nats.js.api import StorageType, StreamConfig, StreamInfo, StreamState
from nats.js.kv import KeyValue
from nats.js.object_store import ObjectStore

pytestmark = pytest.mark.unit


def _stream(**overrides) -> StreamInfo:
    config = StreamConfig(name="KV_x", **overrides)
    state = StreamState(messages=0, bytes=0, first_seq=0, last_seq=0, consumer_count=0)
    return StreamInfo(config=config, state=state)


@pytest.fixture
def lines():
    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    yield captured
    logger.remove(sink)


def _js_with_bucket(info: StreamInfo) -> tuple[AsyncMock, AsyncMock]:
    kv = AsyncMock()
    kv.status.return_value = KeyValue.BucketStatus(stream_info=info, bucket="x")
    js = AsyncMock()
    js.key_value.return_value = kv
    return js, kv


def _js_with_store(info: StreamInfo) -> tuple[AsyncMock, AsyncMock]:
    store = AsyncMock()
    store.status.return_value = ObjectStore.ObjectStoreStatus(stream_info=info, bucket="x")
    js = AsyncMock()
    js.object_store.return_value = store
    return js, store


# --- each declared option is compared against the stream ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "stream", "said"),
    [
        pytest.param({"ttl": 300}, {}, "ttl: declared 300.0, on the broker 0.0", id="ttl"),
        pytest.param(
            {"ttl": 0}, {"max_age": 60.0}, "ttl: declared 0.0, on the broker 60.0", id="ttl-0"
        ),
        pytest.param(
            {"history": 10},
            {"max_msgs_per_subject": 1},
            "history: declared 10, on the broker 1",
            id="history",
        ),
        pytest.param(
            {"max_bytes": 1024},
            {"max_bytes": -1},
            "max_bytes: declared 1024, on the broker -1",
            id="max_bytes",
        ),
        pytest.param(
            {"max_value_size": 64},
            {"max_msg_size": -1},
            "max_value_size: declared 64, on the broker -1",
            id="max_value_size",
        ),
        pytest.param(
            {"replicas": 3},
            {"num_replicas": 1},
            "replicas: declared 3, on the broker 1",
            id="replicas",
        ),
        pytest.param(
            {"storage": "memory"},
            {"storage": StorageType.FILE},
            "storage: declared 'memory', on the broker 'file'",
            id="storage",
        ),
        pytest.param(
            {"storage": StorageType.MEMORY},
            {"storage": "file"},
            "storage: declared 'memory', on the broker 'file'",
            id="storage-as-the-broker-spells-it",
        ),
        pytest.param(
            {"description": "d"},
            {"description": "other"},
            "description: declared 'd', on the broker 'other'",
            id="description",
        ),
        pytest.param(
            {"direct": True},
            {"allow_direct": False},
            "direct: declared True, on the broker False",
            id="direct",
        ),
    ],
)
async def test_each_declared_bucket_option_that_differs_is_named(lines, declared, stream, said):
    js, _ = _js_with_bucket(_stream(**stream))

    await KvExtension(buckets=[BucketConfig(name="x", **declared)], js=js).start()

    assert any("Bucket 'x' already exists" in line and said in line for line in lines), lines


@pytest.mark.asyncio
async def test_one_warning_names_every_option_that_differs_and_says_it_is_not_applied(lines):
    js, _ = _js_with_bucket(_stream(max_msgs_per_subject=1, storage=StorageType.FILE))

    await KvExtension(
        buckets=[BucketConfig(name="x", ttl=300, history=10, storage="memory")], js=js
    ).start()

    warnings = [line for line in lines if "already exists" in line]
    assert len(warnings) == 1, lines
    for said in ("ttl: declared 300.0", "history: declared 10", "storage: declared 'memory'"):
        assert said in warnings[0]
    assert "not applied" in warnings[0]


# --- what is not drift ---------------------------------------------------------


@pytest.mark.asyncio
async def test_CONTROL_a_declaration_that_matches_the_stream_says_nothing(lines):
    js, _ = _js_with_bucket(
        _stream(
            max_age=300.0,
            max_msgs_per_subject=10,
            max_bytes=1024,
            max_msg_size=64,
            num_replicas=3,
            storage=StorageType.MEMORY,
            description="d",
            allow_direct=True,
        )
    )
    config = BucketConfig(
        name="x",
        ttl=timedelta(minutes=5),
        history=10,
        max_bytes=1024,
        max_value_size=64,
        replicas=3,
        storage=StorageType.MEMORY,
        description="d",
        direct=True,
    )

    await KvExtension(buckets=[config], js=js).start()

    assert [line for line in lines if "already exists" in line] == []


@pytest.mark.asyncio
async def test_a_bucket_declared_by_name_alone_is_not_compared_and_costs_no_read(lines):
    js, kv = _js_with_bucket(_stream(max_age=300.0, max_msgs_per_subject=10))

    await KvExtension(buckets=["x"], js=js).start()

    assert lines == []
    kv.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_default_history_is_not_a_request(lines):
    """`history=1` is what a declaration holds when it asks for nothing."""
    js, _ = _js_with_bucket(_stream(max_age=300.0, max_msgs_per_subject=10))

    await KvExtension(buckets=[BucketConfig(name="x", ttl=300, history=1)], js=js).start()

    assert [line for line in lines if "already exists" in line] == []


@pytest.mark.asyncio
async def test_a_bucket_the_service_creates_is_not_compared_with_itself(lines):
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    created = AsyncMock()
    js.create_key_value.return_value = created

    await KvExtension(buckets=[BucketConfig(name="x", ttl=300)], js=js).start()

    assert lines == []
    created.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stand_in_for_the_stream_is_not_compared_as_if_it_were_one(lines):
    """A mock's attributes are mocks, and `float(MagicMock())` is 1.0: no drift can be read."""
    js = AsyncMock()
    js.key_value.return_value = AsyncMock()

    await KvExtension(
        buckets=[BucketConfig(name="x", ttl=300, history=10, replicas=3, storage="memory")], js=js
    ).start()

    assert lines == []


# --- a stream that cannot be read never stops the service starting -------------


@pytest.mark.asyncio
async def test_a_status_that_cannot_be_read_is_warned_about_and_the_bucket_still_opens(lines):
    js, kv = _js_with_bucket(_stream())
    kv.status.side_effect = RuntimeError("no answer")
    ext = KvExtension(buckets=[BucketConfig(name="x", ttl=300)], js=js)

    await ext.start()

    assert any("could not read" in line and "'x'" in line and "no answer" in line for line in lines)
    assert await ext.get_bucket("x") is kv


# --- object stores -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "stream", "said"),
    [
        pytest.param({"ttl": 300}, {}, "ttl: declared 300.0, on the broker 0.0", id="ttl"),
        pytest.param(
            {"max_bytes": 1024}, {"max_bytes": -1}, "max_bytes: declared 1024", id="max_bytes"
        ),
        pytest.param({"replicas": 3}, {"num_replicas": 1}, "replicas: declared 3", id="replicas"),
        pytest.param(
            {"storage": "memory"},
            {"storage": StorageType.FILE},
            "storage: declared 'memory'",
            id="storage",
        ),
        pytest.param({"description": "d"}, {}, "description: declared 'd'", id="description"),
    ],
)
async def test_each_declared_object_store_option_that_differs_is_named(
    lines, declared, stream, said
):
    js, _ = _js_with_store(_stream(**stream))

    await KvExtension(object_stores=[ObjectStoreConfig(name="x", **declared)], js=js).start()

    assert any("Object store 'x' already exists" in line and said in line for line in lines), lines


@pytest.mark.asyncio
async def test_CONTROL_an_object_store_that_matches_its_declaration_says_nothing(lines):
    js, _ = _js_with_store(
        _stream(max_age=300.0, max_bytes=1024, num_replicas=3, storage=StorageType.MEMORY)
    )
    config = ObjectStoreConfig(
        name="x", ttl=300, max_bytes=1024, replicas=3, storage=StorageType.MEMORY
    )

    await KvExtension(object_stores=[config], js=js).start()

    assert [line for line in lines if "already exists" in line] == []
