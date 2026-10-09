"""The bucket and object-store configuration tables, read through what a start provisions.

Each assertion reads the table's effect: the request a first start sends, or the configuration a
declaration becomes, and not a restatement of the table's own construction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import BucketConfig, BucketConfigError, KvExtension, ObjectStoreConfig
from nats.js.api import Placement, RePublish

pytestmark = pytest.mark.unit


def _js_where_nothing_exists() -> AsyncMock:
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    js.object_store.side_effect = nats.js.errors.BucketNotFoundError()
    return js


# --- a name only a ttl map mentions ------------------------------------------


@pytest.mark.asyncio
async def test_a_bucket_only_the_ttl_map_names_is_provisioned_with_that_ttl():
    js = _js_where_nothing_exists()

    await KvExtension(bucket_ttls={"only_in_the_map": 60}, js=js).start()

    assert js.create_key_value.await_args.kwargs == {"bucket": "only_in_the_map", "ttl": 60.0}


@pytest.mark.asyncio
async def test_an_object_store_only_its_ttl_map_names_is_provisioned_with_that_ttl():
    js = _js_where_nothing_exists()

    await KvExtension(object_store_ttls={"only_in_the_map": 60}, js=js).start()

    assert js.create_object_store.await_args.kwargs == {"bucket": "only_in_the_map", "ttl": 60.0}


@pytest.mark.asyncio
async def test_a_ttl_map_entry_for_a_declared_bucket_adds_its_ttl_and_keeps_its_options():
    js = _js_where_nothing_exists()
    declared = BucketConfig(name="a", history=3)

    await KvExtension(buckets=[declared], bucket_ttls={"a": 60}, js=js).start()

    assert js.create_key_value.await_count == 1
    assert js.create_key_value.await_args.kwargs == {"bucket": "a", "ttl": 60.0, "history": 3}


# --- the name a declaration carries ------------------------------------------


def test_a_dictionary_naming_its_bucket_with_the_alias_takes_the_ttl_the_map_holds_for_it():
    ext = KvExtension(buckets=[{"bucket": "a"}], bucket_ttls={"a": 60})

    ext._ensure_initialized()

    assert ext._bucket_configs is not None and ext._bucket_configs["a"].ttl == 60


def test_a_dictionary_naming_its_store_with_the_alias_takes_the_ttl_the_map_holds_for_it():
    ext = KvExtension(object_stores=[{"bucket": "s"}], object_store_ttls={"s": 60})

    ext._ensure_initialized()

    assert ext._object_store_configs is not None and ext._object_store_configs["s"].ttl == 60


# --- a dictionary's structured options ---------------------------------------


def test_a_dictionary_placement_becomes_a_placement():
    config = BucketConfig.from_value({"name": "n", "placement": {"cluster": "c", "tags": ["t"]}})

    assert config.placement == Placement(cluster="c", tags=["t"])


def test_a_dictionary_republish_becomes_a_republish():
    config = BucketConfig.from_value({"name": "n", "republish": {"src": ">", "dest": "x.>"}})

    assert config.republish == RePublish(src=">", dest="x.>")


def test_a_placement_that_is_already_built_is_kept_as_it_is():
    placement = Placement(cluster="c")

    assert BucketConfig.from_value({"name": "n", "placement": placement}).placement is placement


# --- what each class says when a dictionary has no name ---------------------


def test_a_bucket_dictionary_with_no_name_says_it_is_a_bucket_config():
    with pytest.raises(BucketConfigError, match=r"^Bucket config dictionary must contain"):
        BucketConfig.from_value({"ttl": 5})


def test_an_object_store_dictionary_with_no_name_says_it_is_an_object_store_config():
    with pytest.raises(BucketConfigError, match=r"^ObjectStore config dictionary must contain"):
        ObjectStoreConfig.from_value({"ttl": 5})


def test_an_unknown_object_store_option_names_the_object_store():
    with pytest.raises(BucketConfigError, match=r"^Object store 'x': unknown option"):
        ObjectStoreConfig.from_value({"name": "x", "history": 5})
