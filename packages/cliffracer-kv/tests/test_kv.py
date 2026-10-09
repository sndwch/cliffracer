"""Comprehensive unit tests for cliffracer-kv."""

from __future__ import annotations

import io
import json
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import nats.js.errors
import pytest
from cliffracer_kv import (
    BucketConfig,
    BucketConfigError,
    JetStreamUnavailableError,
    KvError,
    KvExtension,
    ObjectStoreConfig,
)
from cliffracer_kv.config import normalize_ttl_seconds
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig
from conftest import broker_url

pytestmark = pytest.mark.unit


class SampleUser(BaseModel):
    id: str
    username: str
    is_active: bool = True


# ==============================================================================
# 1. Config & TTL Unit Tests
# ==============================================================================


def test_normalize_ttl_seconds():
    assert normalize_ttl_seconds(None) is None
    assert normalize_ttl_seconds(60) == 60.0
    assert normalize_ttl_seconds(3600.5) == 3600.5
    assert normalize_ttl_seconds(timedelta(minutes=5)) == 300.0
    assert normalize_ttl_seconds(timedelta(hours=1)) == 3600.0

    with pytest.raises(BucketConfigError, match="Invalid TTL value"):
        normalize_ttl_seconds("not-a-number")


def test_bucket_config_from_value():
    # From string
    cfg = BucketConfig.from_value("users", default_ttl=300)
    assert cfg.name == "users"
    assert cfg.ttl == 300.0

    # From BucketConfig
    cfg2 = BucketConfig.from_value(BucketConfig(name="cache", ttl=60))
    assert cfg2.name == "cache"
    assert cfg2.ttl == 60

    # From BucketConfig with None ttl getting default
    cfg3 = BucketConfig.from_value(BucketConfig(name="sessions"), default_ttl=120)
    assert cfg3.name == "sessions"
    assert cfg3.ttl == 120

    # From dict
    cfg4 = BucketConfig.from_value({"name": "events", "ttl": timedelta(days=1), "history": 5})
    assert cfg4.name == "events"
    assert cfg4.ttl == timedelta(days=1)
    assert cfg4.history == 5

    # From dict using 'bucket' key
    cfg5 = BucketConfig.from_value({"bucket": "metrics", "max_bytes": 1024})
    assert cfg5.name == "metrics"
    assert cfg5.max_bytes == 1024

    # Invalid values
    with pytest.raises(BucketConfigError, match="must contain a valid string"):
        BucketConfig.from_value({"other": "field"})

    with pytest.raises(BucketConfigError, match="Cannot create BucketConfig"):
        BucketConfig.from_value(123)


def test_object_store_config_from_value():
    # From string
    cfg = ObjectStoreConfig.from_value("assets", default_ttl=3600)
    assert cfg.name == "assets"
    assert cfg.ttl == 3600

    # From ObjectStoreConfig
    cfg2 = ObjectStoreConfig.from_value(ObjectStoreConfig(name="backups", ttl=86400))
    assert cfg2.name == "backups"
    assert cfg2.ttl == 86400

    # From dict
    cfg3 = ObjectStoreConfig.from_value({"bucket": "media", "ttl": timedelta(hours=2)})
    assert cfg3.name == "media"
    assert cfg3.ttl == timedelta(hours=2)

    with pytest.raises(BucketConfigError, match="must contain a valid string"):
        ObjectStoreConfig.from_value({})

    with pytest.raises(BucketConfigError, match="Cannot create ObjectStoreConfig"):
        ObjectStoreConfig.from_value(None)


# ==============================================================================
# 2. Serialization & Deserialization Unit Tests
# ==============================================================================


def test_serialize_and_deserialize_pydantic_model():
    user = SampleUser(id="u123", username="alice", is_active=True)
    serialized = serialize_value(user)
    assert isinstance(serialized, bytes)
    assert b"alice" in serialized

    # Deserialized with explicit model type
    res = deserialize_value(serialized, as_type=SampleUser)
    assert isinstance(res, SampleUser)
    assert res.id == "u123"
    assert res.username == "alice"
    assert res.is_active is True

    # Deserialized with as_type=None infers JSON dict
    res_inferred = deserialize_value(serialized)
    assert isinstance(res_inferred, dict)
    assert res_inferred["id"] == "u123"


