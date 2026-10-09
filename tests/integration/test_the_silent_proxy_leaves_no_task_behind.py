"""The proxy the silent-partition tests run through leaves no task behind when it closes.

`SilentProxy` serves each connection from a task the server starts, and that task opens the
upstream connection and then starts two pump tasks. Its teardown cancelled the pumps it knew about
and closed the writers it knew about, so a connection that arrived while it was closing, which a
client reconnecting during a partition does, had its pumps started afterwards and nothing
cancelled them. The root conftest's leaked-task check then failed the test at teardown. It showed
up as the silent-partition window tests failing about three runs in ten on a loaded CI runner, on
both parameter sets, and not at all on an idle machine.

This forces the arrival into the closing window instead of waiting for load to do it, and then
requires that no task of the proxy's is alive.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.test_a_silent_partition_is_noticed_within_the_ping_window import (
    LOOPBACK,
    SilentProxy,
    proxy_tasks,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def test_a_client_that_arrives_as_the_proxy_closes_leaves_no_task_behind():
    for _ in range(60):
        async with SilentProxy() as proxy:
            reader, writer = await asyncio.open_connection(LOOPBACK, proxy.port)
        writer.close()
        await asyncio.sleep(0)

        alive = proxy_tasks()
        assert not alive, f"the proxy left {len(alive)} task(s) running: {alive}"


async def test_a_proxy_that_served_traffic_leaves_no_task_behind():
    """The ordinary case, so a fix for the race cannot have broken the proxy's normal teardown."""
    async with SilentProxy() as proxy:
        reader, writer = await asyncio.open_connection(LOOPBACK, proxy.port)
        writer.write(b"PING\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(1), timeout=5)  # the broker's INFO arrives through it
        proxy.silent = True
        writer.write(b"PING\r\n")
        await writer.drain()
        writer.close()

    assert not proxy_tasks()
