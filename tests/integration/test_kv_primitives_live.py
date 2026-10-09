"""KV reservations, revisions, watches and expiry against the configured broker.

A watch's initial data is read by the contract `KvExtension.watch` documents: nats-py's end marker,
`None`, can be queued before the entries as well as after them, and the snapshot's last entry is
the one whose `delta` is 0. A loop that stops at the first `None` reads a snapshot as empty when the
marker comes first, which is the state `tests/fixtures/kv_lagging_watch.py` forces.
"""

import asyncio
import json
import uuid

import nats
import nats.js.errors
import pytest
import pytest_asyncio
from cliffracer_kv import BucketConfig, BucketConfigError, KvExtension
from nats.js.kv import KV_DEL, KV_MARKER_REASON, KV_OP, KV_PURGE

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext
from cliffracer.core.jetstream import all_streams
from tests.conftest import broker_url
from tests.fixtures.kv_lagging_watch import lagging_watches

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def _initial(watcher, kv=None, keys: str = ">", timeout: float = 3) -> list:
    """The watch's initial entries, with its end marker consumed, whichever order they arrive in.

    The entries end at the one whose `delta` is 0, or at a marker queued after an entry, which
    nats-py does once a message it skipped (a deletion, with `ignore_deletes`) left nothing
    pending. A marker before any entry may precede them, so it does not end the read.

    A watch that skips deletions, whose marker came first, can end on a deletion it never delivers:
    then no entry has `delta` 0 and no marker follows. Its caller passes the bucket `kv` and the
    watch's `keys`, and an entry whose `delta` counts only messages the watch skips ends the read.

    Every wait is bounded, so entries that never arrive raise `nats.errors.TimeoutError` rather
    than reading as none; an empty snapshot is not read here.
    """
    entries: list = []
    marked = ended = False
    while not (marked and ended):
        item = await watcher.updates(timeout=timeout)
        if item is None:
            marked = True
            ended = ended or bool(entries)
        else:
            entries.append(item)
            ended = item.delta == 0 or (
                marked and kv is not None and await _only_skipped_messages_follow(kv, keys, item)
            )
    return entries


async def _only_skipped_messages_follow(kv, keys: str, entry) -> bool:
    """Whether the `entry.delta` messages after `entry` on the watch's subjects are all ones a
    delete-skipping watch does not deliver: a deletion, a purge, or a marker the server placed."""
    after = entry.revision
    for _ in range(entry.delta):
        msg = await kv._js.get_msg(kv._stream, seq=after + 1, subject=f"{kv._pre}{keys}", next=True)
        headers = msg.headers or {}
        if headers.get(KV_OP) not in (KV_DEL, KV_PURGE) and KV_MARKER_REASON not in headers:
            return False
        after = msg.seq
    return True


def _replay(entries) -> list[tuple[str, int, object]]:
    return [(e.key, e.revision, json.loads(e.value)) for e in entries]


@pytest_asyncio.fixture
async def live_kv():
    marker = uuid.uuid4().hex
    bucket = f"primitives_{marker}"
    connections = []
    extensions = []
    owned = set()
    try:
        for _ in range(2):
            connections.append(await nats.connect(broker_url()))
        for nc in connections:
            ext = KvExtension(
                buckets=[BucketConfig(name=bucket, history=5)],
                js=nc.jetstream(),
            )
            config = ServiceConfig(name="kv_probe")
            await ext.setup(ExtensionSetupContext(config, config.nats_url, None))
            await ext.start()
            extensions.append(ext)
            owned.add((await ext.status(bucket)).stream_info.config.name)
        yield extensions[0], extensions[1], bucket
    finally:
        try:
            for ext in extensions:
                await ext.stop()
            if connections:
                js = connections[0].jetstream()
                for name in owned:
                    await js.delete_stream(name)
            if len(connections) == 2:
                leftovers = [
                    info.config.name
                    for info in await all_streams(connections[1].jetstream())
                    if info.config.name.split("_")[-1] == marker
                ]
                assert leftovers == [], f"KV test left broker streams behind: {leftovers}"
        finally:
            for nc in connections:
                await nc.close()


