"""`ResilienceExtension` does not let a handler run when its limiter cannot decide.

A limiter whose backing store is down raises something other than a refusal.
The extension is `fails_closed`, so the container skips the handler and answers
the caller with an error: a rate limit that stops existing at exactly the
moment its store is unhealthy is the failure a limit is for. A refusal
(`RateLimitExceeded`) is honoured whatever the flag says, so the tests that
only ever hit the limit cannot tell the two postures apart; this drives the
other kind of failure through the container.

The second test is the control: the same handler runs when the limiter works,
so the first cannot pass because nothing ever runs.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import InMemoryRateLimiter, RateLimiter, ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


class _UnavailableLimiter(RateLimiter):
    """A limiter whose backing store is down: every decision raises."""

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        raise RuntimeError("the rate limit store is down")

    async def reset(self, key: str | None = None) -> None:
        return None

    async def prune_expired(self, window: float | None = None) -> int:
        return 0

    async def get_retry_after(self, key: str, window: float) -> float:
        return 0.0


def _rpc_msg(subject: str) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = subject
    msg.data = json.dumps({}).encode()
    msg.headers = {}
    return msg


def _replies(msg: AsyncMock) -> list[dict]:
    return [json.loads(call.args[0].decode()) for call in msg.respond.await_args_list]


async def test_a_limiter_that_fails_does_not_let_the_handler_run():
    executed = 0

    class Svc(CliffracerService):
        resilience = ResilienceExtension(limiter=_UnavailableLimiter())

        @rpc
        @rate_limit(calls=5, window=10.0)
        async def work(self) -> str:
            nonlocal executed
            executed += 1
            return "done"

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _rpc_msg("svc.rpc.work")
    await svc.container._handle_rpc_request(msg)

    assert executed == 0, "the handler ran although the limiter could not decide"
    (reply,) = _replies(msg)
    assert "error" in reply, reply
    assert reply.get("result") != "done", reply


async def test_CONTROL_the_same_handler_runs_when_the_limiter_works():
    executed = 0

    class Svc(CliffracerService):
        resilience = ResilienceExtension(limiter=InMemoryRateLimiter())

        @rpc
        @rate_limit(calls=5, window=10.0)
        async def work(self) -> str:
            nonlocal executed
            executed += 1
            return "done"

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _rpc_msg("svc.rpc.work")
    await svc.container._handle_rpc_request(msg)

    assert executed == 1
    (reply,) = _replies(msg)
    assert reply.get("result") == "done", reply
