"""What a declared configuration sends to JetStream, what `stop()` releases, and the config builders.

The mock tier below reads the arguments the extension hands the broker and the state it leaves
behind, which is where these behaviours are decided; the values a test plants on its own mock and
reads back prove nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nats.js.errors
import pytest
from cliffracer_kv import (
    BucketConfig,
    BucketConfigError,
    JetStreamUnavailableError,
    KvExtension,
    ObjectStoreConfig,
)

pytestmark = pytest.mark.unit


def _js_with_no_bucket() -> AsyncMock:
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    js.object_store.side_effect = nats.js.errors.BucketNotFoundError()
    return js


# --- every declared option reaches the broker ----------------------------------


@pytest.mark.asyncio
async def test_every_declared_bucket_option_reaches_create_key_value():
    js = _js_with_no_bucket()
    config = BucketConfig(
        name="full",
        ttl=60,
        description="d",
        history=5,
        max_bytes=1024,
        max_value_size=64,
        replicas=3,
        storage="memory",
    )

    await KvExtension(buckets=[config], js=js).start()

    assert js.create_key_value.await_args.kwargs == {
        "bucket": "full",
        "ttl": 60.0,
        "description": "d",
        "history": 5,
        "max_bytes": 1024,
        "max_value_size": 64,
        "replicas": 3,
        "storage": "memory",
    }


@pytest.mark.asyncio
async def test_a_bucket_with_no_options_sends_only_its_name():
    """An option left unset is absent from the request, not sent as None or as a default."""
    js = _js_with_no_bucket()

    await KvExtension(buckets=["bare"], js=js).start()

    assert js.create_key_value.await_args.kwargs == {"bucket": "bare"}


@pytest.mark.asyncio
async def test_every_declared_object_store_option_reaches_create_object_store():
    js = _js_with_no_bucket()
    config = ObjectStoreConfig(
        name="blobs", ttl=60, description="d", max_bytes=1024, replicas=3, storage="memory"
    )

    await KvExtension(object_stores=[config], js=js).start()

    assert js.create_object_store.await_args.kwargs == {
        "bucket": "blobs",
        "ttl": 60.0,
        "description": "d",
        "max_bytes": 1024,
        "replicas": 3,
        "storage": "memory",
    }


@pytest.mark.asyncio
async def test_an_object_store_with_no_options_sends_only_its_name():
    js = _js_with_no_bucket()

    await KvExtension(object_stores=["bare"], js=js).start()

    assert js.create_object_store.await_args.kwargs == {"bucket": "bare"}


# --- stop() releases what start() took -----------------------------------------


@pytest.mark.asyncio
async def test_stop_drops_the_handles_and_health_stops_reporting_them():
    js = AsyncMock()
    ext = KvExtension(buckets=["cache"], object_stores=["blobs"], js=js)
    await ext.start()

    assert ext.health_details() == {
        "connected": True,
        "buckets": ["cache"],
        "object_stores": ["blobs"],
    }

    await ext.stop()

    assert ext.health_details() == {"connected": False, "buckets": [], "object_stores": []}


@pytest.mark.asyncio
async def test_a_handle_asked_for_after_stop_is_opened_again_not_served_from_the_cache():
    """A restarted service must not keep handles bound to the connection it just closed."""
    js = AsyncMock()
    ext = KvExtension(buckets=["cache"], js=js)
    await ext.start()
    opened = js.key_value.await_count

    await ext.stop()
    await ext.get_bucket("cache")

    assert js.key_value.await_count == opened + 1


# --- the empty-store promise ---------------------------------------------------


@pytest.mark.asyncio
async def test_listing_a_store_that_has_no_objects_returns_an_empty_list():
    js = AsyncMock()
    store = AsyncMock()
    store.list.side_effect = nats.js.errors.NotFoundError()
    js.object_store.return_value = store
    ext = KvExtension(object_stores=["blobs"], js=js)
    await ext.start()

    assert await ext.list_objects("blobs") == []


# --- a connection that cannot make a JetStream context -------------------------


@pytest.mark.asyncio
async def test_an_explicit_connection_that_cannot_make_a_context_is_named_in_the_error():
    nc = MagicMock()
    nc.jetstream.side_effect = RuntimeError("no jetstream here")
    ext = KvExtension(buckets=["cache"], nc=nc)

    with pytest.raises(JetStreamUnavailableError, match="explicit NATS connection") as caught:
        await ext.start()

    assert "no jetstream here" in str(caught.value)


# --- the config builders, read through what they return ------------------------


def test_a_bucket_dictionary_carries_every_option_it_names():
    config = BucketConfig.from_value(
        {
            "name": "full",
            "ttl": 30,
            "description": "d",
            "history": 5,
            "max_bytes": 1024,
            "max_value_size": 64,
            "replicas": 3,
            "storage": "memory",
        }
    )

    assert (
        config.name,
        config.ttl,
        config.description,
        config.history,
        config.max_bytes,
        config.max_value_size,
        config.replicas,
        config.storage,
    ) == ("full", 30, "d", 5, 1024, 64, 3, "memory")


def test_an_object_store_dictionary_carries_every_option_it_names():
    config = ObjectStoreConfig.from_value(
        {
            "name": "blobs",
            "ttl": 30,
            "description": "d",
            "max_bytes": 1024,
            "replicas": 3,
            "storage": "memory",
        }
    )

    assert (
        config.name,
        config.ttl,
        config.description,
        config.max_bytes,
        config.replicas,
        config.storage,
    ) == ("blobs", 30, "d", 1024, 3, "memory")


@pytest.mark.parametrize("config", [BucketConfig, ObjectStoreConfig])
def test_a_dictionary_with_no_ttl_takes_the_default(config):
    assert config.from_value({"name": "x"}, default_ttl=120).ttl == 120


def test_an_object_store_with_no_ttl_takes_the_default_and_keeps_its_other_options():
    original = ObjectStoreConfig(
        name="blobs", description="d", max_bytes=1024, replicas=3, storage="memory"
    )

    config = ObjectStoreConfig.from_value(original, default_ttl=120)

    assert (
        config.name,
        config.ttl,
        config.description,
        config.max_bytes,
        config.replicas,
        config.storage,
    ) == ("blobs", 120, "d", 1024, 3, "memory")
    assert original.ttl is None


@pytest.mark.parametrize("config", [BucketConfig, ObjectStoreConfig])
def test_a_dictionary_whose_name_is_not_a_string_is_refused(config):
    with pytest.raises(BucketConfigError):
        config.from_value({"name": 5})


@pytest.mark.parametrize("config", [BucketConfig, ObjectStoreConfig])
def test_the_bucket_key_is_an_alias_for_the_name(config):
    assert config.from_value({"bucket": "x"}).name == "x"
