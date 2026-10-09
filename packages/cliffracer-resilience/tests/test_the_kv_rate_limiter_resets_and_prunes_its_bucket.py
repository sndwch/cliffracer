"""`KvRateLimiter` can clear and trim the bucket it keeps its counters in.

`reset()` with no key cleared only the local fallback and left every distributed counter in
place, so a caller who reset to get a clean slate got a limiter that still refused; nothing
watched the per-key KV delete either. The bucket also had no way to shrink: one entry per
partition key, never removed. The store here follows nats-py's KeyValue (`get` raises
`KeyNotFoundError`, `create` and `update` check the revision, `keys` raises `NoKeysError` on an
empty bucket); a real server was measured separately (see the pull request).
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_resilience import InMemoryRateLimiter, KvRateLimiter
from cliffracer_resilience.rate_limiter import RateLimiterUnavailableError

pytestmark = pytest.mark.unit


class _Kv:
    """The slice of nats-py's KeyValue the limiter uses, with its revision rules."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[bytes, int]] = {}
        self._revision = 0
        self.fail_delete: Exception | None = None
        self.touch_on_get: set[str] = set()

    def _next(self) -> int:
        self._revision += 1
        return self._revision

    async def get(self, key):
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        value, revision = self.entries[key]
        entry = SimpleNamespace(value=value, revision=revision)
        if key in self.touch_on_get:  # someone writes after this read, before the caller acts
            self.entries[key] = (json.dumps([time.time()]).encode(), self._next())
        return entry

    async def create(self, key, value):
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries[key] = (value, self._next())

    async def update(self, key, value, last=None):
        if key not in self.entries or (last is not None and self.entries[key][1] != last):
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries[key] = (value, self._next())

    async def delete(self, key, last=None):
        if self.fail_delete is not None:
            raise self.fail_delete
        if last is not None and key in self.entries and self.entries[key][1] != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries.pop(key, None)

    async def keys(self):
        if not self.entries:
            raise nats.js.errors.NoKeysError()
        return list(self.entries)


def _limiter(kv: _Kv, **kwargs) -> KvRateLimiter:
    return KvRateLimiter(kv=kv, **kwargs)


async def test_reset_with_no_key_clears_every_counter_in_the_bucket():
    kv = _Kv()
    limiter = _limiter(kv)
    assert await limiter.acquire("a", 1, 60.0) and await limiter.acquire("b", 1, 60.0)
    assert not await limiter.acquire("a", 1, 60.0) and not await limiter.acquire("b", 1, 60.0)

    await limiter.reset()

    assert kv.entries == {}
    assert await limiter.acquire("a", 1, 60.0) and await limiter.acquire("b", 1, 60.0)


async def test_reset_with_a_key_clears_that_key_and_leaves_the_others():
    kv = _Kv()
    limiter = _limiter(kv)
    await limiter.acquire("a", 1, 60.0)
    await limiter.acquire("b", 1, 60.0)

    await limiter.reset("a")

    assert await limiter.acquire("a", 1, 60.0), "the reset key still refuses"
    assert not await limiter.acquire("b", 1, 60.0), "reset(key) cleared another key"
    assert len(kv.entries) == 2


async def test_reset_of_an_empty_bucket_is_not_an_error():
    await _limiter(_Kv()).reset()


async def test_a_failed_reset_is_reported_unless_the_limiter_falls_back():
    kv = _Kv()
    await _limiter(kv).acquire("a", 1, 60.0)
    kv.fail_delete = RuntimeError("the store is down")

    with pytest.raises(RateLimiterUnavailableError):
        await _limiter(kv).reset("a")
    with pytest.raises(RateLimiterUnavailableError):
        await _limiter(kv).reset()

    degraded = _limiter(kv, in_memory_fallback=True)
    await degraded.reset()  # does not raise; the backend is marked degraded
    assert degraded.health_details()["status"] == "degraded"


async def test_prune_expired_deletes_only_the_entries_that_have_left_the_window():
    kv = _Kv()
    limiter = _limiter(kv)
    now = time.time()
    for name, stamps in (("old", [now - 1000]), ("live", [now - 1000, now - 1]), ("fresh", [now])):
        kv.entries[limiter._safe_key(name)] = (json.dumps(stamps).encode(), kv._next())

    removed = await limiter.prune_expired(window=60.0)

    assert removed == 1
    assert set(kv.entries) == {limiter._safe_key("live"), limiter._safe_key("fresh")}


async def test_prune_expired_without_a_window_leaves_the_bucket_alone():
    kv = _Kv()
    limiter = _limiter(kv)
    kv.entries[limiter._safe_key("old")] = (json.dumps([time.time() - 1000]).encode(), 1)

    assert await limiter.prune_expired() == 0
    assert len(kv.entries) == 1


async def test_prune_expired_keeps_an_entry_that_was_written_while_it_was_being_judged():
    kv = _Kv()
    limiter = _limiter(kv)
    key = limiter._safe_key("racing")
    kv.entries[key] = (json.dumps([time.time() - 1000]).encode(), kv._next())
    kv.touch_on_get.add(key)

    assert await limiter.prune_expired(window=60.0) == 0
    assert key in kv.entries


async def test_the_bucket_is_created_with_the_ttl_the_limiter_was_given():
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    js.create_key_value.return_value = _Kv()

    await KvRateLimiter(js=js, bucket_name="limits", bucket_ttl=3600.0).init_kv()

    js.create_key_value.assert_awaited_once_with(bucket="limits", ttl=3600.0)


async def test_CONTROL_without_a_ttl_the_bucket_is_created_as_before():
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError()
    js.create_key_value.return_value = _Kv()

    await KvRateLimiter(js=js, bucket_name="limits").init_kv()

    js.create_key_value.assert_awaited_once_with(bucket="limits")


async def test_the_in_memory_table_sweeps_expired_keys_once_it_passes_max_keys():
    limiter = InMemoryRateLimiter(max_keys=3)
    for i in range(10):
        await limiter.acquire(f"k{i}", 1, 0.001)
    time.sleep(0.01)

    await limiter.acquire("trigger", 1, 0.001)

    assert len(limiter._windows) <= 2, len(limiter._windows)


async def test_the_in_memory_table_never_evicts_a_key_that_is_still_inside_its_window():
    limiter = InMemoryRateLimiter(max_keys=3)
    assert await limiter.acquire("spent", 1, 3600.0)
    for i in range(20):  # far past max_keys, all live
        await limiter.acquire(f"other{i}", 1, 3600.0)

    assert len(limiter._windows) == 21
    assert not await limiter.acquire("spent", 1, 3600.0), "a live key's spent budget was forgotten"