@pytest.mark.asyncio
async def test_competing_stock_reservations_have_one_winner_and_cancellation_allows_rebooking(
    live_kv,
):
    first, second, bucket = live_kv
    results = await asyncio.gather(
        first.create(bucket, "stock.sku1", {"order": "order-a"}),
        second.create(bucket, "stock.sku1", {"order": "order-b"}),
        return_exceptions=True,
    )
    winners = [i for i, result in enumerate(results) if isinstance(result, int)]
    losers = [
        result for result in results if isinstance(result, nats.js.errors.KeyWrongLastSequenceError)
    ]
    assert len(winners) == 1 and len(losers) == 1, results
    assert await first.get(bucket, "stock.sku1") == {"order": ("order-a", "order-b")[winners[0]]}
    revision = results[winners[0]]
    await second.delete(bucket, "stock.sku1", last=revision)
    replacement = await second.create(bucket, "stock.sku1", {"order": "order-c"})
    assert replacement > revision
    assert await first.get(bucket, "stock.sku1") == {"order": "order-c"}


@pytest.mark.asyncio
async def test_stale_inventory_versions_cannot_update_or_delete_current_stock(live_kv):
    first, second, bucket = live_kv
    old = await first.create(bucket, "inventory.sku1", {"quantity": 10})
    current = await second.put(bucket, "inventory.sku1", {"quantity": 9}, revision=old)
    with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
        await first.put(bucket, "inventory.sku1", {"quantity": 11}, revision=old)
    with pytest.raises(nats.js.errors.BadRequestError) as rejected_delete:
        await first.delete(bucket, "inventory.sku1", last=old)
    assert rejected_delete.value.err_code == 10071
    assert await first.get(bucket, "inventory.sku1") == {"quantity": 9}
    assert await first.get(bucket, "inventory.sku1", revision=old) == {"quantity": 10}
    assert (await first.status(bucket)).values == 2
    assert current > old


@pytest.mark.asyncio
async def test_watch_snapshot_live_changes_and_reopen_preserve_native_entries(live_kv):
    first, second, bucket = live_kv
    initial = await first.create(bucket, "user.alice", {"plan": "basic"})
    await first.create(bucket, "product.sku1", {"price": 25})
    async with first.watch(bucket, "user.*") as watcher:
        assert _replay(await _initial(watcher)) == [("user.alice", initial, {"plan": "basic"})]
        update = await second.put(bucket, "user.alice", {"plan": "premium"})
        event = await watcher.updates(timeout=3)
        assert (event.revision, json.loads(event.value)) == (update, {"plan": "premium"})
        await second.delete(bucket, "user.alice", last=update)
        deleted = await watcher.updates(timeout=3)
        assert (deleted.key, deleted.operation) == ("user.alice", "DEL")
    while_away = await second.create(bucket, "user.bob", {"plan": "business"})
    async with first.watchall(bucket, include_history=True, ignore_deletes=True) as watcher:
        replay = _replay(await _initial(watcher, await first.get_bucket(bucket)))
        assert ("user.alice", initial, {"plan": "basic"}) in replay
        assert ("user.alice", update, {"plan": "premium"}) in replay
        assert ("user.bob", while_away, {"plan": "business"}) in replay
        assert all(
            key != "user.alice" or revision in (initial, update) for key, revision, _ in replay
        )


async def _history_with_a_deletion(ext, bucket) -> list[tuple[str, int, object]]:
    """The writes the watch test makes before its replay, and the replay it expects."""
    initial = await ext.create(bucket, "user.alice", {"plan": "basic"})
    await ext.create(bucket, "product.sku1", {"price": 25})
    update = await ext.put(bucket, "user.alice", {"plan": "premium"})
    await ext.delete(bucket, "user.alice", last=update)
    bob = await ext.create(bucket, "user.bob", {"plan": "business"})
    return [
        ("user.alice", initial, {"plan": "basic"}),
        ("product.sku1", initial + 1, {"price": 25}),
        ("user.alice", update, {"plan": "premium"}),
        ("user.bob", bob, {"plan": "business"}),
    ]


@pytest.mark.asyncio
async def test_the_replay_is_whole_when_the_end_marker_is_queued_first(live_kv):
    first, _, bucket = live_kv
    expected = await _history_with_a_deletion(first, bucket)

    kv = await first.get_bucket(bucket)
    with lagging_watches(kv) as made:
        async with first.watchall(bucket, include_history=True, ignore_deletes=True) as watcher:
            replay = _replay(await _initial(watcher, kv))

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
    assert replay == expected


