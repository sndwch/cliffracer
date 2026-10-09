"""Offer expiry notifications and competing stock reservations over real KV."""

import asyncio
import json
import uuid

import nats
import nats.js.errors
import pytest
import pytest_asyncio
from cliffracer_kv import BucketConfig, BucketConfigError, KvExtension

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext
from cliffracer.core.jetstream import all_streams
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest_asyncio.fixture
async def live_offers():
    marker = uuid.uuid4().hex
    bucket = f"offers_{marker}"
    connections = []
    extensions = []
    owned = set()
    try:
        for _ in range(2):
            nc = await nats.connect(broker_url())
            connections.append(nc)
            ext = KvExtension(js=nc.jetstream())
            config = ServiceConfig(name="offers")
            await ext.setup(ExtensionSetupContext(config, config.nats_url, None))
            await ext.start()
            extensions.append(ext)

        async def provision(*, ttl=None, markers=None, history=1):
            handle = await extensions[0].get_bucket(
                bucket,
                default_config=BucketConfig(
                    name=bucket, ttl=ttl, limit_marker_ttl=markers, history=history
                ),
            )
            owned.add((await handle.status()).stream_info.config.name)
            await extensions[1].get_bucket(bucket)
            return handle

        version = connections[0].connected_server_version
        supported = (version.major, version.minor, version.patch) >= (
            2,
            11,
            2,
        ) and not version.prerelease
        yield *extensions, bucket, provision, supported
    finally:
        try:
            for ext in extensions:
                await ext.stop()
            if connections:
                for name in owned:
                    await connections[0].jetstream().delete_stream(name)
            if len(connections) == 2:
                leftovers = [
                    info.config.name
                    for info in await all_streams(connections[1].jetstream())
                    if info.config.name.split("_")[-1] == marker
                ]
                assert leftovers == [], f"Offer tests left broker streams behind: {leftovers}"
        finally:
            for nc in connections:
                await nc.close()


@pytest.mark.asyncio
async def test_a_retained_marker_allows_one_competing_stock_reservation(live_offers, monkeypatch):
    first, second, bucket, provision, _ = live_offers
    handle = await provision()
    # A marker-shaped broker record exercises native reads and CAS independently
    # of server TTL support. Automatic expiry has separate live cases below.
    status = await handle.status()
    subject = status.stream_info.config.subjects[0].removesuffix(">") + "stock.sku1"
    marker = await first.js.publish(subject, b"", headers={"Nats-Marker-Reason": "MaxAge"})
    observed = 0
    both_read = asyncio.Event()

    def synchronized_read(context):
        original = context.get_msg

        async def read(*args, **kwargs):
            nonlocal observed
            message = await original(*args, **kwargs)
            if message.seq == marker.seq:
                observed += 1
                if observed == 2:
                    both_read.set()
                await both_read.wait()
            return message

        monkeypatch.setattr(context, "get_msg", read)

    synchronized_read(first.js)
    synchronized_read(second.js)
    async with asyncio.timeout(5):
        results = await asyncio.gather(
            first.create(bucket, "stock.sku1", {"order": "order-a"}),
            second.create(bucket, "stock.sku1", {"order": "order-b"}),
            return_exceptions=True,
        )
    winners = [index for index, result in enumerate(results) if isinstance(result, int)]
    assert len(winners) == 1, results
    assert observed == 2
    assert (
        sum(isinstance(result, nats.js.errors.KeyWrongLastSequenceError) for result in results) == 1
    )
    assert await first.get(bucket, "stock.sku1") == {"order": ("order-a", "order-b")[winners[0]]}


@pytest.mark.asyncio
async def test_marker_configuration_requires_the_notification_support_floor(live_offers):
    first, _, bucket, provision, supported = live_offers
    if supported:
        await provision(markers=5)
        assert (await first.status(bucket)).marker_ttl == 5
    else:
        with pytest.raises(BucketConfigError, match=r"2\.11\.2"):
            await provision(markers=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", ["bucket", "key"])
async def test_expired_offers_notify_watchers_and_can_be_recreated_during_marker_retention(
    live_offers, expiry
):
    first, second, bucket, provision, supported = live_offers
    if not supported:
        pytest.skip("Automatic expiry notifications require NATS Server 2.11.2+")
    handle = await provision(ttl=1 if expiry == "bucket" else None, markers=5)
    async with second.watch(bucket, "offer.sku1") as watcher:
        assert await watcher.updates(timeout=3) is None
        created = await first.create(
            bucket, "offer.sku1", {"discount": 10}, ttl=1 if expiry == "key" else None
        )
        value = await watcher.updates(timeout=3)
        assert (value.revision, json.loads(value.value)) == (created, {"discount": 10})
        expired = await watcher.updates(timeout=6)
        assert (expired.key, expired.operation, expired.value) == ("offer.sku1", "PURGE", b"")
        with pytest.raises(nats.js.errors.KeyNotFoundError) as missing:
            await handle.get("offer.sku1")
        assert missing.value.entry.revision == expired.revision
        assert await first.get(bucket, "offer.sku1", default="expired") == "expired"
        assert (
            await first.get(bucket, "offer.sku1", revision=expired.revision, default="expired")
            == "expired"
        )
        replacement = await first.create(bucket, "offer.sku1", {"discount": 15})
        assert replacement > expired.revision
        event = await watcher.updates(timeout=3)
        assert (event.revision, json.loads(event.value)) == (replacement, {"discount": 15})


@pytest.mark.asyncio
async def test_offer_history_cannot_silently_extend_the_requested_expiry(live_offers):
    first, second, bucket, provision, supported = live_offers
    if not supported:
        pytest.skip("Automatic expiry notifications require NATS Server 2.11.2+")
    await provision(markers=2, history=5)
    await first.create(bucket, "offer.sku2", {"discount": 5})
    with pytest.raises(BucketConfigError, match="marker retention"):
        await first.create(bucket, "offer.sku1", {"discount": 10}, ttl=1)
    assert await second.get(bucket, "offer.sku1") is None
    async with second.watch(bucket, "offer.sku1") as watcher:
        assert await watcher.updates(timeout=3) is None
        await first.create(bucket, "offer.sku1", {"discount": 10}, ttl=2)
        assert json.loads((await watcher.updates(timeout=3)).value) == {"discount": 10}
        assert (await watcher.updates(timeout=6)).operation == "PURGE"
    assert await second.get(bucket, "offer.sku2") == {"discount": 5}
