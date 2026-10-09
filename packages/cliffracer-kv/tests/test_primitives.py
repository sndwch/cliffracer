"""Native KV operations preserve their wire conditions and resource lifetime."""

import asyncio
import io
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import BucketConfig, BucketConfigError, KvExtension
from nats.js.api import Header, PubAck, RawStreamMsg, StreamConfig, StreamInfo, StreamState
from nats.js.kv import KeyValue
from pydantic import BaseModel

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext

pytestmark = pytest.mark.unit


class Customer(BaseModel):
    name: str


def stream_info(*, messages=1, allow_msg_ttl=True, marker_ttl=None):
    return StreamInfo(
        config=StreamConfig(
            name="KV_profiles",
            allow_msg_ttl=allow_msg_ttl,
            subject_delete_marker_ttl=marker_ttl,
            max_msgs_per_subject=5,
        ),
        state=StreamState(
            messages=messages, bytes=16, first_seq=1, last_seq=messages, consumer_count=0
        ),
    )


@pytest.fixture
def native_kv():
    """The real nats-py KV client over a recording JetStream boundary."""
    js = AsyncMock()
    kv = KeyValue(name="profiles", stream="KV_profiles", pre="$KV.profiles.", js=js, direct=False)
    js.key_value.return_value = kv
    js.publish.return_value = PubAck(stream="KV_profiles", seq=17)
    js.stream_info.return_value = stream_info()
    return KvExtension(js=js), js


@pytest.mark.asyncio
async def test_create_serializes_the_model_and_sends_the_absence_condition(native_kv):
    ext, js = native_kv
    assert await ext.create("profiles", "user.alice", Customer(name="Alice")) == 17
    call = js.publish.await_args
    assert call.args[0] == "$KV.profiles.user.alice"
    assert json.loads(call.args[1]) == {"name": "Alice"}
    assert call.kwargs.get("headers", {}).get(Header.EXPECTED_LAST_SUBJECT_SEQUENCE) == "0"
    assert call.kwargs["msg_ttl"] is None
    js.stream_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_preserves_the_native_conflict(native_kv):
    ext, js = native_kv
    js.publish.side_effect = nats.js.errors.APIError(code=400, err_code=10071)
    js.get_msg.return_value = RawStreamMsg(
        subject="$KV.profiles.user.alice", seq=4, data=b'"Alice"'
    )
    with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
        await ext.create("profiles", "user.alice", "Bob")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "purge"])
async def test_ttl_reaches_the_native_publish(native_kv, operation):
    ext, js = native_kv
    args = (
        ("profiles", "user.alice", "Alice") if operation == "create" else ("profiles", "user.alice")
    )
    await getattr(ext, operation)(*args, ttl=timedelta(seconds=3))
    assert js.publish.await_args.kwargs["msg_ttl"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "purge"])
@pytest.mark.parametrize("accepted", [False, None])
async def test_unsupported_ttl_is_refused_before_writing(native_kv, operation, accepted):
    ext, js = native_kv
    js.stream_info.return_value = stream_info(allow_msg_ttl=accepted)
    args = (
        ("profiles", "user.alice", "Alice") if operation == "create" else ("profiles", "user.alice")
    )
    with pytest.raises(BucketConfigError, match=r"^Per-key TTL requires NATS Server 2\.11\+"):
        await getattr(ext, operation)(*args, ttl=1)
    js.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [True, "2", 0, -1, 0.5, 1.5, float("nan"), float("inf")])
async def test_invalid_ttl_is_not_silently_truncated(native_kv, ttl):
    ext, js = native_kv
    with pytest.raises(BucketConfigError, match=r"^Message TTL must be a whole positive"):
        await ext.create("profiles", "user.alice", "Alice", ttl=ttl)
    js.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_ttl_status_failure_reaches_the_caller(native_kv):
    ext, js = native_kv
    js.stream_info.side_effect = nats.js.errors.ServiceUnavailableError()
    with pytest.raises(nats.js.errors.ServiceUnavailableError):
        await ext.create("profiles", "user.alice", "Alice", ttl=2)
    js.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_historical_read_returns_the_requested_revision(native_kv):
    ext, js = native_kv

    async def read(stream, *, subject=None, seq=None, direct=False):
        return RawStreamMsg(
            subject="$KV.profiles.user.alice",
            seq=seq or 9,
            data=b'{"name":"old"}' if seq == 2 else b'{"name":"current"}',
        )

    js.get_msg.side_effect = read
    assert await ext.get("profiles", "user.alice", as_type=Customer, revision=2) == Customer(
        name="old"
    )
    assert await ext.get("profiles", "user.alice", as_type=Customer) == Customer(name="current")
    js.get_msg.return_value = None
    js.get_msg.side_effect = nats.js.errors.NotFoundError()
    assert await ext.get("profiles", "user.alice", revision=100, default="absent") == "absent"


