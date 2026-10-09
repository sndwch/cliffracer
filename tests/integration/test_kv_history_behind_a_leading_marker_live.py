"""KV history and keys are complete when the client's count lags the server, against a broker.

nats-py's watch queues its end marker first when the consumer reports nothing
pending and the subscription reports nothing received. Under load both hold
while the entries are sent but still unread. Here that state is forced on one
watch subscription: its `consumer_info()` is read only once the server reports
nothing pending, and its received count reads 0. Nothing else is patched, and
the patch is removed after each read.

The first CONTROL shows the forcing works: nats-py's own `history()` reads the
key as having none. The second shows the key's history without it.
"""

import uuid
from collections.abc import AsyncIterator

import nats
import nats.errors
import nats.js.errors
import pytest
import pytest_asyncio
from cliffracer_kv import BucketConfig, KvExtension
from nats.js.kv import KV_DEL, KV_PURGE

from cliffracer import ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext
from tests.conftest import broker_url
from tests.fixtures.kv_lagging_watch import lagging_watches

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest_asyncio.fixture
async def filled() -> AsyncIterator[tuple[KvExtension, object, str, list[int]]]:
    bucket = f"leading_marker_{uuid.uuid4().hex}"
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    ext = KvExtension(buckets=[BucketConfig(name=bucket, history=5)], js=js)
    stream = None
    try:
        config = ServiceConfig(name="kv_probe")
        await ext.setup(ExtensionSetupContext(config, config.nats_url, None))
        await ext.start()
        # The session prefixes stream names, so the stream is deleted by the
        # name the broker reports rather than one built from the bucket.
        stream = (await ext.status(bucket)).stream_info.config.name
        revisions = [await ext.put(bucket, "user.1", {"n": n}) for n in (1, 2)]
        yield ext, await ext.get_bucket(bucket), bucket, revisions
    finally:
        try:
            await ext.stop()
            if stream is not None:
                await js.delete_stream(stream)
        finally:
            await nc.close()


@pytest.mark.asyncio
async def test_history_is_complete_when_the_marker_is_queued_first(filled):
    ext, kv, bucket, revisions = filled

    with lagging_watches(kv) as made:
        hist = await ext.history(bucket, "user.1")

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
    assert [e.revision for e in hist] == revisions


@pytest.mark.asyncio
async def test_keys_are_complete_when_the_marker_is_queued_first(filled):
    ext, kv, bucket, _ = filled

    with lagging_watches(kv) as made:
        keys = await ext.keys(bucket)

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
    assert keys == ["user.1"]


@pytest.mark.asyncio
async def test_CONTROL_the_forced_interleaving_empties_nats_pys_own_history(filled):
    _, kv, _, _ = filled

    with lagging_watches(kv) as made, pytest.raises(nats.js.errors.NoKeysError):
        await kv.history("user.1")

    assert [m.forced for m in made] == [True], "the interleaving was not forced"


@pytest.mark.asyncio
async def test_CONTROL_without_the_interleaving_the_history_is_complete(filled):
    ext, _, bucket, revisions = filled

    assert [e.revision for e in await ext.history(bucket, "user.1")] == revisions


# --- a whole replay that skips deletions, with the marker queued first ---------------------

#: How long the CONTROL waits for an end that never comes.
NO_END = 2.0


async def _replay_skipping_deletions(ext, bucket: str, *, ignore_deletes: bool):
    """Read a watch as a caller would to know its replay is whole: until the entry whose delta is
    0, keeping the entries that are not deletions. Returns (kept keys, the last entry read)."""
    kept: list[str] = []
    async with ext.watch(bucket, ignore_deletes=ignore_deletes) as watcher:
        while True:
            entry = await watcher.updates(timeout=NO_END)
            if entry is None:  # the end marker, which may come first; it says nothing here
                continue
            if entry.operation not in (KV_DEL, KV_PURGE):
                kept.append(entry.key)
            if entry.delta == 0:
                return kept, entry


@pytest_asyncio.fixture
async def ending_in_a_deletion(filled):
    ext, kv, bucket, _ = filled
    await ext.put(bucket, "user.2", {"n": 3})
    await ext.delete(bucket, "user.2")  # the stream's last message is a deletion
    return ext, kv, bucket


@pytest.mark.asyncio
async def test_a_replay_that_skips_deletions_itself_ends_on_a_deletion_with_the_marker_first(
    ending_in_a_deletion,
):
    ext, kv, bucket = ending_in_a_deletion

    with lagging_watches(kv) as made:
        kept, last = await _replay_skipping_deletions(ext, bucket, ignore_deletes=False)

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
    assert (last.delta, last.operation, last.key) == (0, KV_DEL, "user.2")
    assert kept == ["user.1"]


@pytest.mark.asyncio
async def test_CONTROL_with_ignore_deletes_the_same_replay_has_no_end_to_read(
    ending_in_a_deletion,
):
    ext, kv, bucket = ending_in_a_deletion

    with lagging_watches(kv) as made, pytest.raises(nats.errors.TimeoutError):
        await _replay_skipping_deletions(ext, bucket, ignore_deletes=True)

    assert [m.forced for m in made] == [True], "the interleaving was not forced"
