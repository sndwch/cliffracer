"""`/health` names the limiter that is counting, instead of reporting any custom one as local memory.

An operator reads `rate_limiter.backend` and `status` to learn whether a distributed limiter is
authoritative. A limiter other than the two shipped ones was reported as `memory` and `local`,
which is the opposite of what a distributed one is.
"""

import pytest
from cliffracer_resilience import InMemoryRateLimiter, RateLimiter, ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


class Silent(RateLimiter):
    """A limiter that says nothing about itself."""

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        return True

    async def get_retry_after(self, key: str, window: float) -> float:
        return 0.0

    async def reset(self, key: str | None = None) -> None:
        return None


class Remote(Silent):
    """A distributed limiter that reports what it is."""

    def health_details(self):
        return {"backend": "redis", "status": "ok", "nodes": 3}


async def _health(limiter: RateLimiter, handler_limiter: RateLimiter | None = None) -> dict:
    class Svc(CliffracerService):
        resilience = ResilienceExtension(limiter=limiter)

        @rpc
        @rate_limit(calls=1, window=60.0, limiter=handler_limiter)
        async def work(self) -> int:
            return 1

    service = Svc(ServiceConfig(name="svc", subject_prefix=None))
    await service.container._setup_extensions()
    return service.resilience.health_details()


async def test_a_custom_limiter_reports_what_it_says_about_itself():
    details = await _health(Remote())

    assert details["rate_limiter"] == {"backend": "redis", "status": "ok", "nodes": 3}


async def test_a_custom_limiter_that_says_nothing_is_named_and_not_called_local():
    details = await _health(Silent())

    assert details["rate_limiter"] == {"backend": "Silent", "status": "unreported"}


async def test_a_handlers_own_custom_limiter_is_described_the_same_way():
    details = await _health(InMemoryRateLimiter(), handler_limiter=Remote())

    assert details["handler_rate_limiters"] == {
        "work": {"backend": "redis", "status": "ok", "nodes": 3}
    }


async def test_CONTROL_the_in_memory_limiter_is_still_local_with_its_key_count():
    details = await _health(InMemoryRateLimiter())

    assert details["rate_limiter"] == {"backend": "memory", "status": "local", "tracked_keys": 0}
