"""What the README says a limit costs, and what it does not.

A limit is checked before the payload is validated (validation runs after every declared
extension), so a payload that validation refuses has already spent a permit of it; and a
`@rate_limit` function that is called directly counts against its own in-memory limiter, not the
extension's. The second is a decision still open, so this pins what is true and is the test to
change when it is decided differently.
"""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import (
    InMemoryRateLimiter,
    RateLimitConfig,
    RateLimitExceeded,
    ResilienceExtension,
    rate_limit,
)

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


class CountingLimiter(InMemoryRateLimiter):
    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[str] = []

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        self.acquired.append(key)
        return await super().acquire(key, calls, window)


def _service_class(limiter: CountingLimiter) -> type[CliffracerService]:
    class Search(CliffracerService):
        resilience = ResilienceExtension(limiter=limiter)

        @rpc
        @rate_limit(calls=3, window=60.0)
        async def search(self, query: str) -> str:
            return f"found {query}"

    return Search


async def _call(svc: Any, payload: dict) -> dict:
    msg = AsyncMock()
    msg.subject = "search.rpc.search"
    msg.data = json.dumps(payload).encode()
    msg.headers = None
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.await_args_list[0].args[0].decode())


async def test_a_payload_validation_refuses_has_spent_a_permit_of_the_limit():
    limiter = CountingLimiter()
    svc = _service_class(limiter)(ServiceConfig(name="search", health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    malformed = [await _call(svc, {"query": 12345, "bogus": "x"}) for _ in range(20)]
    well_formed = [await _call(svc, {"query": "a"}) for _ in range(4)]

    assert limiter.acquired == ["search:search"] * 24, (
        "every call reached the limit, malformed or not"
    )
    assert [r["error"] for r in malformed[:3]] == ["validation failed"] * 3
    assert {r["error"] for r in malformed[3:]} == {"refused: rate limit exceeded"}
    assert [r["success"] for r in well_formed] == [False] * 4, (
        "the limit was spent on the malformed"
    )


async def test_a_directly_called_limit_counts_against_its_own_limiter_not_the_extensions():
    extension_limiter = CountingLimiter()
    extension = ResilienceExtension(limiter=extension_limiter)
    extension._rate_limits["checkout"] = RateLimitConfig(calls=10, window=60.0)

    @rate_limit(calls=1, window=60.0)
    async def reserve(sku: str) -> str:
        return sku

    @rate_limit(calls=1, window=60.0)
    async def release(sku: str) -> str:
        return sku

    context = WorkerContext(
        kind="rpc",
        subject="store.rpc.checkout",
        headers={},
        correlation_id="c",
        payload={},
        data={"handler_name": "checkout"},
    )
    await extension.worker_setup(context)
    try:
        assert await reserve("a") == "a"
        assert await release("a") == "a", "two decorated functions do not share one budget"
        with pytest.raises(RateLimitExceeded):
            await reserve("a")
    finally:
        await extension.worker_teardown(context)

    assert extension_limiter.acquired == ["checkout"], "only the dispatched handler used it"
