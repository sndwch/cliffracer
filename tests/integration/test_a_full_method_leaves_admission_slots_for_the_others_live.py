"""On a live broker, a slow limited method's queue leaves admission slots for the other methods.

The service admits 4 requests at once (`max_rpc_in_flight=4`). `slow` runs one at a time and each
call holds until released; `fast` is unlimited. Ten `slow` requests arrive, then `fast` is called.
With the default `max_queued` (half the bound, 2), `slow` holds 3 admitted requests and `fast` is
answered. With `max_queued=9`, the slow method's queue takes every slot and `fast` is `busy`, which
is what every service with a limited method did before the cap.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

NEVER = 10.0


class Desk(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="queue_cap_live", health_port=0, max_rpc_in_flight=4))
        self.release = asyncio.Event()

    @rpc(max_concurrency=1)
    async def slow(self) -> int:
        await self.release.wait()
        return 1

    @rpc(max_concurrency=1, max_queued=9)
    async def slow_unbounded(self) -> int:
        await self.release.wait()
        return 1

    @rpc
    async def fast(self) -> int:
        return 2


@pytest.fixture
async def service():
    svc = Desk()
    await svc.start()
    try:
        yield svc
    finally:
        svc.release.set()
        await svc.stop()


async def _call(nc, service: Desk, method: str) -> dict:
    subject = HandlerDiscovery.with_namespace(service.config, f"queue_cap_live.rpc.{method}")
    reply = await nc.request(subject, b"{}", timeout=NEVER)
    return json.loads(reply.data)


async def _queue(nc, service: Desk, method: str, waiting: int) -> list[asyncio.Task]:
    calls = [asyncio.create_task(_call(nc, service, method)) for _ in range(10)]

    async def admitted() -> None:
        while (
            service.container.dispatcher.limits.details().get(method, {}).get("waiting", 0)
            < waiting
        ):
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(admitted(), NEVER)
    except BaseException:
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        raise
    return calls


async def test_with_the_default_cap_the_fast_method_is_answered(nats_connection, service):
    slow = await _queue(nats_connection, service, "slow", waiting=2)

    reply = await _call(nats_connection, service, "fast")

    assert reply.get("result") == 2, reply
    service.release.set()
    replies = await asyncio.gather(*slow)
    codes = sorted(r.get("code", "ok") for r in replies)
    assert codes == ["busy"] * 7 + ["ok"] * 3


async def test_CONTROL_a_queue_as_deep_as_the_bound_starves_the_fast_method(
    nats_connection, service
):
    slow = await _queue(nats_connection, service, "slow_unbounded", waiting=3)

    reply = await _call(nats_connection, service, "fast")

    assert reply.get("code") == "busy"
    service.release.set()
    await asyncio.gather(*slow)
