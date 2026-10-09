"""A key the distributed limiter has refused is refused again from memory until the window moves.

Every refusal read the key's whole timestamp list (26.6 KB at `calls=1000`) to say no, and the
extension then read it a second time for the retry hint: a denied call sent about 100 bytes and
received 53 KB, so a client hammering a full limit cost the broker most of what the limit existed
to save. The list can only gain timestamps from other replicas, never lose one before it leaves
the window, so "refused until the (n - calls + 1)-th oldest timestamp expires" cannot become wrong
by waiting; the one thing that can make it wrong is a `reset()` made on another replica, which is
seen when the cached deadline passes and not before. The limiter's own `reset()` clears it.

Time is a fake clock and the store counts its reads, because the question is how many.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_resilience import KvRateLimiter

pytestmark = pytest.mark.unit


class _Kv:
    """The slice of nats-py's KeyValue the limiter uses, counting what is asked of it."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[bytes, int]] = {}
        self._revision = 0
        self.gets = self.writes = 0
        self.fail_reads: Exception | None = None

    async def get(self, key):
        self.gets += 1
        if self.fail_reads is not None:
            raise self.fail_reads
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        value, revision = self.entries[key]
        return SimpleNamespace(value=value, revision=revision)

    async def create(self, key, value):
        self.writes += 1
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self._revision += 1
        self.entries[key] = (value, self._revision)

    async def update(self, key, value, last=None):
        self.writes += 1
        if key not in self.entries or (last is not None and self.entries[key][1] != last):
            raise nats.js.errors.KeyWrongLastSequenceError()
        self._revision += 1
        self.entries[key] = (value, self._revision)

    async def delete(self, key, last=None):
        self.entries.pop(key, None)

    async def keys(self):
        if not self.entries:
            raise nats.js.errors.NoKeysError()
        return list(self.entries)


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves; `time.time` is what the limiter stamps and compares."""
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _limiter(kv: _Kv, **kwargs) -> KvRateLimiter:
    return KvRateLimiter(kv=kv, **kwargs)


async def _fill(limiter, key, calls, window, clock, gap=0.0):
    for _ in range(calls):
        assert await limiter.acquire(key, calls, window) is True
        clock[0] += gap


async def test_a_denied_key_is_read_once_and_then_answered_with_no_reads(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 3, 60.0, clock)
    reads_before = kv.gets

    results = [await limiter.acquire("k", 3, 60.0) for _ in range(10)]

    assert results == [False] * 10
    assert kv.gets - reads_before == 1, "only the first refusal reads the list"
    assert limiter.denied_locally == 9


async def test_the_retry_hint_after_a_refusal_is_answered_without_a_read_too(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 3, 60.0, clock, gap=1.0)  # stamps at +0, +1, +2
    await limiter.acquire("k", 3, 60.0)  # refused: reads once, caches
    reads = kv.gets
    clock[0] += 10.0

    hint = await limiter.get_retry_after("k", 60.0)

    assert kv.gets == reads
    assert hint == pytest.approx(60.0 - 13.0)  # the oldest stamp leaves 60s after +0; now is +13


async def test_the_cached_hint_equals_what_a_live_read_gives(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 3, 60.0, clock, gap=1.0)
    await limiter.acquire("k", 3, 60.0)
    clock[0] += 7.5
    cached = await limiter.get_retry_after("k", 60.0)

    live = await _limiter(kv).get_retry_after("k", 60.0)  # a replica with no cache reads it

    assert cached == pytest.approx(live)


async def test_the_refusal_ends_when_the_window_moves_far_enough_to_free_a_slot(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 2, 10.0, clock, gap=4.0)  # stamps at +0 and +4, now +8
    assert await limiter.acquire("k", 2, 10.0) is False  # reads, caches until +10
    reads = kv.gets

    clock[0] += 1.9  # +9.9: the oldest stamp has not left yet
    assert await limiter.acquire("k", 2, 10.0) is False
    assert kv.gets == reads

    clock[0] += 0.2  # +10.1: the oldest stamp left, one slot is free
    assert await limiter.acquire("k", 2, 10.0) is True
    assert kv.gets == reads + 1, "the cache ended at its deadline and the list was read again"


async def test_the_deadline_is_when_enough_stamps_have_left_not_the_first_one(clock):
    """With 4 stamps counted and calls=2, two must leave before a call is allowed again."""
    kv = _Kv()
    limiter = _limiter(kv)
    safe = limiter._safe_key("k")
    now = clock[0]
    kv.entries[safe] = (json.dumps([now - 8, now - 6, now - 4, now - 2]).encode(), 1)

    assert await limiter.acquire("k", 2, 10.0) is False
    reads = kv.gets
    clock[0] += 2.5  # the stamp at -8 has left (window 10), three remain: still over calls=2
    assert await limiter.acquire("k", 2, 10.0) is False
    assert kv.gets == reads
    clock[0] += 2.0  # the stamp at -6 has left too: two remain, which is still calls=2
    assert await limiter.acquire("k", 2, 10.0) is False
    assert kv.gets == reads
    clock[0] += 1.6  # the stamp at -4 has left (at +6): one remains, a slot is free
    assert await limiter.acquire("k", 2, 10.0) is True


async def test_another_replica_resetting_the_key_is_seen_when_the_deadline_passes(clock):
    """The documented price: a reset elsewhere is not seen before the cached deadline."""
    kv = _Kv()
    limiter, other = _limiter(kv), _limiter(kv)
    await _fill(limiter, "k", 2, 10.0, clock)
    assert await limiter.acquire("k", 2, 10.0) is False  # cached until +10

    await other.reset("k")

    assert await limiter.acquire("k", 2, 10.0) is False, "still refused from memory"
    clock[0] += 10.1
    assert await limiter.acquire("k", 2, 10.0) is True, "seen at the deadline"


async def test_the_limiters_own_reset_clears_the_refusal(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 2, 10.0, clock)
    assert await limiter.acquire("k", 2, 10.0) is False

    await limiter.reset("k")

    assert await limiter.acquire("k", 2, 10.0) is True


async def test_resetting_every_key_clears_every_refusal(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    for key in ("a", "b"):
        await _fill(limiter, key, 1, 10.0, clock)
        assert await limiter.acquire(key, 1, 10.0) is False

    await limiter.reset()

    assert await limiter.acquire("a", 1, 10.0) is True
    assert await limiter.acquire("b", 1, 10.0) is True


async def test_another_replica_adding_stamps_cannot_shorten_a_refusal(clock):
    """Stamps only accumulate from elsewhere, so a cached refusal never outlives its truth."""
    kv = _Kv()
    limiter, other = _limiter(kv), _limiter(kv)
    await _fill(limiter, "k", 2, 10.0, clock)
    assert await limiter.acquire("k", 2, 10.0) is False
    reads = kv.gets

    assert await other.acquire("k", 2, 10.0) is False  # the other replica is refused as well
    clock[0] += 5.0

    assert await limiter.acquire("k", 2, 10.0) is False
    assert kv.gets == reads + 1  # the other replica's own refusal read; ours did not


async def test_a_different_limit_or_window_for_the_same_key_is_not_answered_from_the_cache(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 2, 10.0, clock)
    assert await limiter.acquire("k", 2, 10.0) is False
    reads = kv.gets

    assert await limiter.acquire("k", 5, 10.0) is True  # a higher limit sees room
    assert kv.gets > reads


async def test_the_cache_is_bounded_by_deny_cache_size(clock):
    kv = _Kv()
    limiter = _limiter(kv, deny_cache_size=3)
    for index in range(10):
        key = f"key{index}"
        await _fill(limiter, key, 1, 60.0, clock)
        assert await limiter.acquire(key, 1, 60.0) is False

    assert len(limiter._denied_until) <= 3
    assert len(limiter._retry_at) <= 3


async def test_a_size_of_zero_turns_the_cache_off(clock):
    kv = _Kv()
    limiter = _limiter(kv, deny_cache_size=0)
    await _fill(limiter, "k", 1, 60.0, clock)
    reads = kv.gets

    for _ in range(5):
        assert await limiter.acquire("k", 1, 60.0) is False

    assert kv.gets - reads == 5
    assert limiter.denied_locally == 0


async def test_a_local_refusal_needs_no_broker(clock):
    kv = _Kv()
    limiter = _limiter(kv)
    await _fill(limiter, "k", 1, 60.0, clock)
    assert await limiter.acquire("k", 1, 60.0) is False
    kv.fail_reads = RuntimeError("broker gone")

    assert await limiter.acquire("k", 1, 60.0) is False
    assert limiter.health_details()["status"] == "distributed"


async def test_CONTROL_permitted_calls_still_read_and_write_the_list(clock):
    kv = _Kv()
    limiter = _limiter(kv)

    for _ in range(3):
        assert await limiter.acquire("k", 5, 60.0) is True

    assert kv.gets == 3 and kv.writes == 3
