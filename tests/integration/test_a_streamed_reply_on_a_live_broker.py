"""A handler that streams its reply, on a live broker: every item in order, then the envelope, and
a stream that stops when its caller has gone, whether it unsubscribed or closed its connection.

The caller here is a plain nats-py client that subscribes its own inbox, as a streaming caller
does. The in-memory broker models the no-responders status that tells the service its caller has
gone; these rows hold the service to what the real broker sends.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import nats
import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SEQ, END = "Cliffracer-Stream-Seq", "Cliffracer-Stream-End"
#: Bound on any wait here, so a stream that never ends fails by name instead of hanging.
WITHIN = 10.0


class Feed(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="stream_live", health_port=0))
        self.yielded = 0
        self.closed = asyncio.Event()

    @rpc
    async def tail(self, n: int, pause: float) -> AsyncIterator[int]:
        try:
            for value in range(n):
                self.yielded += 1
                yield value
                await asyncio.sleep(pause)
        finally:
            self.closed.set()


@pytest.fixture
async def feed():
    svc = Feed()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def _ask(nc: Any, feed: Feed, n: int, pause: float) -> tuple[Any, list[Any]]:
    """Subscribe an inbox on `nc`, send the request, and return the subscription and the list
    the messages arrive in."""
    got: list[Any] = []

    async def record(msg: Any) -> None:
        got.append(msg)

    inbox = nc.new_inbox()
    sub = await nc.subscribe(inbox, cb=record)
    await nc.flush()
    await nc.publish(
        HandlerDiscovery.with_namespace(feed.config, "stream_live.rpc.tail"),
        json.dumps({"n": n, "pause": pause}).encode(),
        reply=inbox,
        headers={"Content-Type": "application/json", "Cliffracer-Stream": "1"},
    )
    await nc.flush()
    return sub, got


async def _until(condition: Any, what: str) -> None:
    deadline = time.monotonic() + WITHIN
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} within {WITHIN}s")
        await asyncio.sleep(0.005)


async def test_a_thousand_items_arrive_in_order_and_the_envelope_counts_them(nats_connection, feed):
    _, got = await _ask(nats_connection, feed, 1000, 0.0)
    await _until(lambda: got and SEQ not in (got[-1].headers or {}), "no envelope ended the stream")

    items = [(int(m.headers[SEQ]), json.loads(m.data)) for m in got[:-1]]
    assert items == [(i, i) for i in range(1000)]
    envelope = json.loads(got[-1].data)
    assert (envelope["success"], envelope["items"], got[-1].headers[END]) == (True, 1000, "1000")


async def test_a_stream_stops_soon_after_its_caller_unsubscribes(nats_connection, feed):
    sub, got = await _ask(nats_connection, feed, 1000, 0.005)
    await _until(lambda: len(got) >= 2, "the first two items did not arrive")
    await sub.unsubscribe()

    await asyncio.wait_for(feed.closed.wait(), WITHIN)
    assert feed.yielded < 100, f"the handler went on for {feed.yielded} items after its caller left"


async def test_a_stream_stops_soon_after_its_callers_connection_closes(feed):
    nc = await nats.connect(broker_url())
    _, got = await _ask(nc, feed, 1000, 0.005)
    await _until(lambda: len(got) >= 2, "the first two items did not arrive")
    await nc.close()

    await asyncio.wait_for(feed.closed.wait(), WITHIN)
    assert feed.yielded < 100, f"the handler went on for {feed.yielded} items after its caller left"
