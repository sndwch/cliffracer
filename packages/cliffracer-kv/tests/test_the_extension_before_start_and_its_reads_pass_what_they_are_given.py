"""The KV extension before `start()`, the context it uses, and what its reads pass on.

An extension never set up stops cleanly and reports itself unconnected. Opening a bucket or a store
without `start()` caches the context the health reads. The `js` property returns the context in use;
a service connection that makes no context is refused. Revision 1 is a revision. `keys()` lists a key
once and `history()` every revision with its value. `watchall` and `list_objects` pass their
defaults on as declared.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_kv import BucketConfigError, JetStreamUnavailableError, KvExtension

pytestmark = pytest.mark.unit


def _js(connected=True):
    js = AsyncMock()
    js._nc = SimpleNamespace(is_connected=connected)
    return js


def test_an_extension_never_set_up_stops_and_reports_health():
    ext = KvExtension(buckets=["cache"], object_stores=["blobs"])

    asyncio.run(ext.stop())

    assert ext.health_details() == {"connected": False, "buckets": [], "object_stores": []}


def test_arguments_the_constructor_refuses_raise_rather_than_make_nothing():
    with pytest.raises(TypeError):
        KvExtension(no_such_option=1)


@pytest.mark.parametrize("open_", ["get_bucket", "get_object_store"])
def test_opening_without_start_caches_the_context_the_health_reads(open_):
    ext = KvExtension(js=_js())

    asyncio.run(getattr(ext, open_)("b"))

    assert ext.health_details()["connected"] is True


def test_the_context_property_returns_the_context_in_use():
    js = _js()
    ext = KvExtension(js=js)

    assert ext.js is js


def test_the_cached_context_wins_over_the_one_declared():
    declared, cached = _js(), _js()
    ext = KvExtension(js=declared)
    ext._js = cached

    assert ext.js is cached


def test_a_service_connection_that_makes_no_context_is_refused():
    ext = KvExtension()
    ext.service = SimpleNamespace(js=None, nc=SimpleNamespace(jetstream=lambda: None))

    with pytest.raises(JetStreamUnavailableError):
        _ = ext.js


def test_a_client_that_does_not_say_whether_it_is_connected_counts_as_connected():
    ext = KvExtension(nc=SimpleNamespace(), js=SimpleNamespace())
    ext._js = ext._explicit_js

    assert ext.health_details()["connected"] is True


def test_a_bucket_declared_twice_differently_is_named_as_a_bucket():
    ext = KvExtension(buckets=[{"name": "a", "history": 2}, {"name": "a", "history": 3}])

    with pytest.raises(BucketConfigError, match=r"^Bucket 'a' is declared twice"):
        ext._ensure_initialized()


class _Bucket:
    """A bucket whose reads succeed: get and delete record what they were given."""

    def __init__(self):
        self.calls = []

    async def get(self, key, revision=None):
        self.calls.append(("get", revision))
        return SimpleNamespace(value=b'"v"', operation=None, revision=revision)

    async def delete(self, key, last=None):
        self.calls.append(("delete", last))


def _with(bucket):
    ext = KvExtension(js=_js())
    ext.get_bucket = AsyncMock(return_value=bucket)
    return ext


def test_revision_one_is_a_revision_for_get_and_delete():
    bucket = _Bucket()
    ext = _with(bucket)

    asyncio.run(ext.get("b", "k", revision=1))
    asyncio.run(ext.delete("b", "k", last=1))

    assert bucket.calls == [("get", 1), ("delete", 1)]


class _Watcher:
    def __init__(self, entries):
        self.entries = list(entries)

    async def updates(self, timeout=None):
        return self.entries.pop(0) if self.entries else None

    async def stop(self):
        pass


def _entry(key, revision, value, delta):
    return SimpleNamespace(key=key, revision=revision, value=value, delta=delta, operation=None)


class _History:
    """A bucket holding two revisions of key `a`: a watch sees both only with include_history,
    and their values only without meta_only."""

    _js = SimpleNamespace(_timeout=1)

    async def watch(self, keys, include_history=False, meta_only=False):
        revisions = [(1, b"one"), (2, b"two")] if include_history else [(2, b"two")]
        return _Watcher(
            _entry("a", rev, None if meta_only else value, len(revisions) - 1 - i)
            for i, (rev, value) in enumerate(revisions)
        )


def test_keys_lists_each_key_once():
    assert asyncio.run(_with(_History()).keys("b")) == ["a"]


def test_history_returns_every_revision_with_its_value():
    entries = asyncio.run(_with(_History()).history("b", "a"))

    assert [(e.revision, e.value) for e in entries] == [(1, b"one"), (2, b"two")]


class _LostStream:
    """A bucket whose watch ends at once and whose stream is gone."""

    _stream = "KV_b"
    _pre = "$KV.b."
    _direct = False

    def __init__(self):
        async def missing(*args, **kwargs):
            raise nats.js.errors.NotFoundError

        self._js = SimpleNamespace(_timeout=1, get_msg=missing, stream_info=missing)

    async def watch(self, keys, include_history=False, meta_only=False):
        return _Watcher([])


def test_a_history_read_of_a_bucket_whose_stream_is_gone_raises():
    with pytest.raises(nats.js.errors.NotFoundError):
        asyncio.run(_with(_LostStream()).history("b", "a"))


def test_watchall_forwards_its_defaults_to_watch():
    ext = KvExtension(js=_js())
    seen = {}

    def watch(bucket, key, **kwargs):
        seen.update(kwargs)
        return "watching"

    ext.watch = watch

    ext.watchall("b")

    assert (seen["include_history"], seen["ignore_deletes"], seen["meta_only"]) == (
        False,
        False,
        False,
    )


def test_list_objects_leaves_out_deleted_objects_by_default():
    store = SimpleNamespace(list=AsyncMock(return_value=[]))
    ext = KvExtension(js=_js())
    ext.get_object_store = AsyncMock(return_value=store)

    asyncio.run(ext.list_objects("b"))

    assert store.list.await_args.kwargs.get("ignore_deletes", False) is False


class _MarkerBucket:
    """A bucket that keeps `history` revisions and markers for 10 s, on a server that has them."""

    def __init__(self, history):
        self.history = history
        self.created = []
        from nats.aio.client import ServerVersion

        self._js = SimpleNamespace(
            _nc=SimpleNamespace(connected_server_version=ServerVersion("2.11.2"))
        )

    async def status(self):
        return SimpleNamespace(
            stream_info=SimpleNamespace(config=SimpleNamespace(allow_msg_ttl=True)),
            marker_ttl=10,
            history=self.history,
        )

    async def create(self, key, value, msg_ttl=None):
        self.created.append(msg_ttl)
        return 1


def test_a_ttl_below_the_marker_retention_is_refused_on_a_bucket_keeping_two_revisions():
    bucket = _MarkerBucket(history=2)

    with pytest.raises(BucketConfigError, match="at least the marker retention"):
        asyncio.run(_with(bucket).create("b", "k", "v", ttl=5))

    assert bucket.created == []


def test_CONTROL_the_same_ttl_is_taken_on_a_bucket_keeping_one():
    bucket = _MarkerBucket(history=1)

    asyncio.run(_with(bucket).create("b", "k", "v", ttl=5))

    assert bucket.created == [5.0]