def test_serialize_and_deserialize_primitives():
    # Dict
    d = {"key": "value", "count": 42}
    raw = serialize_value(d)
    assert deserialize_value(raw) == d
    assert deserialize_value(raw, as_type=dict) == d

    # List
    lst = [1, 2, "three"]
    assert deserialize_value(serialize_value(lst)) == lst
    assert deserialize_value(serialize_value(lst), as_type=list) == lst

    # String
    s = "hello nats kv"
    raw_s = serialize_value(s)
    assert deserialize_value(raw_s) == s
    assert deserialize_value(raw_s, as_type=str) == s

    # Raw bytes
    b = b"\x00\x01\x02\xff"
    assert serialize_value(b) == b
    assert deserialize_value(b, as_type=bytes) == b
    assert deserialize_value(b) == b

    # Missing / None
    assert deserialize_value(None, default="fallback") == "fallback"
    assert deserialize_value(None, as_type=SampleUser, default=None) is None


# ==============================================================================
# 3. Bucket Auto-Provisioning & TTL Configuration Unit Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_bucket_auto_provisioning_with_bucket_level_ttl():
    """Verify that bucket-level TTL configuration is passed directly to JetStream."""
    js_mock = AsyncMock()
    # First key_value check raises BucketNotFoundError to trigger auto-provisioning
    js_mock.key_value.side_effect = nats.js.errors.BucketNotFoundError()

    kv_mock = AsyncMock()
    js_mock.create_key_value.return_value = kv_mock

    ext = KvExtension(
        buckets=[
            "cache",
            BucketConfig(name="sessions", ttl=timedelta(minutes=30)),
            {"name": "temp_tokens", "ttl": 60},
        ],
        bucket_ttls={"cache": 3600.0},
        js=js_mock,
    )

    await ext.start()

    # Verify create_key_value calls
    assert js_mock.create_key_value.await_count == 3

    calls = js_mock.create_key_value.call_args_list
    call_dict = {call.kwargs["bucket"]: call.kwargs for call in calls}

    # Verify 'cache' received TTL 3600.0
    assert "cache" in call_dict
    assert call_dict["cache"]["ttl"] == 3600.0

    # Verify 'sessions' received timedelta converted to 1800.0 seconds
    assert "sessions" in call_dict
    assert call_dict["sessions"]["ttl"] == 1800.0

    # Verify 'temp_tokens' received 60.0 seconds
    assert "temp_tokens" in call_dict
    assert call_dict["temp_tokens"]["ttl"] == 60.0


@pytest.mark.asyncio
async def test_object_store_auto_provisioning_with_ttl():
    """Verify that object store provisioning passes TTL to JetStream create_object_store."""
    js_mock = AsyncMock()
    js_mock.object_store.side_effect = nats.js.errors.BucketNotFoundError()

    obj_mock = AsyncMock()
    js_mock.create_object_store.return_value = obj_mock

    ext = KvExtension(
        object_stores=[
            "media",
            ObjectStoreConfig(name="ephemeral_blobs", ttl=timedelta(hours=1)),
        ],
        object_store_ttls={"media": 7200.0},
        js=js_mock,
    )

    await ext.start()

    assert js_mock.create_object_store.await_count == 2
    calls = {c.kwargs["bucket"]: c.kwargs for c in js_mock.create_object_store.call_args_list}

    assert calls["media"]["ttl"] == 7200.0
    assert calls["ephemeral_blobs"]["ttl"] == 3600.0


# ==============================================================================
# 4. Key-Value Operations Mock Unit Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_kv_put_and_get_operations():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock

    # Put mock returns revision sequence
    kv_mock.put.return_value = 1

    # Get mock returns an Entry
    entry = SimpleNamespace(
        value=b'{"id": "u1", "username": "bob", "is_active": true}', operation=None
    )
    kv_mock.get.return_value = entry

    ext = KvExtension(js=js_mock)

    # Put model
    user = SampleUser(id="u1", username="bob")
    rev = await ext.put("users", "u1", user)
    assert rev == 1
    assert kv_mock.put.await_count == 1
    call_key, call_val = kv_mock.put.call_args.args
    assert call_key == "u1"
    assert b"bob" in call_val

    # Get model
    retrieved = await ext.get("users", "u1", as_type=SampleUser)
    assert isinstance(retrieved, SampleUser)
    assert retrieved.username == "bob"


