"""Comprehensive unit tests for cliffracer-kv."""

from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace
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
from cliffracer_kv.config import normalize_ttl_seconds
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig


class SampleUser(BaseModel):
    id: str
    username: str
    is_active: bool = True


# ==============================================================================
# 1. Config & TTL Unit Tests
# ==============================================================================


@pytest.mark.unit
def test_normalize_ttl_seconds():
    assert normalize_ttl_seconds(None) is None
    assert normalize_ttl_seconds(60) == 60.0
    assert normalize_ttl_seconds(3600.5) == 3600.5
    assert normalize_ttl_seconds(timedelta(minutes=5)) == 300.0
    assert normalize_ttl_seconds(timedelta(hours=1)) == 3600.0

    with pytest.raises(BucketConfigError, match="Invalid TTL value"):
        normalize_ttl_seconds("not-a-number")


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_object_store_auto_provisioning_with_ttl():
    """Verify that object store provisioning passes TTL to JetStream create_object_store."""
    js_mock = AsyncMock()
    js_mock.object_store.side_effect = nats.js.errors.ObjectNotFoundError()

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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kv_optimistic_concurrency_revision():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    kv_mock.update.return_value = 2

    ext = KvExtension(js=js_mock)
    rev = await ext.put("users", "u1", {"status": "updated"}, revision=1)
    assert rev == 2
    assert kv_mock.update.await_count == 1
    assert kv_mock.update.call_args.kwargs["last"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kv_delete_and_purge():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    kv_mock.delete.return_value = True
    kv_mock.purge.return_value = True

    ext = KvExtension(js=js_mock)

    assert await ext.delete("users", "u1") is True
    assert kv_mock.delete.await_count == 1

    assert await ext.purge("users", "u1") is True
    assert kv_mock.purge.await_count == 1

    # Missing key on delete returns False
    kv_mock.delete.side_effect = nats.js.errors.KeyNotFoundError(entry=None, op="DEL")
    assert await ext.delete("users", "missing") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kv_keys_and_history():
    js_mock = AsyncMock()
    kv_mock = AsyncMock()
    js_mock.key_value.return_value = kv_mock
    kv_mock.keys.return_value = ["k1", "k2"]
    kv_mock.history.return_value = [SimpleNamespace(revision=1), SimpleNamespace(revision=2)]

    ext = KvExtension(js=js_mock)
    assert await ext.keys("bucket") == ["k1", "k2"]
    assert len(await ext.history("bucket", "k1")) == 2

    # Empty bucket raises NoKeysError in nats-py, ext returns []
    kv_mock.keys.side_effect = nats.js.errors.NoKeysError()
    kv_mock.history.side_effect = nats.js.errors.NoKeysError()
    assert await ext.keys("empty_bucket") == []
    assert await ext.history("empty_bucket", "k1") == []


# ==============================================================================
# 5. Object Store Operations Mock Unit Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_object_store_operations():
    js_mock = AsyncMock()
    obj_mock = AsyncMock()
    js_mock.object_store.return_value = obj_mock

    obj_mock.put.return_value = SimpleNamespace(name="report.pdf", size=1024)
    obj_mock.get.return_value = SimpleNamespace(
        data=b"%PDF-1.4...", info=SimpleNamespace(name="report.pdf")
    )
    obj_mock.delete.return_value = True
    obj_mock.list.return_value = [SimpleNamespace(name="report.pdf")]

    ext = KvExtension(js=js_mock)

    # Put
    info = await ext.put_object("docs", "report.pdf", b"%PDF-1.4...")
    assert info.name == "report.pdf"

    # Put str
    await ext.put_object("docs", "note.txt", "hello string")
    assert obj_mock.put.call_args.args[1] == b"hello string"

    # Get
    res = await ext.get_object("docs", "report.pdf")
    assert res.data.startswith(b"%PDF")

    # Get not found
    obj_mock.get.side_effect = nats.js.errors.ObjectNotFoundError()
    assert await ext.get_object("docs", "missing.pdf") is None

    # Delete
    assert await ext.delete_object("docs", "report.pdf") is True

    # List
    items = await ext.list_objects("docs")
    assert len(items) == 1


# ==============================================================================
# 6. JetStream Unavailable Error Handling
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jetstream_unavailable_raises_error():
    # Neither service nor js provided
    ext = KvExtension()
    with pytest.raises(JetStreamUnavailableError, match="JetStream is required"):
        await ext.start()

    # Service without JetStream enabled
    service = SimpleNamespace(js=None, nc=None)
    ext_bound = ext.bind(service, "kv")
    with pytest.raises(JetStreamUnavailableError, match="JetStream is required"):
        await ext_bound.start()

    # Service with nc whose jetstream() raises error
    nc_mock = MagicMock()
    nc_mock.jetstream.side_effect = RuntimeError("JetStream not enabled on server")
    service_with_nc = SimpleNamespace(js=None, nc=nc_mock)
    ext_bound_nc = ext.bind(service_with_nc, "kv")
    with pytest.raises(JetStreamUnavailableError, match="Failed to create JetStream"):
        await ext_bound_nc.start()


# ==============================================================================
# 7. Cliffracer Extension Shallow-Copy & Service Binding
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_services_do_not_share_kv_state():
    """Verify that shallow copying during bind preserves isolated per-service state."""

    class SvcA(CliffracerService):
        kv = KvExtension(buckets=["a_bucket"])

    class SvcB(CliffracerService):
        kv = KvExtension(buckets=["b_bucket"])

    cfg_a = ServiceConfig(name="svca", nats_url="nats://localhost:4222", jetstream_enabled=False)
    cfg_b = ServiceConfig(name="svcb", nats_url="nats://localhost:4222", jetstream_enabled=False)

    svc_a = SvcA(config=cfg_a)
    svc_b = SvcB(config=cfg_b)

    # Extension bound as self.kv
    assert hasattr(svc_a, "kv")
    assert hasattr(svc_b, "kv")
    assert svc_a.kv is not svc_b.kv

    await svc_a.container._setup_extensions()
    await svc_b.container._setup_extensions()

    # Ensure stores are distinct dict instances
    assert svc_a.kv._kv_stores is not svc_b.kv._kv_stores
    assert svc_a.kv._obj_stores is not svc_b.kv._obj_stores
    assert "a_bucket" in svc_a.kv._bucket_configs
    assert "b_bucket" in svc_b.kv._bucket_configs
    assert "a_bucket" not in svc_b.kv._bucket_configs


# ==============================================================================
# 8. Live Integration Test against Real JetStream Broker
# ==============================================================================


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_jetstream_kv_with_bucket_ttl():
    """End-to-end integration test with live NATS JetStream verifying bucket TTL and full API."""
    nats_url = os.getenv("CLIFFRACER_TEST_NATS_URL", "nats://127.0.0.1:4222")
    import nats

    try:
        nc = await nats.connect(nats_url, connect_timeout=1)
        js = nc.jetstream()
    except Exception:
        pytest.skip(f"Live NATS server with JetStream not reachable at {nats_url}")

    bucket_name = "test_live_ttl_bucket"
    obj_store_name = "test_live_ttl_obj"

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

        assert await ext.delete(bucket_name, "config.app") is True
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
