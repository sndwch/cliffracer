"""A generated client and `stream_rpc` read a streamed reply on a live broker: every item in
order and typed, and a loop left early stops the service.

The in-memory broker models the reply inbox and the no-responders status; these rows hold the
reader to what the real broker and nats-py do.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import nats
import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceClient, ServiceConfig, rpc
from cliffracer.generate_client import emit
from cliffracer.introspect import describe
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

#: Bound on any wait here, so a stream that never ends fails by name instead of hanging.
WITHIN = 10.0


class Line(BaseModel):
    number: int


class Feed(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="stream_client_live", health_port=0))
        self.yielded = 0
        self.closed = asyncio.Event()

    @rpc
    async def tail(self, n: int, pause: float = 0.0) -> AsyncIterator[Line]:
        try:
            for number in range(n):
                self.yielded += 1
                yield Line(number=number)
                await asyncio.sleep(pause)
        finally:
            self.closed.set()


class Caller(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="stream_client_caller", health_port=0))


@pytest.fixture
async def feed():
    svc = Feed()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


def _client(nc: Any, feed: Feed) -> ServiceClient:
    namespace: dict[str, Any] = {}
    exec(compile(emit(describe(Feed)), "<generated>", "exec"), namespace)  # noqa: S102
    cls = next(
        v
        for v in namespace.values()
        if isinstance(v, type) and issubclass(v, ServiceClient) and v is not ServiceClient
    )
    return cls(nc=nc, service=feed.config.name, subject_prefix=feed.config.subject_prefix or "")


async def test_a_generated_client_reads_a_thousand_items_in_order(feed):
    nc = await nats.connect(broker_url())
    try:
        client = _client(nc, feed)
        lines = await asyncio.wait_for(_collect(client.tail(n=1000)), WITHIN)
    finally:
        await nc.close()

    assert lines == [Line(number=i) for i in range(1000)]


async def test_a_loop_left_early_stops_the_service(feed):
    nc = await nats.connect(broker_url())
    try:
        client = _client(nc, feed)
        stream = client.tail(n=1000, pause=0.005)
        got = []
        async for line in stream:
            got.append(line)
            if len(got) == 2:
                break
        await stream.aclose()
        await asyncio.wait_for(feed.closed.wait(), WITHIN)
    finally:
        await nc.close()

    assert feed.yielded < 100, f"the handler went on for {feed.yielded} items"


async def test_stream_rpc_reads_a_stream_from_another_service(feed):
    caller = Caller()
    await caller.start()
    try:
        lines = await asyncio.wait_for(
            _collect(caller.stream_rpc("stream_client_live", "tail", n=5)), WITHIN
        )
    finally:
        await caller.stop()

    assert [line["number"] for line in lines] == [0, 1, 2, 3, 4]


async def _collect(items: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in items]
