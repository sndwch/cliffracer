"""An in-memory limiter drops each expired permit as it reads it.

`acquire`, `get_retry_after` and `prune_expired` each walk a key's permits from the oldest and drop
those a window old. Each test holds one expired permit in a deque that lets its first item be read
1000 times and then fails by name, so a walk that reads without dropping fails fast instead of
spinning. The clock is the module's own `time`, replaced.
"""

from collections import deque
from types import SimpleNamespace

import pytest
from cliffracer_resilience import InMemoryRateLimiter
from cliffracer_resilience import rate_limiter as module

pytestmark = pytest.mark.unit


class ReadBounded(deque):
    """A deque whose items may be read 1000 times in all."""

    reads = 0

    def __getitem__(self, index):
        self.reads += 1
        if self.reads > 1000:
            raise AssertionError("an expired permit was read 1000 times and never dropped")
        return super().__getitem__(index)


@pytest.fixture
def limiter(monkeypatch):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100.0))
    limiter = InMemoryRateLimiter()
    limiter._windows["k"] = ReadBounded([0.0])
    return limiter


async def test_acquire_drops_an_expired_permit_and_admits(limiter):
    assert await limiter.acquire("k", 1, 10.0) is True
    assert list(limiter._windows["k"]) == [100.0]


async def test_a_retry_hint_drops_an_expired_permit_and_hints_no_wait(limiter):
    assert await limiter.get_retry_after("k", 10.0) == 0.0
    assert "k" not in limiter._windows


async def test_prune_drops_an_expired_permit_and_the_key_it_emptied(limiter):
    assert await limiter.prune_expired(10.0) == 1
    assert limiter.tracked_keys == 0
