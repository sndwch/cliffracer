"""Every declared bucket option is compared with the broker's, and `start()` reuses what is open.

The drift check compared eight of the eleven options: a `republish` or a `placement` declared for
a bucket that already existed was ignored with no warning, so a declared audit republish never
happened and nothing said so. And `start()` opened every declared bucket again even when the
service's own `on_startup` had already opened it through `get_bucket()`, so the drift warning came
twice and the handle the service held was replaced by another.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cliffracer_kv import BucketConfig, KvExtension
from loguru import logger
from nats.js.api import Placement, RePublish, StreamConfig, StreamInfo, StreamState
from nats.js.kv import KeyValue

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


def _drift_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if "already exists" in line]


# --- republish and placement ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "stream", "said"),
    [
        pytest.param(
            {"republish": RePublish(src=">", dest="audit.>")},
            {},
            "republish: declared {'src': '>', 'dest': 'audit.>', 'headers_only': False}, "
            "on the broker None",
            id="republish-missing",
        ),
        pytest.param(
            {"republish": RePublish(src=">", dest="audit.>")},
            {"republish": RePublish(src=">", dest="other.>")},
            "republish: declared {'src': '>', 'dest': 'audit.>', 'headers_only': False}, "
            "on the broker {'src': '>', 'dest': 'other.>', 'headers_only': False}",
            id="republish-differs",
        ),
        pytest.param(
            {"placement": Placement(cluster="east", tags=["a"])},
            {},
            "placement: declared {'cluster': 'east', 'tags': ['a']}, on the broker None",
            id="placement-missing",
        ),
        pytest.param(
            {"placement": Placement(cluster="east", tags=["a"])},
            {"placement": Placement(cluster="west", tags=["a"])},
            "placement: declared {'cluster': 'east', 'tags': ['a']}, "
            "on the broker {'cluster': 'west', 'tags': ['a']}",
            id="placement-differs",
        ),
    ],
)
async def test_a_republish_or_placement_that_differs_is_named(lines, declared, stream, said):
    js, _ = _js_with_bucket(_stream(**stream))

    await KvExtension(buckets=[BucketConfig(name="x", **declared)], js=js).start()

    assert any(said in line for line in _drift_lines(lines)), lines


@pytest.mark.asyncio
async def test_a_declaration_of_only_a_republish_is_compared_and_costs_a_read(lines):
    js, kv = _js_with_bucket(_stream())

    await KvExtension(
        buckets=[BucketConfig(name="x", republish=RePublish(src=">", dest="audit.>"))], js=js
    ).start()

    kv.status.assert_awaited()
    assert _drift_lines(lines), lines


@pytest.mark.asyncio
async def test_CONTROL_a_republish_and_placement_that_match_say_nothing(lines):
    stream = {
        "republish": RePublish(src=">", dest="audit.>", headers_only=True),
        "placement": Placement(cluster="east", tags=["b", "a"]),
    }
    js, _ = _js_with_bucket(_stream(**stream))
    config = BucketConfig(
        name="x",
        republish=RePublish(src=">", dest="audit.>", headers_only=True),
        placement=Placement(cluster="east", tags=["a", "b"]),
    )

    await KvExtension(buckets=[config], js=js).start()

    assert _drift_lines(lines) == []


@pytest.mark.asyncio
async def test_CONTROL_a_bucket_declared_with_neither_is_not_compared_on_them(lines):
    js, _ = _js_with_bucket(
        _stream(republish=RePublish(src=">", dest="audit.>"), placement=Placement(cluster="east"))
    )

    await KvExtension(buckets=[BucketConfig(name="x", ttl=300, history=1)], js=js).start()

    assert [
        line for line in _drift_lines(lines) if "republish" in line or "placement" in line
    ] == []


# --- start() reuses what is open -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bucket_opened_before_start_is_not_opened_or_reported_again(lines):
    js, kv = _js_with_bucket(_stream(max_age=3600.0))
    extension = KvExtension(buckets=[BucketConfig(name="x", ttl=60)], js=js)
    extension._declare()

    first = await extension.get_bucket("x")
    assert len(_drift_lines(lines)) == 1, lines
    await extension.start()
    second = await extension.get_bucket("x")

    assert len(_drift_lines(lines)) == 1, lines
    assert second is first
    assert js.key_value.await_count == 1


@pytest.mark.asyncio
async def test_a_store_opened_before_start_is_not_opened_again():
    store = AsyncMock()
    js = AsyncMock()
    js.object_store.return_value = store
    extension = KvExtension(object_stores=["media"], js=js)
    extension._declare()

    first = await extension.get_object_store("media")
    await extension.start()

    assert await extension.get_object_store("media") is first
    assert js.object_store.await_count == 1


@pytest.mark.asyncio
async def test_CONTROL_start_still_opens_every_declared_bucket_that_is_not_open():
    js, _ = _js_with_bucket(_stream())
    extension = KvExtension(buckets=["x", "y"], js=js)

    await extension.start()

    assert sorted(extension._kv_stores) == ["x", "y"]
    assert js.key_value.await_count == 2