@pytest.mark.asyncio
async def test_kv_get_missing_key_returns_default():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock

    # KeyNotFoundError on get
    kv_mock.get.side_effect = nats.js.errors.KeyNotFoundError(entry=None, op="GET")

    ext = KvExtension(js=js_mock)
    val = await ext.get("users", "nonexistent", default="missing_default")
    assert val == "missing_default"


@pytest.mark.asyncio
async def test_a_put_with_a_revision_is_an_update_of_that_key_at_that_revision():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    kv_mock.update.return_value = 2

    ext = KvExtension(js=js_mock)
    rev = await ext.put("users", "u1", {"status": "updated"}, revision=1)

    assert kv_mock.put.await_count == 0, "a revision-checked write must not be a plain put"
    assert kv_mock.update.await_count == 1
    key, payload = kv_mock.update.await_args.args
    assert key == "u1"
    assert json.loads(payload) == {"status": "updated"}
    assert kv_mock.update.await_args.kwargs == {"last": 1}
    assert rev == 2


@pytest.mark.asyncio
async def test_kv_delete_and_purge():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    kv_mock.delete.return_value = True
    kv_mock.purge.return_value = True

    ext = KvExtension(js=js_mock)

    assert await ext.delete("users", "u1") is None
    assert kv_mock.delete.await_args.args == ("u1",)
    assert kv_mock.delete.await_args.kwargs == {"last": None}

    assert await ext.purge("users", "u1") is None
    assert kv_mock.purge.await_args.args == ("u1",)
    assert kv_mock.purge.await_args.kwargs == {"msg_ttl": None}


@pytest.mark.asyncio
async def test_delete_hands_the_revision_to_the_broker_so_a_stale_one_is_refused_there():
    kv_mock = AsyncMock()
    js_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    refusal = nats.js.errors.BadRequestError(code=400, err_code=10071, description="wrong last")
    kv_mock.delete.side_effect = refusal

    ext = KvExtension(js=js_mock)

    with pytest.raises(nats.js.errors.BadRequestError) as caught:
        await ext.delete("users", "u1", last=7)

    assert caught.value is refusal
    assert kv_mock.delete.await_args.kwargs == {"last": 7}


# ==============================================================================
# 5. Object Store Operations Mock Unit Tests
# ==============================================================================


def _object_store():
    js_mock = AsyncMock()
    obj_mock = AsyncMock()
    js_mock.object_store.return_value = obj_mock
    return KvExtension(js=js_mock), obj_mock


@pytest.mark.asyncio
async def test_put_object_stores_the_bytes_under_the_name_with_the_meta():
    ext, obj_mock = _object_store()
    meta = SimpleNamespace(description="a report")

    result = await ext.put_object("docs", "report.pdf", b"%PDF-1.4...", meta=meta)

    assert obj_mock.put.await_args.args == ("report.pdf", b"%PDF-1.4...")
    assert obj_mock.put.await_args.kwargs == {"meta": meta}
    assert result is obj_mock.put.return_value, "the store's own info comes back unwrapped"


@pytest.mark.asyncio
async def test_put_object_encodes_a_string_and_sends_no_meta_by_default():
    ext, obj_mock = _object_store()

    await ext.put_object("docs", "note.txt", "hello string")

    assert obj_mock.put.await_args.args == ("note.txt", b"hello string")
    assert obj_mock.put.await_args.kwargs == {"meta": None}


@pytest.mark.asyncio
async def test_get_object_asks_for_the_name_and_hands_back_the_stores_result():
    ext, obj_mock = _object_store()

    result = await ext.get_object("docs", "report.pdf")

    assert obj_mock.get.await_args.args == ("report.pdf",)
    assert obj_mock.get.await_args.kwargs == {"writeinto": None, "show_deleted": False}
    assert result is obj_mock.get.return_value


