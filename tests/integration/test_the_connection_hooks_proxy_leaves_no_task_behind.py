"""The proxy the connection-hook tests run through leaves no task behind when it closes.

`Proxy` serves each connection from a task the server starts, and that task opens the upstream
connection and then starts two pump tasks. Its teardown cancelled the pumps it knew about, so a
connection that arrived while it was closing, which a client reconnecting after a cut does, had its
pumps started afterwards and nothing cancelled them: the root conftest's leaked-task check then
failed whichever test was closing the proxy. This forces the arrival into the closing window rather
than waiting for load to do it, and requires that no task of the proxy's is alive.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.test_a_silent_partition_is_noticed_within_the_ping_window import (
    proxy_tasks,
)
from tests.integration.test_extension_connection_hooks import LOOPBACK, Proxy

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def test_a_client_that_arrives_as_the_proxy_closes_leaves_no_task_behind():
    for _ in range(60):
        async with Proxy() as proxy:
            reader, writer = await asyncio.open_connection(LOOPBACK, proxy.port)
        writer.close()
        await asyncio.sleep(0)

        alive = proxy_tasks()
        assert not alive, f"the proxy left {len(alive)} task(s) running: {alive}"


async def test_a_proxy_that_served_traffic_and_was_cut_leaves_no_task_behind():
    """The ordinary case, so a fix for the race cannot have broken the proxy's normal teardown."""
    async with Proxy() as proxy:
        reader, writer = await asyncio.open_connection(LOOPBACK, proxy.port)
        writer.write(b"PING\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(1), timeout=5)  # the broker's INFO arrives through it
        await proxy.sever()
        writer.close()

    assert not proxy_tasks()
    assert proxy.connections == 1
