"""A limiter that could not open its bucket does not spend a JetStream round trip on every dispatch.

`acquire` opens the bucket on demand when it has a JetStream context and no bucket. When the open
fails that happened on every message: each paid for its own failed request, and a limiter without
the in-memory fallback raised from the same failing call each time. The open is now retried at most
once every `INTERVAL`, and the dispatches in between are decided without a round trip.
"""

from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_resilience import KvRateLimiter
from cliffracer_resilience import rate_limiter as module
from cliffracer_resilience.rate_limiter import RateLimiterUnavailableError

pytestmark = pytest.mark.unit

#: A literal, so a limiter that never waits fails on how often it opened the bucket.
INTERVAL = 5.0


def _js_that_cannot_open() -> AsyncMock:
    js = AsyncMock()
    js.key_value.side_effect = RuntimeError("no responders")
    return js


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(module.time, "monotonic", fake.monotonic)
    return fake


async def test_a_failed_open_is_tried_once_for_a_burst_of_dispatches(clock):
    js = _js_that_cannot_open()
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)

    for _ in range(20):
        assert await limiter.acquire("caller", 100, 60.0) is True

    assert js.key_value.await_count == 1, "each dispatch paid for its own failed open"


async def test_the_open_is_tried_again_once_the_interval_has_passed(clock):
    js = _js_that_cannot_open()
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)
    await limiter.acquire("caller", 100, 60.0)

    clock.now += INTERVAL - 0.1
    await limiter.acquire("caller", 100, 60.0)
    assert js.key_value.await_count == 1

    clock.now += 0.2
    await limiter.acquire("caller", 100, 60.0)
    assert js.key_value.await_count == 2


async def test_a_limiter_without_the_fallback_still_refuses_each_dispatch_without_a_round_trip(
    clock,
):
    js = _js_that_cannot_open()
    limiter = KvRateLimiter(js=js, in_memory_fallback=False)

    for _ in range(5):
        with pytest.raises(RateLimiterUnavailableError):
            await limiter.acquire("caller", 100, 60.0)

    assert js.key_value.await_count == 1


async def test_the_bucket_that_opens_on_a_later_try_is_used(clock):
    js = AsyncMock()
    bucket = AsyncMock()
    bucket.get.side_effect = Exception("not found")
    js.key_value.side_effect = [RuntimeError("down"), bucket]
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)
    await limiter.acquire("caller", 100, 60.0)
    assert limiter._kv is None

    clock.now += INTERVAL + 0.1
    await limiter.acquire("caller", 100, 60.0)

    assert limiter._kv is bucket


async def test_CONTROL_a_limiter_whose_bucket_opens_opens_it_once_and_uses_it(clock):
    js = AsyncMock()
    bucket = AsyncMock()
    bucket.get.side_effect = Exception("not found")
    js.key_value.return_value = bucket
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)

    for _ in range(3):
        await limiter.acquire("caller", 100, 60.0)

    assert js.key_value.await_count == 1
    assert bucket.get.await_count >= 3, "the dispatches were decided in the bucket"


def test_the_interval_is_five_seconds():
    assert module.BUCKET_REOPEN_SECONDS == INTERVAL


def _root_cause(exc: BaseException) -> BaseException:
    while exc.__cause__ is not None:
        exc = exc.__cause__
    return exc


async def test_a_dispatch_that_waits_out_the_interval_reports_the_failed_open_not_a_missing_bucket(
    clock,
):
    js = AsyncMock()
    js.key_value.side_effect = ConnectionRefusedError("broker down")
    limiter = KvRateLimiter(js=js, in_memory_fallback=False)

    with pytest.raises(RateLimiterUnavailableError) as first:
        await limiter.acquire("caller", 100, 60.0)
    with pytest.raises(RateLimiterUnavailableError) as waiting:
        await limiter.acquire("caller", 100, 60.0)

    assert js.key_value.await_count == 1, "the second dispatch was meant to wait"
    assert isinstance(_root_cause(first.value), ConnectionRefusedError)
    assert isinstance(_root_cause(waiting.value), ConnectionRefusedError), _root_cause(
        waiting.value
    )
    assert limiter.health_details()["last_error_type"] == "ConnectionRefusedError"


async def test_the_failure_a_waiting_limiter_reports_is_cleared_when_the_bucket_opens(clock):
    js = AsyncMock()
    bucket = AsyncMock()
    bucket.get.side_effect = nats.js.errors.KeyNotFoundError()
    js.key_value.side_effect = [ConnectionRefusedError("down"), bucket]
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)
    await limiter.acquire("caller", 100, 60.0)
    assert limiter.health_details()["last_error_type"] == "ConnectionRefusedError"

    clock.now += INTERVAL + 0.1
    await limiter.acquire("caller", 100, 60.0)

    assert limiter.health_details()["last_error_type"] is None
