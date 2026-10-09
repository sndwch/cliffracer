"""A KV limiter's retry hint counts from the oldest live stamp, whatever order replicas wrote them.

Replicas sharing a bucket append `time.time()` stamps in the order they write, so with clocks apart
the stored list is not sorted. A refused caller is told to retry when the oldest live stamp leaves
the window, the earliest the limiter can admit again, from the hint it remembers and from the one
it reads back from the bucket. Here the bucket holds no more stamps than the limit, so that is
exactly when it admits. The fake bucket answers as nats-py's KeyValue does: `get` raises
`KeyNotFoundError`, and `create` and `update` check the revision. The clock is the module's own
`time`, replaced, so the event loop's clock is untouched.
"""

import json
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_resilience import KvRateLimiter
from cliffracer_resilience import rate_limiter as module

pytestmark = pytest.mark.unit


class _Kv:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[bytes, int]] = {}
        self._revision = 0
        self.writes = 0
        self.fail_get: Exception | None = None
        self.fail_keys: Exception | None = None
        self.listed_extra: list[str] = []

    def _next(self) -> int:
        self._revision += 1
        return self._revision

    def put(self, key: str, stamps: list[float] | bytes) -> None:
        value = stamps if isinstance(stamps, bytes) else json.dumps(stamps).encode()
        self.entries[key] = (value, self._next())

    async def get(self, key):
        if self.fail_get is not None:
            raise self.fail_get
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        value, revision = self.entries[key]
        return SimpleNamespace(value=value, revision=revision)

    async def create(self, key, value):
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.writes += 1
        self.entries[key] = (value, self._next())

    async def update(self, key, value, last=None):
        if key not in self.entries or (last is not None and self.entries[key][1] != last):
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.writes += 1
        self.entries[key] = (value, self._next())

    async def delete(self, key, last=None):
        if last is not None and key in self.entries and self.entries[key][1] != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries.pop(key, None)

    async def keys(self):
        if self.fail_keys is not None:
            raise self.fail_keys
        listed = self.listed_extra + list(self.entries)
        if not listed:
            raise nats.js.errors.NoKeysError()
        return listed


class _Js:
    """A JetStream context on a connection that is open until told otherwise."""

    def __init__(self, kv: _Kv | None = None, fail: Exception | None = None) -> None:
        self._nc = SimpleNamespace(is_closed=False)
        self.kv = kv
        self.fail = fail
        self.opens = 0

    async def key_value(self, name):
        self.opens += 1
        if self.fail is not None:
            raise self.fail
        return self.kv


class _Clock:
    def __init__(self) -> None:
        self.wall = 1000.0
        self.mono = 1000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def _limiter(kv, **options):
    return KvRateLimiter(js=_Js(kv), bucket_name="limits", **options)


async def _scenario(monkeypatch, fresh_reader):
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    kv = _Kv()
    a, b = _limiter(kv), _limiter(kv)
    clock.wall = 1005.0  # replica A's clock
    assert await a.acquire("k", 2, 10.0) is True
    clock.wall = 1000.0  # replica B's clock is 5 s behind
    assert await b.acquire("k", 2, 10.0) is True
    clock.wall = 1001.0
    # A reader that remembers no refusal reads the hint from the bucket.
    reader = _limiter(kv, deny_cache_size=0) if fresh_reader else a
    assert await reader.acquire("k", 2, 10.0) is False
    hint = await reader.get_retry_after("k", 10.0)
    stored = json.loads(kv.entries[next(iter(kv.entries))][0])
    clock.wall = 1001.0 + 9.5  # the stamp written at 1000 has expired
    admitted = await _limiter(kv).acquire("k", 2, 10.0)
    return stored, hint, admitted


@pytest.mark.parametrize("fresh_reader", [False, True], ids=["remembered", "read-from-bucket"])
async def test_the_retry_hint_is_when_capacity_returns_whatever_order_replicas_wrote(
    monkeypatch, fresh_reader
):
    stored, hint, admitted = await _scenario(monkeypatch, fresh_reader)

    assert stored == [1005.0, 1000.0]
    assert admitted is True  # capacity returned 9.5 s after the refusal
    assert hint == pytest.approx(9.0)  # so the hint is 9 s, the oldest live stamp's expiry


async def test_CONTROL_stamps_written_in_clock_order_give_the_right_hint(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    kv = _Kv()
    a, b = _limiter(kv), _limiter(kv)
    clock.wall = 1000.0
    assert await b.acquire("k", 2, 10.0) is True
    clock.wall = 1005.0
    assert await a.acquire("k", 2, 10.0) is True
    clock.wall = 1001.0

    assert await a.acquire("k", 2, 10.0) is False
    assert await a.get_retry_after("k", 10.0) == pytest.approx(9.0)
