"""`cliffracer.calls.call` and `stream` against a service on a live broker."""

import asyncio
from collections.abc import AsyncIterator

import nats
import pytest

from cliffracer import CliffracerService, RpcNoRespondersError, ServiceConfig, rpc
from cliffracer.calls import call, stream
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

WITHIN = 10.0


class Sums(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="untyped_live", health_port=0))

    @rpc
    async def add(self, a: int, b: int) -> int:
        return a + b

    @rpc
    async def count(self, n: int) -> AsyncIterator[int]:
        for value in range(n):
            yield value


@pytest.fixture
async def sums():
    service = Sums()
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


async def test_a_call_and_a_stream_reach_a_live_service(sums):
    nc = await nats.connect(broker_url())
    try:
        prefix = sums.config.subject_prefix or ""
        total = await call(nc, "untyped_live", "add", {"a": 2, "b": 3}, subject_prefix=prefix)
        items = await asyncio.wait_for(
            _collect(stream(nc, "untyped_live", "count", {"n": 500}, subject_prefix=prefix)),
            WITHIN,
        )
    finally:
        await nc.close()

    assert total == 5
    assert items == list(range(500))


async def test_a_call_to_a_method_nothing_holds_raises_no_responders_on_a_live_broker(sums):
    nc = await nats.connect(broker_url())
    try:
        with pytest.raises(RpcNoRespondersError):
            await call(
                nc, "nobody_live", "add", {}, subject_prefix=sums.config.subject_prefix or ""
            )
    finally:
        await nc.close()


async def _collect(items):
    return [item async for item in items]
