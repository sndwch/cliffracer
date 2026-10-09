"""Callers that open one unopened bucket at the same moment provision it once and share a handle.

`get_bucket` and `get_object_store` checked the cache, then awaited the broker with no lock, so N
concurrent first callers all missed the cache, all called `create_*`, and each kept a handle of
its own. With a declaration that differs between two callers the loser got the broker's refusal.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import KvExtension

pytestmark = pytest.mark.unit

CALLERS = 5


def _js_where_nothing_exists(*, delay: float = 0.01) -> tuple[AsyncMock, list[str], list[str]]:
    """A context whose reads miss and whose creates take a moment, recording each create."""
    js = AsyncMock()
    buckets: list[str] = []
    stores: list[str] = []

    async def miss(_name):
        await asyncio.sleep(delay)
        raise nats.js.errors.BucketNotFoundError()

    async def create_bucket(**params):
        await asyncio.sleep(delay)
        buckets.append(params["bucket"])
        return AsyncMock()

    async def create_store(**params):
        await asyncio.sleep(delay)
        stores.append(params["bucket"])
        return AsyncMock()

    js.key_value.side_effect = miss
    js.object_store.side_effect = miss
    js.create_key_value.side_effect = create_bucket
    js.create_object_store.side_effect = create_store
    return js, buckets, stores


@pytest.mark.asyncio
async def test_concurrent_first_callers_provision_a_bucket_once_and_share_its_handle():
    js, created, _ = _js_where_nothing_exists()
    ext = KvExtension(js=js)

    handles = await asyncio.gather(*(ext.get_bucket("shared") for _ in range(CALLERS)))

    assert created == ["shared"]
    assert len({id(handle) for handle in handles}) == 1


@pytest.mark.asyncio
async def test_concurrent_first_callers_provision_an_object_store_once_and_share_its_handle():
    js, _, created = _js_where_nothing_exists()
    ext = KvExtension(js=js)

    handles = await asyncio.gather(*(ext.get_object_store("blobs") for _ in range(CALLERS)))

    assert created == ["blobs"]
    assert len({id(handle) for handle in handles}) == 1


@pytest.mark.asyncio
async def test_different_names_are_opened_at_the_same_time_not_one_after_another():
    """One lock for everything would also stop the race, and would serialise unrelated opens.

    Each create waits for the other to have started, so they only finish if they overlap.
    """
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def create(**params):
        mine, other = ("a", "b") if params["bucket"] == "a" else ("b", "a")
        started[mine].set()
        await started[other].wait()
        return AsyncMock()

    js.create_key_value.side_effect = create
    ext = KvExtension(js=js)

    async with asyncio.timeout(5):
        await asyncio.gather(ext.get_bucket("a"), ext.get_bucket("b"))

    assert js.create_key_value.await_count == 2


@pytest.mark.asyncio
async def test_a_bucket_that_is_already_open_is_returned_without_asking_the_broker_again():
    js, created, _ = _js_where_nothing_exists()
    ext = KvExtension(js=js)
    first = await ext.get_bucket("shared")
    reads = js.key_value.await_count

    again = await ext.get_bucket("shared")

    assert again is first
    assert js.key_value.await_count == reads
    assert created == ["shared"]


@pytest.mark.asyncio
async def test_a_first_open_that_fails_does_not_stop_the_next_caller_trying():
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    attempts = []

    async def create(**params):
        attempts.append(params["bucket"])
        if len(attempts) == 1:
            raise nats.js.errors.ServerError(code=500, err_code=1, description="busy")
        return AsyncMock()

    js.create_key_value.side_effect = create
    ext = KvExtension(js=js)

    with pytest.raises(nats.js.errors.ServerError):
        await ext.get_bucket("shared")
    handle = await ext.get_bucket("shared")

    assert attempts == ["shared", "shared"]
    assert handle is not None


@pytest.mark.asyncio
async def test_a_bucket_and_an_object_store_with_one_name_do_not_wait_for_each_other():
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    js.object_store.side_effect = nats.js.errors.BucketNotFoundError()
    started = {"bucket": asyncio.Event(), "store": asyncio.Event()}

    async def create_bucket(**_params):
        started["bucket"].set()
        await started["store"].wait()
        return AsyncMock()

    async def create_store(**_params):
        started["store"].set()
        await started["bucket"].wait()
        return AsyncMock()

    js.create_key_value.side_effect = create_bucket
    js.create_object_store.side_effect = create_store
    ext = KvExtension(js=js)

    async with asyncio.timeout(5):
        await asyncio.gather(ext.get_bucket("x"), ext.get_object_store("x"))