@pytest.mark.asyncio
async def test_a_revision_for_another_key_does_not_return_its_value(native_kv):
    ext, js = native_kv
    js.get_msg.return_value = RawStreamMsg(subject="$KV.profiles.user.bob", seq=2, data=b'"other"')
    assert await ext.get("profiles", "user.alice", revision=2) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, -1, True, "2", 1.5])
async def test_invalid_revision_cannot_become_a_current_read(native_kv, revision):
    ext, js = native_kv
    with pytest.raises(ValueError, match=r"^Revision must be a positive integer$"):
        await ext.get("profiles", "user.alice", revision=revision)
    js.get_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_reads_the_broker_again(native_kv):
    ext, js = native_kv
    js.stream_info.side_effect = [stream_info(messages=1), stream_info(messages=7)]
    assert (await ext.status("profiles")).values == 1
    assert (await ext.status("profiles")).values == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["normal", "error", "cancel"])
async def test_watch_releases_its_subscription_on_every_exit(exit_kind):
    js = AsyncMock()
    kv = AsyncMock(spec=KeyValue)
    watcher = KeyValue.KeyWatcher(js)
    watcher._sub = AsyncMock()
    kv.watch.return_value = watcher
    js.key_value.return_value = kv
    ext = KvExtension(js=js)

    async def consume():
        async with ext.watch("profiles", "user.*") as updates:
            await updates._updates.put(
                KeyValue.Entry(
                    bucket="profiles",
                    key="user.alice",
                    value=b"Alice",
                    revision=3,
                    delta=0,
                    created=None,
                    operation=None,
                )
            )
            event = await updates.updates()
            assert (event.key, event.value, event.revision) == ("user.alice", b"Alice", 3)
            if exit_kind == "error":
                raise ValueError("consumer failed")
            if exit_kind == "cancel":
                asyncio.current_task().cancel()
                await asyncio.Event().wait()

    task = asyncio.create_task(consume())
    if exit_kind == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    elif exit_kind == "error":
        with pytest.raises(ValueError, match=r"^consumer failed$"):
            await task
    else:
        await task
    watcher._sub.unsubscribe.assert_awaited_once()
    kv.watch.assert_awaited_once_with(
        "user.*",
        include_history=False,
        ignore_deletes=False,
        meta_only=False,
        inactive_threshold=None,
    )


@pytest.mark.asyncio
async def test_watchall_forwards_snapshot_and_filter_options():
    js = AsyncMock()
    ext = KvExtension(js=js)
    async with ext.watchall(
        "profiles", include_history=True, ignore_deletes=True, meta_only=True, inactive_threshold=12
    ):
        pass
    js.key_value.return_value.watch.assert_awaited_once_with(
        ">",
        include_history=True,
        ignore_deletes=True,
        meta_only=True,
        inactive_threshold=12,
    )


@pytest.mark.asyncio
async def test_runtime_bucket_defaults_reach_the_broker_under_the_service_prefix():
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    ext = KvExtension(js=js)
    config = ServiceConfig(name="customers", subject_prefix="isolated")
    await ext.setup(ExtensionSetupContext(config, config.nats_url, None))
    await ext.get_bucket(
        "sessions", default_config=BucketConfig(name="sessions", ttl=300, history=5)
    )
    await ext.get_bucket("sessions")
    js.create_key_value.assert_awaited_once_with(bucket="isolated_sessions", ttl=300.0, history=5)


@pytest.mark.asyncio
async def test_explicit_declaration_takes_precedence_over_runtime_defaults():
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    ext = KvExtension(buckets=[BucketConfig(name="sessions", ttl=900)], js=js)
    await ext.get_bucket("sessions", default_config=BucketConfig(name="sessions", ttl=300))
    js.create_key_value.assert_awaited_once_with(bucket="sessions", ttl=900.0)
    with pytest.raises(BucketConfigError, match=r"^Default bucket configuration must name"):
        await ext.get_bucket("sessions", default_config=BucketConfig(name="wrong"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [Customer(name="Alice"), {"name": "Alice"}, [1, 2], None, True, 3]
)
async def test_object_values_use_the_kv_serialization(value):
    js = AsyncMock()
    ext = KvExtension(js=js)
    await ext.put_object("objects", "item", value)
    payload = js.object_store.return_value.put.await_args.args[1]
    assert isinstance(payload, bytes)
    assert json.loads(payload) == (value.model_dump() if isinstance(value, BaseModel) else value)


@pytest.mark.asyncio
async def test_object_file_streams_still_reach_the_client_unchanged():
    js = AsyncMock()
    ext = KvExtension(js=js)
    stream = io.BytesIO(b"a large object")
    await ext.put_object("objects", "item", stream)
    forwarded = js.object_store.return_value.put.await_args.args[1]
    assert forwarded.read() == b"a large object"
