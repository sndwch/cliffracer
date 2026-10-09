"""Expired offers are absent to readers and available to one new creator."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import BucketConfig, BucketConfigError, KvExtension
from nats.aio.client import ServerVersion
from nats.js.api import Header, PubAck, RawStreamMsg, StreamConfig, StreamInfo, StreamState
from nats.js.kv import KeyValue

pytestmark = pytest.mark.unit


@pytest.fixture
def offers():
    js = AsyncMock()
    js._nc = SimpleNamespace(connected_server_version=ServerVersion("2.11.2"))
    kv = KeyValue(name="offers", stream="KV_offers", pre="$KV.offers.", js=js, direct=True)
    js.key_value.return_value = kv
    js.create_key_value.return_value = kv
    js.publish.return_value = PubAck(stream="KV_offers", seq=9)
    js.stream_info.return_value = StreamInfo(
        config=StreamConfig(
            name="KV_offers",
            allow_msg_ttl=True,
            subject_delete_marker_ttl=2,
            max_msgs_per_subject=5,
        ),
        state=StreamState(messages=1, bytes=16, first_seq=8, last_seq=8, consumer_count=0),
    )
    js.get_msg.return_value = RawStreamMsg(
        subject="$KV.offers.offer.sku1",
        seq=8,
        data=None,
        headers={"Nats-Marker-Reason": "MaxAge"},
    )
    return KvExtension(js=js), js, kv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,operation", [("MaxAge", "PURGE"), ("Purge", "PURGE"), ("Remove", "DEL")]
)
async def test_an_expired_offer_is_missing_to_native_and_decoded_reads(offers, reason, operation):
    ext, js, _ = offers
    js.get_msg.return_value.headers = {"Nats-Marker-Reason": reason}
    bucket = await ext.get_bucket("offers")
    with pytest.raises(nats.js.errors.KeyNotFoundError) as missing:
        await bucket.get("offer.sku1")
    assert missing.value.entry.revision == 8
    assert missing.value.op == operation
    assert await ext.get("offers", "offer.sku1", default="expired") == "expired"
    assert await ext.get("offers", "offer.sku1", revision=8, default="expired") == "expired"


@pytest.mark.asyncio
async def test_recreating_an_expired_offer_checks_the_marker_revision_and_keeps_its_ttl(offers):
    ext, js, _ = offers
    js.publish.side_effect = [
        nats.js.errors.BadRequestError(code=400, err_code=10071),
        PubAck(stream="KV_offers", seq=9),
    ]
    assert await ext.create("offers", "offer.sku1", {"discount": 15}, ttl=3) == 9
    assert [
        call.kwargs["headers"][Header.EXPECTED_LAST_SUBJECT_SEQUENCE]
        for call in js.publish.await_args_list
    ] == ["0", "8"]
    assert all(call.kwargs["msg_ttl"] == 3 for call in js.publish.await_args_list)


@pytest.mark.asyncio
async def test_a_competing_offer_written_after_the_marker_read_is_not_overwritten(offers):
    ext, js, _ = offers
    js.publish.side_effect = nats.js.errors.BadRequestError(code=400, err_code=10071)
    with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
        await ext.create("offers", "offer.sku1", {"discount": 15})
    assert js.publish.await_count == 2
    assert js.publish.await_args.kwargs["headers"][Header.EXPECTED_LAST_SUBJECT_SEQUENCE] == "8"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b'{"discount":10}'])
async def test_a_live_offer_including_an_empty_value_still_refuses_recreation(offers, payload):
    ext, js, _ = offers
    js.get_msg.return_value.headers = {}
    js.get_msg.return_value.data = payload
    js.publish.side_effect = nats.js.errors.BadRequestError(code=400, err_code=10071)
    assert (await (await ext.get_bucket("offers")).get("offer.sku1")).value == payload
    with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
        await ext.create("offers", "offer.sku1", {"discount": 15})
    js.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_marker_translation_does_not_change_the_shared_client_or_raw_response(offers):
    ext, js, original = offers
    raw = js.get_msg.return_value
    await ext.get("offers", "offer.sku1")
    assert raw.headers == {"Nats-Marker-Reason": "MaxAge"}
    assert (await original.get("offer.sku1")).revision == 8
    assert original._js is js


@pytest.mark.asyncio
async def test_an_unrecognized_marker_does_not_grant_permission_to_replace_an_offer(offers):
    ext, js, _ = offers
    js.get_msg.return_value.headers = {"Nats-Marker-Reason": "FutureReason"}
    js.publish.side_effect = nats.js.errors.BadRequestError(code=400, err_code=10071)
    with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
        await ext.create("offers", "offer.sku1", {"discount": 15})
    js.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_kv_operation_takes_precedence_over_an_expiry_reason(offers):
    ext, js, _ = offers
    js.get_msg.return_value.headers = {"KV-Operation": "DEL", "Nats-Marker-Reason": "MaxAge"}
    with pytest.raises(nats.js.errors.KeyNotFoundError) as missing:
        await (await ext.get_bucket("offers")).get("offer.sku1")
    assert missing.value.op == "DEL"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "purge"])
async def test_offer_expiry_is_not_silently_extended_by_marker_retention(offers, operation):
    ext, js, _ = offers
    args = (
        ("offers", "offer.sku1", {"discount": 10})
        if operation == "create"
        else ("offers", "offer.sku1")
    )
    with pytest.raises(BucketConfigError, match="marker retention"):
        await getattr(ext, operation)(*args, ttl=1)
    js.publish.assert_not_awaited()
    await getattr(ext, operation)(*args, ttl=2)
    assert js.publish.await_args.kwargs["msg_ttl"] == 2


@pytest.mark.asyncio
async def test_one_revision_allows_a_shorter_offer_ttl_than_marker_retention(offers):
    ext, js, _ = offers
    js.stream_info.return_value.config.max_msgs_per_subject = 1
    await ext.create("offers", "offer.sku1", {"discount": 10}, ttl=1)
    assert js.publish.await_args.kwargs["msg_ttl"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2.10.29", "2.11.0", "2.11.1", "2.11.2-rc.1"])
async def test_marker_configuration_is_refused_before_provisioning_on_an_older_broker(
    offers, version
):
    ext, js, _ = offers
    js._nc.connected_server_version = ServerVersion(version)
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    with pytest.raises(BucketConfigError, match=r"2\.11\.2"):
        await ext.get_bucket(
            "offers", default_config=BucketConfig(name="offers", limit_marker_ttl=2)
        )
    js.create_key_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_configuration_requires_an_observable_server_version(offers):
    ext, js, _ = offers
    js._nc = None
    with pytest.raises(BucketConfigError, match="Cannot verify"):
        await ext.get_bucket(
            "offers", default_config=BucketConfig(name="offers", limit_marker_ttl=2)
        )
    js.create_key_value.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [True, False])
async def test_declared_marker_retention_is_verified_against_the_broker(offers, existing):
    _, js, _ = offers
    if not existing:
        js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    ext = KvExtension(buckets=[{"name": "offers", "limit_marker_ttl": 2}], js=js)
    await ext.start()
    if not existing:
        assert js.create_key_value.await_args.kwargs["limit_marker_ttl"] == 2
    await ext.stop()
    js.stream_info.return_value.config.subject_delete_marker_ttl = 5
    with pytest.raises(BucketConfigError, match="marker retention"):
        await ext.start()


@pytest.mark.asyncio
async def test_default_bucket_age_preserves_declared_marker_retention(offers):
    _, js, _ = offers
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    ext = KvExtension(
        buckets=[BucketConfig(name="offers", limit_marker_ttl=2)],
        bucket_ttls={"offers": 10},
        js=js,
    )
    await ext.start()
    js.create_key_value.assert_awaited_once_with(bucket="offers", ttl=10.0, limit_marker_ttl=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "purge"])
async def test_existing_marker_buckets_also_refuse_ttl_writes_on_an_older_broker(offers, operation):
    ext, js, _ = offers
    js._nc.connected_server_version = ServerVersion("2.11.0")
    args = (
        ("offers", "offer.sku1", {"discount": 10})
        if operation == "create"
        else ("offers", "offer.sku1")
    )
    with pytest.raises(BucketConfigError, match=r"2\.11\.2"):
        await getattr(ext, operation)(*args, ttl=3)
    js.publish.assert_not_awaited()
    await getattr(ext, operation)(*args)
    js.publish.assert_awaited_once()