@pytest.mark.asyncio
async def test_get_object_forwards_where_to_write_and_whether_to_show_deleted():
    ext, obj_mock = _object_store()
    sink = io.BytesIO()

    await ext.get_object("docs", "report.pdf", writeinto=sink, show_deleted=True)

    assert obj_mock.get.await_args.kwargs == {"writeinto": sink, "show_deleted": True}


@pytest.mark.asyncio
async def test_get_object_returns_none_for_a_name_the_store_does_not_have():
    ext, obj_mock = _object_store()
    obj_mock.get.side_effect = nats.js.errors.ObjectNotFoundError()

    assert await ext.get_object("docs", "missing.pdf") is None


@pytest.mark.asyncio
async def test_delete_object_deletes_the_name_and_hands_back_the_stores_answer():
    ext, obj_mock = _object_store()

    result = await ext.delete_object("docs", "report.pdf")

    assert obj_mock.delete.await_args.args == ("report.pdf",)
    assert result is obj_mock.delete.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore_deletes", [False, True])
async def test_list_objects_forwards_ignore_deletes_and_hands_back_the_list(ignore_deletes):
    ext, obj_mock = _object_store()
    listed = [SimpleNamespace(name="report.pdf")]
    obj_mock.list.return_value = listed

    result = await ext.list_objects("docs", ignore_deletes=ignore_deletes)

    assert obj_mock.list.await_args.kwargs == {"ignore_deletes": ignore_deletes}
    assert result is listed


# ==============================================================================
# 6. JetStream Unavailable Error Handling
# ==============================================================================


@pytest.mark.asyncio
async def test_jetstream_unavailable_raises_error():
    # Neither service nor js provided
    ext = KvExtension()
    with pytest.raises(JetStreamUnavailableError, match="no JetStream context"):
        await ext.start()

    # Service without JetStream enabled
    service = SimpleNamespace(js=None, nc=None)
    ext_bound = ext.bind(service, "kv")
    with pytest.raises(JetStreamUnavailableError, match="no JetStream context"):
        await ext_bound.start()

    # Service with nc whose jetstream() raises error
    nc_mock = MagicMock()
    nc_mock.jetstream.side_effect = RuntimeError("JetStream not enabled on server")
    service_with_nc = SimpleNamespace(js=None, nc=nc_mock)
    ext_bound_nc = ext.bind(service_with_nc, "kv")
    with pytest.raises(JetStreamUnavailableError, match="Failed to create JetStream"):
        await ext_bound_nc.start()


# ==============================================================================
# 7. Cliffracer Extension Service Binding
# ==============================================================================


@pytest.mark.asyncio
async def test_two_services_do_not_share_kv_state():
    """Two bound instances hold distinct KV and object store state.

    This cannot fail on state built in `__init__`: `bind()` runs `__init__` again
    for each service, so such state is never shared either.
    """

    class IsolatedKvService(CliffracerService):
        kv = KvExtension(buckets=["shared_spec_bucket"])

    cfg_a = ServiceConfig(name="svca", nats_url=broker_url(), jetstream_enabled=False)
    cfg_b = ServiceConfig(name="svcb", nats_url=broker_url(), jetstream_enabled=False)

    svc_a = IsolatedKvService(config=cfg_a)
    svc_b = IsolatedKvService(config=cfg_b)

    # Extension instances are bound distinctly per service instance
    assert hasattr(svc_a, "kv")
    assert hasattr(svc_b, "kv")
    assert svc_a.kv is not svc_b.kv

    await svc_a.container._setup_extensions()
    await svc_b.container._setup_extensions()

    # Storage handle dictionaries are distinct instances
    assert svc_a.kv._kv_stores is not svc_b.kv._kv_stores
    assert svc_a.kv._obj_stores is not svc_b.kv._obj_stores
    assert svc_a.kv._bucket_configs is not svc_b.kv._bucket_configs

    # Behavioral check: mutating handle cache on instance A does not leak to instance B
    sentinel_kv = object()
    sentinel_obj = object()
    svc_a.kv._kv_stores["sentinel_bucket"] = sentinel_kv
    svc_a.kv._obj_stores["sentinel_store"] = sentinel_obj

    assert "sentinel_bucket" not in svc_b.kv._kv_stores
    assert "sentinel_store" not in svc_b.kv._obj_stores


