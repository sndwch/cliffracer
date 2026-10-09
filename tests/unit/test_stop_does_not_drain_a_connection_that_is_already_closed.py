"""The terminal-close callback runs inside nats-py's own close; the stop it starts must not drain.

nats-py awaits `closed_cb` from within `_close()`, so a stop that awaited the closed connection's
`drain()` would wait on the very call that is awaiting it. `ConnectionManager.disconnect` skips the
drain when the connection is already closed, and that skip is what makes the stop on a terminal
close finish. The decision record says so; these read what reaches the connection.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.connection import ConnectionManager

pytestmark = pytest.mark.unit


def _manager(**connection_state: bool) -> tuple[ConnectionManager, MagicMock]:
    manager = ConnectionManager(ServiceConfig(name="closing"))
    nc = MagicMock()
    nc.drain = AsyncMock()
    nc.close = AsyncMock()
    for name in ("is_closed", "is_draining", "is_connected", "is_connecting", "is_reconnecting"):
        setattr(nc, name, connection_state.get(name, False))
    manager.nc = nc
    return manager, nc


async def test_a_closed_connection_is_not_drained():
    manager, nc = _manager(is_closed=True)

    await manager.disconnect()

    nc.drain.assert_not_awaited()


async def test_a_connection_that_is_reconnecting_is_not_drained_either():
    manager, nc = _manager(is_reconnecting=True)

    await manager.disconnect()

    nc.drain.assert_not_awaited()
    nc.close.assert_awaited_once()


async def test_CONTROL_a_connected_connection_is_drained_then_closed():
    manager, nc = _manager(is_connected=True)

    await manager.disconnect()

    nc.drain.assert_awaited_once()
    nc.close.assert_awaited_once()
