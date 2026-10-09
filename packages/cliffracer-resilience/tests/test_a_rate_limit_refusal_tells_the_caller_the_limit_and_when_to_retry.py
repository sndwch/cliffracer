"""A caller refused by a rate limit learns the limit and when to retry, and never the key.

The refusal exception carried `details` (limit, window, key fingerprint) and a `retry_after`, and
the RPC reply threw both away, so a well-behaved client could only hammer. The reply now carries
them next to its unchanged `success`, `error` and `code`; the key is the fingerprint, never the
header value it was resolved from.
"""

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import InMemoryRateLimiter, ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

SECRET = "Bearer s3cret-token-do-not-leak"


class _Svc(CliffracerService):
    resilience = ResilienceExtension(limiter=InMemoryRateLimiter())

    @rpc
    @rate_limit(calls=1, window=60.0, key="authorization")
    async def work(self) -> int:
        return 1


async def _call(svc: _Svc) -> dict:
    msg = AsyncMock()
    msg.subject = "svc.rpc.work"
    msg.data = json.dumps({}).encode()
    msg.headers = {"Authorization": SECRET}
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.await_args_list[0].args[0].decode())


async def test_a_refused_call_carries_the_limit_the_window_and_a_retry_hint():
    svc = _Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    assert (await _call(svc))["success"] is True
    refused = await _call(svc)

    assert (refused["success"], refused["error"], refused["code"]) == (
        False,
        "refused: rate limit exceeded",
        "refused",
    )
    assert refused["details"]["calls"] == 1 and refused["details"]["window"] == 60.0
    assert 0 < refused["retry_after"] <= 60.0, refused
    assert refused["details"]["key"].startswith("sha256:")


async def test_the_reply_never_contains_the_key_value():
    svc = _Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await _call(svc)

    refused = await _call(svc)

    assert SECRET not in json.dumps(refused), refused
    assert "s3cret" not in json.dumps(refused), refused