@pytest.mark.asyncio
async def test_bucket_auto_provisioning_disabled_raises_on_missing():
    """Verify that create_if_missing=False re-raises BucketNotFoundError and does not create bucket."""
    js_mock = AsyncMock()
    js_mock.key_value.side_effect = nats.js.errors.BucketNotFoundError()

    ext = KvExtension(
        buckets=["nonexistent_bucket"],
        create_if_missing=False,
        js=js_mock,
    )

    with pytest.raises(nats.js.errors.BucketNotFoundError):
        await ext.start()

    assert js_mock.create_key_value.await_count == 0


@pytest.mark.asyncio
async def test_object_store_auto_provisioning_disabled_raises_on_missing():
    """Verify that create_if_missing=False re-raises on missing object store and does not create store."""
    js_mock = AsyncMock()
    js_mock.object_store.side_effect = nats.js.errors.BucketNotFoundError()

    ext = KvExtension(
        object_stores=["nonexistent_store"],
        create_if_missing=False,
        js=js_mock,
    )

    with pytest.raises(nats.js.errors.BucketNotFoundError):
        await ext.start()

    assert js_mock.create_object_store.await_count == 0


# ==============================================================================
# 8. Live Integration Test against Real JetStream Broker
# ==============================================================================


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_jetstream_kv_with_bucket_ttl():
    """End-to-end integration test with live NATS JetStream verifying bucket TTL and full API."""
    nats_url = os.getenv("CLIFFRACER_TEST_NATS_URL", broker_url())
    import nats

    try:
        nc = await nats.connect(nats_url, connect_timeout=1)
        js = nc.jetstream()
    except Exception:
        pytest.skip(f"Live NATS server with JetStream not reachable at {nats_url}")

    # A name per run: two runs on one broker must not delete each other's bucket.
    run = uuid.uuid4().hex[:8]
    bucket_name = f"test_live_ttl_bucket_{run}"
    obj_store_name = f"test_live_ttl_obj_{run}"

    # Clean up prior leftovers if any
    try:
        await js.delete_key_value(bucket_name)
    except Exception:
        pass
    try:
        await js.delete_object_store(obj_store_name)
    except Exception:
        pass

    try:
        # Create extension with bucket TTL configuration (300 seconds) and history=5
        ext = KvExtension(
            buckets=[BucketConfig(name=bucket_name, ttl=300.0, history=5)],
            object_stores=[ObjectStoreConfig(name=obj_store_name, ttl=timedelta(minutes=10))],
            js=js,
        )
        await ext.start()

        # 1. VERIFY BUCKET-LEVEL TTL CONFIGURATION DIRECTLY FROM JETSTREAM STREAM
        stream_info = await js.stream_info(f"KV_{bucket_name}")
        assert stream_info.config.max_age == 300.0, (
            "Bucket-level TTL must be applied to JetStream stream max_age"
        )

        obj_stream_info = await js.stream_info(f"OBJ_{obj_store_name}")
        assert obj_stream_info.config.max_age == 600.0, (
            "ObjectStore TTL must be applied to JetStream stream max_age"
        )

        # 2. Put and Get Pydantic model
        user = SampleUser(id="usr_1", username="carol", is_active=True)
        rev = await ext.put(bucket_name, "user.1", user)
        assert rev >= 1

        fetched_user = await ext.get(bucket_name, "user.1", as_type=SampleUser)
        assert isinstance(fetched_user, SampleUser)
        assert fetched_user.username == "carol"

        # 3. Put and Get Dict
        rev2 = await ext.put(bucket_name, "config.app", {"env": "test", "threads": 4})
        assert rev2 >= 1
        fetched_dict = await ext.get(bucket_name, "config.app")
        assert fetched_dict == {"env": "test", "threads": 4}

        # 4. Optimistic concurrency check
        rev3 = await ext.put(
            bucket_name, "user.1", SampleUser(id="usr_1", username="carol_updated"), revision=rev
        )
        assert rev3 > rev

        with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
            # Wrong revision sequence must fail
            await ext.put(
                bucket_name, "user.1", SampleUser(id="usr_1", username="stale"), revision=rev
            )

        # 5. Keys and history
        all_keys = await ext.keys(bucket_name)
        assert "user.1" in all_keys
        assert "config.app" in all_keys

        hist = await ext.history(bucket_name, "user.1")
        assert len(hist) >= 2

        # 6. Object store put and get
        obj_info = await ext.put_object(obj_store_name, "doc.txt", b"Hello JetStream Object Store")
        assert obj_info.name == "doc.txt"

        obj_res = await ext.get_object(obj_store_name, "doc.txt")
        assert obj_res is not None
        assert obj_res.data == b"Hello JetStream Object Store"

        obj_list = await ext.list_objects(obj_store_name)
        assert any(o.name == "doc.txt" for o in obj_list)

        # 7. Delete object and delete key
        await ext.delete_object(obj_store_name, "doc.txt")
        assert await ext.get_object(obj_store_name, "doc.txt") is None

        assert await ext.delete(bucket_name, "config.app") is None
        assert await ext.get(bucket_name, "config.app") is None

        # 8. Health details
        details = ext.health_details()
        assert details["connected"] is True
        assert bucket_name in details["buckets"]
        assert obj_store_name in details["object_stores"]

    finally:
        try:
            await js.delete_key_value(bucket_name)
        except Exception:
            pass
        try:
            await js.delete_object_store(obj_store_name)
        except Exception:
            pass
        await nc.close()