async def _history_ending_on_a_deletion(ext, bucket) -> list[tuple[str, int, object]]:
    """A history whose last message is a deletion, and the replay a delete-skipping watch reads."""
    alice = await ext.create(bucket, "user.alice", {"plan": "basic"})
    bob = await ext.create(bucket, "user.bob", {"plan": "business"})
    await ext.delete(bucket, "user.alice", last=alice)
    return [("user.alice", alice, {"plan": "basic"}), ("user.bob", bob, {"plan": "business"})]


@pytest.mark.asyncio
async def test_a_replay_ending_on_a_skipped_deletion_ends_at_the_marker_after_it(live_kv):
    """Unforced, nats-py queues its marker when it skips the deletion that leaves nothing pending,
    after the entries; no entry has `delta` 0, so that marker is what ends the read. The bucket is
    passed as every delete-skipping read passes it, since under load the marker can come first."""
    first, _, bucket = live_kv
    expected = await _history_ending_on_a_deletion(first, bucket)

    async with first.watchall(bucket, include_history=True, ignore_deletes=True) as watcher:
        replay = _replay(await _initial(watcher, await first.get_bucket(bucket)))

    assert replay == expected


@pytest.mark.asyncio
async def test_a_replay_ending_on_a_skipped_deletion_is_whole_when_the_marker_is_queued_first(
    live_kv,
):
    """Forced first, the marker cannot end the read and no entry has `delta` 0: the stream says
    only the deletion is left."""
    first, _, bucket = live_kv
    expected = await _history_ending_on_a_deletion(first, bucket)

    kv = await first.get_bucket(bucket)
    with lagging_watches(kv) as made:
        async with first.watchall(bucket, include_history=True, ignore_deletes=True) as watcher:
            replay = _replay(await _initial(watcher, kv))

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
    assert replay == expected


@pytest.mark.asyncio
async def test_CONTROL_the_forced_marker_empties_a_replay_that_stops_at_the_first_none(live_kv):
    """The interleaving is real and is what a loop that stops at `None` reads as no history."""
    first, _, bucket = live_kv
    await _history_with_a_deletion(first, bucket)

    with lagging_watches(await first.get_bucket(bucket)) as made:
        async with first.watchall(bucket, include_history=True, ignore_deletes=True) as watcher:
            replay = []
            while (event := await watcher.updates(timeout=3)) is not None:
                replay.append(event)

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
    assert replay == []


@pytest.mark.asyncio
async def test_per_key_ttl_expires_only_its_key_or_is_refused_before_writing(live_kv):
    first, second, bucket = live_kv
    await first.create(bucket, "product.sku1", {"price": 25})
    status = await first.status(bucket)
    if status.stream_info.config.allow_msg_ttl is not True:
        with pytest.raises(BucketConfigError, match=r"^Per-key TTL requires"):
            await first.create(bucket, "offer.sku1", {"discount": 10}, ttl=1)
        assert await second.get(bucket, "offer.sku1") is None
    else:
        await first.create(bucket, "offer.sku1", {"discount": 10}, ttl=1)
        assert await second.get(bucket, "offer.sku1") == {"discount": 10}
        async with asyncio.timeout(6):
            while await second.get(bucket, "offer.sku1") is not None:
                await asyncio.sleep(0.05)
        assert await second.create(bucket, "offer.sku1", {"discount": 5}) > 0
    assert await second.get(bucket, "product.sku1") == {"price": 25}


@pytest.mark.asyncio
async def test_purge_marker_ttl_expires_or_is_refused_without_removing_the_key(live_kv):
    first, second, bucket = live_kv
    await first.create(bucket, "product.sku1", {"price": 25})
    await first.put(bucket, "product.sku1", {"price": 30})
    status = await first.status(bucket)
    if status.stream_info.config.allow_msg_ttl is not True:
        with pytest.raises(BucketConfigError, match=r"^Per-key TTL requires"):
            await first.purge(bucket, "product.sku1", ttl=1)
        assert await second.get(bucket, "product.sku1") == {"price": 30}
    else:
        await first.purge(bucket, "product.sku1", ttl=1)
        assert await second.get(bucket, "product.sku1") is None
        assert (await second.status(bucket)).values == 1
        async with asyncio.timeout(6):
            while (await second.status(bucket)).values:
                await asyncio.sleep(0.05)
        assert await second.create(bucket, "product.sku1", {"discount": 5}) > 0
