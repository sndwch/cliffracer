"""`KvRateLimiter` against a real JetStream KV bucket, which is where its promise is made.

Every other test of it runs against a mock store. What only a broker can show: that two limiters
on two connections share one budget (the point of the class), that the digest key it derives is
accepted by a real bucket whatever the caller's key looks like, that the revision semantics its
compare-and-set relies on hold, that a window really expires, and that a bucket which cannot be
opened is refused by name and reported in health rather than quietly becoming a per-process limit.
"""

import asyncio
import uuid

import nats
import pytest
from cliffracer_resilience import KvRateLimiter
from cliffracer_resilience.rate_limiter import RateLimiterUnavailableError

from conftest import broker_url

pytestmark = [pytest.mark.unit, pytest.mark.nats_required, pytest.mark.asyncio]


async def _connect():
    try:
        return await nats.connect(broker_url(), connect_timeout=2.0)
    except Exception as exc:  # pragma: no cover - the marker normally keeps this from running
        pytest.skip(f"no broker at {broker_url()}: {exc}")


@pytest.fixture
async def bucket():
    """A uniquely named bucket and two independent connections to the same broker."""
    name = f"rl_{uuid.uuid4().hex[:12]}"
    first, second = await _connect(), await _connect()
    try:
        yield name, first.jetstream(), second.jetstream()
    finally:
        try:
            await first.jetstream().delete_key_value(name)
        except Exception:
            pass
        for nc in (first, second):
            if not nc.is_closed:
                await nc.close()


async def _limiter(js, name: str, **kwargs) -> KvRateLimiter:
    limiter = KvRateLimiter(js=js, bucket_name=name, **kwargs)
    await limiter.init_kv()
    return limiter


async def test_two_limiters_on_two_connections_share_one_budget(bucket):
    name, js_a, js_b = bucket
    a, b = await _limiter(js_a, name), await _limiter(js_b, name)
    calls = 5

    results = await asyncio.gather(
        *[(a if n % 2 == 0 else b).acquire("tenant", calls, 60.0) for n in range(4 * calls)]
    )

    assert sum(results) == calls, results


async def test_a_budget_spent_through_one_limiter_is_spent_for_the_other(bucket):
    name, js_a, js_b = bucket
    a, b = await _limiter(js_a, name), await _limiter(js_b, name)

    assert [await a.acquire("tenant", 2, 60.0) for _ in range(2)] == [True, True]

    assert await b.acquire("tenant", 2, 60.0) is False


async def test_different_keys_have_different_budgets(bucket):
    name, js_a, _ = bucket
    limiter = await _limiter(js_a, name)

    assert await limiter.acquire("a", 1, 60.0) and await limiter.acquire("b", 1, 60.0)
    assert not await limiter.acquire("a", 1, 60.0)


@pytest.mark.parametrize(
    "key",
    ["Bearer eyJhbGciOi.payload.sig/with+slashes=", "has spaces and é中文", "a" * 600, ""],
    ids=["a-token", "spaces-and-unicode", "very-long", "empty"],
)
async def test_a_key_that_needs_sanitising_round_trips_through_a_real_bucket(bucket, key):
    name, js_a, _ = bucket
    limiter = await _limiter(js_a, name)

    first = await limiter.acquire(key, 1, 60.0)
    second = await limiter.acquire(key, 1, 60.0)

    assert (first, second) == (True, False)
    stored = await limiter._kv.keys()
    assert len(stored) == 1 and stored[0].startswith("sha256_"), stored
    assert key not in stored[0] or key == ""


async def test_a_window_really_expires(bucket):
    name, js_a, _ = bucket
    limiter = await _limiter(js_a, name)

    assert await limiter.acquire("k", 1, 0.3)
    assert not await limiter.acquire("k", 1, 0.3)
    await asyncio.sleep(0.45)

    assert await limiter.acquire("k", 1, 0.3)


async def test_the_bucket_is_created_when_missing_and_opened_when_present(bucket):
    name, js_a, js_b = bucket

    first = await _limiter(js_a, name, bucket_ttl=120.0)
    second = await _limiter(js_b, name)

    status = await first._kv.status()
    assert status.bucket == name and status.ttl == 120.0
    assert (await second._kv.status()).bucket == name
    assert first.health_details()["status"] == second.health_details()["status"] == "distributed"


async def test_two_limiters_opening_a_missing_bucket_at_once_both_get_it(bucket):
    name, js_a, js_b = bucket

    first, second = await asyncio.gather(_limiter(js_a, name), _limiter(js_b, name))

    assert await first.acquire("k", 1, 60.0)
    assert not await second.acquire("k", 1, 60.0)


async def test_a_bucket_that_cannot_be_opened_is_refused_by_name_and_reported(bucket):
    name, _, _ = bucket
    dead = await _connect()
    js = dead.jetstream()
    await dead.close()
    limiter = KvRateLimiter(js=js, bucket_name=name)

    with pytest.raises(RateLimiterUnavailableError):
        await limiter.acquire("tenant", 1, 60.0)

    details = limiter.health_details()
    assert details["status"] == "unavailable" and details["last_error_type"], details
    assert limiter._kv is None


async def test_opening_a_bucket_that_cannot_be_opened_raises_from_init_kv_itself(bucket):
    name, _, _ = bucket
    dead = await _connect()
    js = dead.jetstream()
    await dead.close()
    limiter = KvRateLimiter(js=js, bucket_name=name)

    with pytest.raises(RateLimiterUnavailableError):
        await limiter.init_kv()

    assert limiter._kv is None
    assert limiter.health_details()["status"] == "unavailable"


async def test_the_opt_in_fallback_is_a_local_budget_that_health_reports(bucket):
    name, _, _ = bucket
    dead = await _connect()
    js = dead.jetstream()
    await dead.close()
    limiter = KvRateLimiter(js=js, bucket_name=name, in_memory_fallback=True)

    assert [await limiter.acquire("tenant", 2, 60.0) for _ in range(3)] == [True, True, False]

    details = limiter.health_details()
    assert details["status"] == "degraded" and details["fallback_total"] == 3, details
