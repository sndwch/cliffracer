"""`history()` and `keys()` read a key's entries even when the watch's end marker comes first.

nats-py's `KeyValue.watch` queues its "no more entries" marker, `None`, before
any entry when `consumer_info()` reports nothing pending and the subscription
has received nothing. Under load both are true while the entries are still in
the socket: the server has sent them, the client has not read them. The entries
then queue behind the marker. nats-py's `history()` and `keys()` stop at the
marker and raise `NoKeysError`, which the extension used to turn into `[]`, so a
key with history read as a key without it.

The extension now reads the watcher itself. A snapshot ends at the first entry
whose `delta` -- that message's own count of what is still pending -- is 0. A
marker that arrives before any entry is checked against the stream: no message
means `[]`; otherwise the entries are waited for, each within the JetStream
context's timeout, and a wait that runs out raises rather than reading as empty.

The bucket below is a real `KeyValue` whose `watch` queues entries by nats-py's
own rules (`nats/js/kv.py`, `watch_updates`), with the marker optionally forced
first, so the code under test meets the queue shapes nats-py produces.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.errors
import nats.js.errors
import pytest
from cliffracer_kv import KvExtension
from nats.aio.client import Client
from nats.js.kv import KV_DEL, KV_PURGE, KeyValue

pytestmark = pytest.mark.unit

BUCKET = "users"
UNKNOWN_MARKER = "a marker reason nats-py does not know"
TIMEOUT = 0.1


class _Js:
    """The JetStream context calls the extension makes to check a leading marker."""

    def __init__(self, messages: dict[str, int]) -> None:
        self._timeout = TIMEOUT
        self.messages = messages

    async def get_msg(self, stream, seq=None, subject=None, direct=False, next=False):
        if not self.messages.get(subject):
            raise nats.js.errors.NotFoundError
        return SimpleNamespace(subject=subject, seq=self.messages[subject], data=b"")

    async def stream_info(self, name, subjects_filter=None):
        return SimpleNamespace(state=SimpleNamespace(messages=sum(self.messages.values())))


def _entry(key: str, revision: int, delta: int, op: str | None = None) -> KeyValue.Entry:
    return KeyValue.Entry(
        bucket=BUCKET,
        key=key,
        value=b"v",
        revision=revision,
        delta=delta,
        created=None,
        operation=op,
    )


def _bucket(entries: list[KeyValue.Entry], *, marker_first: bool) -> KeyValue:
    """A bucket whose watch delivers `entries` as nats-py queues them.

    `messages` counts every entry per subject, as the stream would hold them.
    """
    messages: dict[str, int] = {}
    for e in entries:
        messages[f"$KV.{BUCKET}.{e.key}"] = messages.get(f"$KV.{BUCKET}.{e.key}", 0) + 1
    kv = KeyValue(
        name=BUCKET, stream=f"KV_{BUCKET}", pre=f"$KV.{BUCKET}.", js=_Js(messages), direct=False
    )

    async def watch(
        keys,
        headers_only=False,
        include_history=False,
        ignore_deletes=False,
        meta_only=False,
        inactive_threshold=None,
    ):
        watcher = KeyValue.KeyWatcher(kv)
        watcher._sub = AsyncMock()
        kv.watchers.append(watcher)
        pattern = keys.rstrip(">")
        if marker_first:
            watcher._updates.put_nowait(None)
            watcher._init_done = True
        for e in entries:
            if not (e.key == keys or (keys.endswith(">") and e.key.startswith(pattern))):
                continue
            if (
                e.operation == UNKNOWN_MARKER
            ):  # skipped, as a marker with a reason nats-py does not know
                if e.delta == 0 and not watcher._init_done:
                    watcher._updates.put_nowait(None)
                    watcher._init_done = True
                continue
            if ignore_deletes and e.operation in (KV_DEL, KV_PURGE):
                if e.delta == 0 and not watcher._init_done:
                    watcher._updates.put_nowait(None)
                    watcher._init_done = True
                continue
            watcher._updates.put_nowait(e)
            if e.delta == 0 and not watcher._init_done:
                watcher._updates.put_nowait(None)
                watcher._init_done = True
        return watcher

    kv.watchers = []  # type: ignore[attr-defined]
    kv.watch = watch  # type: ignore[method-assign]
    return kv


async def _call(kv: KeyValue, method: str, *args, **kwargs):
    ext = KvExtension(js=AsyncMock())
    ext._kv_stores = {BUCKET: kv}
    ext._ensure_initialized = lambda: None  # type: ignore[method-assign]
    return await asyncio.wait_for(getattr(ext, method)(BUCKET, *args, **kwargs), timeout=2.0)


HISTORY = [_entry("user.1", 1, 1), _entry("user.1", 2, 0)]
KEYS = [_entry("user.1", 1, 1), _entry("user.2", 2, 0)]


async def test_history_reads_the_entries_behind_a_leading_marker():
    hist = await _call(_bucket(HISTORY, marker_first=True), "history", "user.1")

    assert [e.revision for e in hist] == [1, 2]


async def test_keys_reads_the_keys_behind_a_leading_marker():
    keys = await _call(_bucket(KEYS, marker_first=True), "keys")

    assert keys == ["user.1", "user.2"]


async def test_a_leading_marker_for_a_key_with_messages_and_no_entries_raises():
    """Entries the stream holds but the watch never delivers are not 'no history'."""
    kv = _bucket([], marker_first=True)  # the watch delivers only the marker ...
    kv._js.messages["$KV.users.user.1"] = 2  # ... while the stream holds two

    with pytest.raises(nats.errors.TimeoutError):
        await _call(kv, "history", "user.1")


async def test_keys_of_a_bucket_holding_only_deletions_is_empty_behind_a_leading_marker():
    tombstones = [_entry("user.1", 1, 1, KV_DEL), _entry("user.2", 2, 0, KV_PURGE)]

    assert await _call(_bucket(tombstones, marker_first=True), "keys") == []


async def test_keys_applies_its_filters():
    entries = [_entry("user.1", 1, 2), _entry("order.1", 2, 1), _entry("user.2", 3, 0)]

    assert await _call(_bucket(entries, marker_first=True), "keys", filters=["user"]) == [
        "user.1",
        "user.2",
    ]


async def test_a_marker_after_entries_ends_the_snapshot_though_no_entry_has_delta_0():
    """nats-py also queues its marker after a message it skips, when that message
    leaves nothing pending. Only a marker before any entry is in doubt."""
    entries = [_entry("user.1", 1, 1), _entry("user.1", 2, 0, UNKNOWN_MARKER)]

    hist = await _call(_bucket(entries, marker_first=False), "history", "user.1")

    assert [e.revision for e in hist] == [1]


async def test_CONTROL_a_leading_marker_for_a_key_with_no_messages_is_empty_history():
    assert await _call(_bucket([], marker_first=True), "history", "user.1") == []


async def test_CONTROL_a_leading_marker_for_an_empty_bucket_is_no_keys():
    assert await _call(_bucket([], marker_first=True), "keys") == []


async def test_CONTROL_history_without_the_race_is_unchanged():
    hist = await _call(_bucket(HISTORY, marker_first=False), "history", "user.1")

    assert [e.revision for e in hist] == [1, 2]


async def test_CONTROL_keys_without_the_race_are_unchanged():
    assert await _call(_bucket(KEYS, marker_first=False), "keys") == ["user.1", "user.2"]


async def test_CONTROL_keys_of_a_bucket_holding_only_deletions_is_empty_without_the_race():
    tombstones = [_entry("user.1", 1, 1, KV_DEL), _entry("user.2", 2, 0, KV_PURGE)]

    assert await _call(_bucket(tombstones, marker_first=False), "keys") == []


def test_the_private_nats_py_names_the_extension_reads_still_exist():
    """The leading-marker check reads nats-py internals; fail by name if one goes."""
    js = Client().jetstream()
    kv = KeyValue(name=BUCKET, stream="KV_users", pre="$KV.users.", js=js, direct=False)

    assert getattr(kv, "_stream", None) == "KV_users", "KeyValue._stream is gone"
    assert getattr(kv, "_pre", None) == "$KV.users.", "KeyValue._pre is gone"
    assert getattr(kv, "_js", None) is js, "KeyValue._js is gone"
    assert getattr(kv, "_direct", None) is False, "KeyValue._direct is gone"
    assert isinstance(getattr(js, "_timeout", None), int | float), (
        "JetStreamContext._timeout is gone"
    )


SERVER_FAILURE = nats.js.errors.ServerError(code=500, err_code=10062, description="not found")


@pytest.mark.parametrize(
    ("method", "args", "call"), [("history", ("user.1",), "get_msg"), ("keys", (), "stream_info")]
)
async def test_a_server_failure_checking_a_leading_marker_is_not_an_empty_answer(
    method, args, call
):
    """Only the stream saying it holds nothing makes a leading marker mean none."""
    kv = _bucket([], marker_first=True)
    setattr(kv._js, call, AsyncMock(side_effect=SERVER_FAILURE))

    with pytest.raises(nats.js.errors.ServerError):
        await _call(kv, method, *args)


async def test_the_watch_is_stopped_whether_the_read_returns_or_raises():
    answered = _bucket(HISTORY, marker_first=True)
    await _call(answered, "history", "user.1")

    timed_out = _bucket([], marker_first=True)
    timed_out._js.messages["$KV.users.user.1"] = 2
    with pytest.raises(nats.errors.TimeoutError):
        await _call(timed_out, "history", "user.1")

    for kv in (answered, timed_out):
        assert [w._sub.unsubscribe.await_count for w in kv.watchers] == [1]
