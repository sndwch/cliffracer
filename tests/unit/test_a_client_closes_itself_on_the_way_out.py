"""`async with` gives an owned connection back, and a borrowed one is left alone.

Constructed without an `nc`, a `ServiceClient` opens and owns a NATS connection,
and `close()` was the only way to return it. There was no `__aenter__` /
`__aexit__`, so the correct usage was a try/finally the docs never showed --
`docs/api-reference.md` demonstrated constructing a client and never closing it.

THE SEMANTICS ARE `close()`'s, NOT NEW ONES. Closing means this: a
client that owned its connection drains it and then refuses later use, because
`close()` is an intent to stop; a client handed a connection it did not open
leaves that connection alone and keeps working, because ending someone else's
connection was never its to do. `__aexit__` calls `close()`, so both halves
follow, and the second is the one worth a control -- a context manager that
drained a borrowed connection would be worse than no context manager.

ENTERING CONNECTS. The client is lazy by construction, which means a bad address
surfaces on the first call rather than at the top of the block. Inside
`async with`, the block's first line is where a caller expects to find out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcConnectionError
from tests.conftest import broker_url

pytestmark = pytest.mark.unit


def _open_connection() -> AsyncMock:
    nc = AsyncMock()
    nc.is_closed = False
    return nc


async def test_entering_returns_the_client_itself():
    """`async with C(...) as c` has to bind the client, not the connection."""
    nc = _open_connection()
    client = ServiceClient(nc, service="svc", verify=False)

    async with client as entered:
        assert entered is client


async def test_entering_opens_the_connection():
    """So a bad address is reported at the `async with`, not at the first call."""
    nc = _open_connection()
    client = ServiceClient(service="svc", nats_url=broker_url(), verify=False)
    from unittest.mock import patch

    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=nc)) as connect:
        async with client:
            connect.assert_awaited_once()


async def test_leaving_drains_a_connection_the_client_opened():
    """The whole point: an owned connection is given back."""
    nc = _open_connection()
    from unittest.mock import patch

    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=nc)):
        async with ServiceClient(service="svc", nats_url=broker_url(), verify=False):
            pass

    nc.drain.assert_awaited_once()


async def test_the_client_refuses_use_after_the_block():
    """`close()`'s semantics, reached through the context manager."""
    nc = _open_connection()
    from unittest.mock import patch

    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=nc)):
        async with ServiceClient(service="svc", nats_url=broker_url(), verify=False) as client:
            pass

    with pytest.raises(RpcConnectionError):
        await client._connection()


async def test_the_connection_is_given_back_even_when_the_block_raises():
    """A `finally` is what a caller writes this to avoid writing."""
    nc = _open_connection()
    from unittest.mock import patch

    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=nc)):
        with pytest.raises(ZeroDivisionError):
            async with ServiceClient(service="svc", nats_url=broker_url(), verify=False):
                raise ZeroDivisionError("the block failed")

    nc.drain.assert_awaited_once()


# --- the control that matters -----------------------------------------------


async def test_CONTROL_a_borrowed_connection_survives_the_block():
    """A context manager that drained someone else's connection would be worse
    than none. `close()` already leaves a borrowed connection alone; this is
    that promise reached through `async with`."""
    nc = _open_connection()
    client = ServiceClient(nc, service="svc", verify=False)

    async with client:
        pass

    nc.drain.assert_not_awaited()
    assert await client._connection() is nc