# ==============================================================================
# 9. An error that is not a not-found must not be read as one
# ==============================================================================
#
# Every read path here pairs a typed `except` with, until now, a text test on
# the message: `"not found" in str(exc).lower()`. The typed half is correct.
# The text half could only ever see exceptions the typed half had already
# declined, so it never fired for the condition it named -- and fired only for
# errors that were NOT that condition but happened to use the word.
#
# Measured against nats-py 2.15.0, which is what the package now requires:
# every condition these paths meet is raised typed. `js.key_value()` raises
# BucketNotFoundError, `kv.get()` converts a 404 to KeyNotFoundError,
# `kv.watch()` raises what its subscription meets, and `APIError.from_error` maps code 404 onto
# NotFoundError. A 500 or a 400 whose description merely contains "not found"
# is a real failure and must reach the caller.
#
# These rows are the falsification: each one passes now and fails against a
# text fallback, because a text fallback swallows exactly these.

# Each fixture carries the word THAT SITE's fallback tested for, so every row
# below fails if the fallback returns. A 500 saying "stream not found" does not
# contain "key not found", and would have made the key-path rows pass in both
# directions -- controls that cannot fail.
SERVER_NOT_FOUND = nats.js.errors.ServerError(
    code=500, err_code=10062, description="stream not found"
)
SERVER_KEY_NOT_FOUND = nats.js.errors.ServerError(
    code=500, err_code=10062, description="key not found in stream while reading"
)
SERVER_NO_KEYS = nats.js.errors.ServerError(
    code=500, err_code=10062, description="no keys could be listed, stream unhealthy"
)
BAD_REQUEST_DELETED = nats.js.errors.BadRequestError(
    code=400, err_code=10071, description="consumer deleted"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [SERVER_KEY_NOT_FOUND, BAD_REQUEST_DELETED],
    ids=lambda e: e.description.split()[0] + "-" + type(e).__name__,
)
async def test_a_server_failure_mentioning_a_missing_key_is_not_a_missing_key(failure):
    """`get` must raise, not hand back the default.

    Returning the default here is the silent direction: the caller cannot tell
    a missing key from a broker that failed while answering.
    """
    kv_mock = AsyncMock()
    kv_mock.get.side_effect = failure
    js_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock

    ext = KvExtension(buckets=["cache"], js=js_mock)
    await ext.start()

    with pytest.raises(type(failure)):
        await ext.get("cache", "some-key", default="fallback")


