"""Two service replicas share one opaque, fail-closed rate-limit budget."""

import uuid

import nats
import pytest
from cliffracer_resilience import KvRateLimiter, RateLimiterUnavailableError

from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.mark.asyncio
async def test_replicas_share_a_secret_free_authoritative_budget():
    bucket = f"rate_limits_{uuid.uuid4().hex}"
    credential = "Bearer eyJhbGciOi.SUPER-SECRET-TOKEN"
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    kv = await js.create_key_value(bucket=bucket)
    first = KvRateLimiter(kv=kv)
    second = KvRateLimiter(kv=kv)
    try:
        assert await first.acquire(credential, calls=2, window=60)
        assert await second.acquire(credential, calls=2, window=60)
        assert not await first.acquire(credential, calls=2, window=60)

        stored = await kv.keys()
        assert stored == [first._safe_key(credential)]
        assert credential not in stored[0]

        await kv.put(stored[0], b'["poison"]')
        with pytest.raises(RateLimiterUnavailableError):
            await second.acquire(credential, calls=2, window=60)
    finally:
        await js.delete_key_value(bucket)
        await nc.close()
