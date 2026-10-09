"""A rate limit counts delivery attempts, as the README says.

`ResilienceExtension` calls `acquire` once for every dispatch it sees and keeps no record of which
message it is, so a JetStream redelivery spends another permit, and a permit spent on a message that
a later extension refuses is not returned. A message the limit itself refuses spends none. These
pin what is documented, so a change to any of the three is a deliberate edit of the test; taking up
an admitted-message record is the decision that would make them change.
"""

from types import SimpleNamespace

import pytest
from cliffracer_resilience import InMemoryRateLimiter, RateLimitConfig, ResilienceExtension

from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext

pytestmark = pytest.mark.unit


class CountingLimiter(InMemoryRateLimiter):
    def __init__(self) -> None:
        super().__init__()
        self.acquired = 0

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        self.acquired += 1
        return await super().acquire(key, calls, window)


def _extension(calls: int) -> tuple[ResilienceExtension, CountingLimiter]:
    limiter = CountingLimiter()
    extension = ResilienceExtension(limiter=limiter)
    extension._rate_limits["handle"] = RateLimitConfig(calls=calls, window=60.0)
    return extension, limiter


def _delivery(number: int) -> WorkerContext:
    """The same message, on its `number`th delivery."""
    return WorkerContext(
        kind="event",
        subject="orders.created",
        headers={"Nats-Msg-Id": "order-1"},
        correlation_id="c",
        payload={"order_id": "order-1"},
        data={"handler_name": "handle"},
        raw=SimpleNamespace(metadata=SimpleNamespace(num_delivered=number)),
    )


async def test_three_deliveries_of_one_message_spend_three_permits():
    extension, limiter = _extension(calls=100)

    for number in (1, 2, 3):
        context = _delivery(number)
        await extension.worker_setup(context)
        await extension.worker_teardown(context)

    assert limiter.acquired == 3
    assert len(limiter._windows["handle"]) == 3


async def test_redeliveries_of_one_message_can_exhaust_a_window_on_their_own():
    extension, limiter = _extension(calls=2)
    outcomes = []

    for number in (1, 2, 3):
        context = _delivery(number)
        try:
            await extension.worker_setup(context)
            outcomes.append("admitted")
        except RejectMessage:
            outcomes.append("refused")
        await extension.worker_teardown(context)

    assert outcomes == ["admitted", "admitted", "refused"]


async def test_a_message_the_limit_refuses_spends_no_permit():
    extension, limiter = _extension(calls=1)
    first = _delivery(1)
    await extension.worker_setup(first)
    await extension.worker_teardown(first)

    refused = _delivery(1)
    with pytest.raises(RejectMessage):
        await extension.worker_setup(refused)
    await extension.worker_teardown(refused)

    assert len(limiter._windows["handle"]) == 1


class Refuses(Extension):
    fails_closed = True

    async def worker_setup(self, ctx: WorkerContext) -> None:
        raise RejectMessage("not allowed")


async def test_a_permit_spent_on_a_message_a_later_extension_refuses_is_not_returned():
    extension, limiter = _extension(calls=100)
    pipeline = ExtensionPipeline([extension, Refuses()])

    async def handler() -> None:
        raise AssertionError("the handler must not run")

    with pytest.raises(RejectMessage):
        await pipeline.run_worker(_delivery(1), handler)

    assert limiter.acquired == 1
    assert len(limiter._windows["handle"]) == 1