@pytest.mark.asyncio
async def test_a_server_failure_mentioning_not_found_does_not_provision_a_bucket():
    """The worst case: an unrelated failure creating infrastructure.

    `_ensure_bucket` auto-provisions when the bucket is absent and
    `create_if_missing` is set. A 500 that merely says "stream not found" is
    not an absent bucket, and must not be answered by creating one.
    """
    js_mock = AsyncMock()
    js_mock.key_value.side_effect = SERVER_NOT_FOUND

    ext = KvExtension(buckets=["cache"], create_if_missing=True, js=js_mock)

    with pytest.raises(nats.js.errors.ServerError):
        await ext.start()

    assert js_mock.create_key_value.await_count == 0, (
        "a server error was answered by creating a bucket"
    )


@pytest.mark.asyncio
async def test_a_server_failure_mentioning_not_found_does_not_provision_an_object_store():
    """The same for the object-store half of the same shape."""
    js_mock = AsyncMock()
    js_mock.object_store.side_effect = SERVER_NOT_FOUND

    ext = KvExtension(object_stores=["blobs"], create_if_missing=True, js=js_mock)

    with pytest.raises(nats.js.errors.ServerError):
        await ext.start()

    assert js_mock.create_object_store.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args", "failure"),
    [
        ("delete", ("cache", "k"), SERVER_KEY_NOT_FOUND),
        ("purge", ("cache", "k"), SERVER_KEY_NOT_FOUND),
        ("keys", ("cache",), SERVER_NO_KEYS),
        ("history", ("cache", "k"), SERVER_KEY_NOT_FOUND),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_a_server_failure_is_not_an_absent_key_on_any_read_path(method, args, failure):
    """Every path that returns an empty answer for absence must still raise."""
    kv_mock = AsyncMock()
    for name in ("get", "delete", "purge", "watch"):
        getattr(kv_mock, name).side_effect = failure
    js_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock

    ext = KvExtension(buckets=["cache"], js=js_mock)
    await ext.start()

    with pytest.raises(nats.js.errors.ServerError):
        await getattr(ext, method)(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_object", ("blobs", "n")),
        ("delete_object", ("blobs", "n")),
        ("list_objects", ("blobs",)),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_a_server_failure_is_not_an_absent_object(method, args):
    """And the object-store read paths, which returned None or []."""
    store_mock = AsyncMock()
    for name in ("get", "delete", "list"):
        getattr(store_mock, name).side_effect = SERVER_NOT_FOUND
    js_mock = AsyncMock()
    js_mock.object_store.return_value = store_mock

    ext = KvExtension(object_stores=["blobs"], js=js_mock)
    await ext.start()

    with pytest.raises(nats.js.errors.ServerError):
        await getattr(ext, method)(*args)


def test_the_typed_errors_these_paths_rely_on_carry_no_shared_word():
    """Why the typed check is enough, stated as an assertion rather than prose.

    Each condition arrives as its own class. Nothing here needs the message,
    and reading the message is what ADR-0011 forbids.
    """
    for cls in (
        nats.js.errors.BucketNotFoundError,
        nats.js.errors.KeyNotFoundError,
        nats.js.errors.KeyDeletedError,
        nats.js.errors.NoKeysError,
        nats.js.errors.ObjectNotFoundError,
    ):
        assert issubclass(cls, nats.js.errors.Error), cls

    # The structured field exists and is what a fallback would have to use.
    err = nats.js.errors.NotFoundError(code=404, err_code=10059, description="stream not found")
    assert err.code == 404
    assert err.err_code == 10059


@pytest.mark.asyncio
async def test_a_stale_revision_reaches_the_caller_as_the_native_error_not_a_kv_error():
    """`KvError` covers what this package raises; nats-py's errors propagate unchanged."""
    kv_mock = AsyncMock()
    js_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    kv_mock.update.side_effect = nats.js.errors.KeyWrongLastSequenceError(
        description="wrong last sequence"
    )

    ext = KvExtension(js=js_mock)

    with pytest.raises(nats.js.errors.KeyWrongLastSequenceError) as caught:
        await ext.put("users", "u1", {"a": 1}, revision=3)

    assert not isinstance(caught.value, KvError)
    assert issubclass(BucketConfigError, KvError)
    assert issubclass(JetStreamUnavailableError, KvError)
