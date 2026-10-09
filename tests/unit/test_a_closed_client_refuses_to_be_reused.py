"""A client that was closed says so, instead of raising a raw nats error.

`close()` drained the connection and left `self._nc` in place. `_connection()`
reopens only `if self._nc is None`, so a closed client handed back the drained
connection forever: the next call reached `nc.request` on it and raised
`nats.errors.ConnectionClosedError`, which `_request` did not catch -- it
handled only `NatsTimeout` and `NoResponders` -- so a raw nats class escaped
past the documented mapping. There was no test for `close()` at all.

REUSE AFTER close() REFUSES; IT DOES NOT RECONNECT. An explicit `close()` is an
intent to stop, and every comparable Python resource -- a file, a socket, a
database connection -- raises on use-after-close rather than quietly opening a
new one. A client that reconnected would make `close()` useless for releasing
anything, and turn close/call/close/call into a connection leak that reads as
working code. This is a coordinator ruling on a public API semantic, recorded
on the pull request; it is reversible, and the tests below are what would have
to change.

A BORROWED CONNECTION IS NOT AFFECTED. `close()` already leaves a connection it
did not open alone, so a client constructed with one keeps working afterwards.
That half is a control here, because a refusal keyed on the wrong flag would
break it and still pass every other test in this module.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from nats.errors import ConnectionClosedError, StaleConnectionError

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcClientError, RpcConnectionError
from tests.conftest import broker_url

pytestmark = pytest.mark.unit


def an_open_connection() -> AsyncMock:
    nc = AsyncMock()
    nc.is_closed = False
    return nc


async def a_connected_client() -> tuple[ServiceClient, AsyncMock]:
    """A client that owns a connection it has already opened."""
    nc = an_open_connection()
    client = ServiceClient(service="svc", nats_url=broker_url(), verify=False)
    with patch("cliffracer.core.dial.connect", AsyncMock(return_value=nc)):
        await client._connection()
    return client, nc


# --- use after close refuses, by name ---------------------------------------


async def test_a_call_after_close_says_the_client_was_closed():
    """The message has to name the client, or a caller cannot tell what closed."""
    client, _ = await a_connected_client()
    await client.close()

    with pytest.raises(RpcConnectionError) as caught:
        await client._request("svc.rpc.do", b"{}", {})

    message = str(caught.value)
    assert "closed" in message, message
    assert "svc" in message, message


async def test_the_refusal_happens_before_the_connection_is_touched():
    """A closed client must not reach the drained connection at all."""
    client, nc = await a_connected_client()
    await client.close()
    nc.request.reset_mock()

    with pytest.raises(RpcConnectionError):
        await client._connection()

    nc.request.assert_not_awaited()


async def test_closing_a_client_that_never_connected_also_refuses():
    """`close()` means stop, whether or not a connection was ever opened.

    The dial helper is patched to fail rather than merely watched: the refusal
    has to come before any dial, and without the fix this test reached a real
    broker -- which the suite's task-leak guard caught, since a client that was
    closed had quietly opened a live connection.
    """
    client = ServiceClient(service="svc", nats_url=broker_url(), verify=False)
    await client.close()

    never = AsyncMock(side_effect=AssertionError("a closed client dialled the broker"))
    with patch("cliffracer.core.dial.connect", never):
        with pytest.raises(RpcConnectionError):
            await client._connection()


async def test_close_can_be_called_twice():
    """Closing an already-closed client is not an error."""
    client, _ = await a_connected_client()
    await client.close()
    await client.close()


# --- the controls -----------------------------------------------------------


async def test_CONTROL_a_fresh_client_still_connects():
    """Without this, "refuses" could mean "always refuses"."""
    client, nc = await a_connected_client()
    assert await client._connection() is nc


async def test_CONTROL_a_borrowed_connection_survives_a_close():
    """`close()` leaves a connection it did not open alone, so reuse still works.

    A refusal keyed on the wrong flag -- on close having been called at all,
    rather than on this client owning what it closed -- would break this and
    pass every other test here.
    """
    nc = an_open_connection()
    client = ServiceClient(nc, service="svc", verify=False)

    await client.close()

    nc.drain.assert_not_awaited()
    assert await client._connection() is nc


# --- a connection that closes underneath a call -----------------------------


@pytest.mark.parametrize(
    "raised", [ConnectionClosedError(), StaleConnectionError()], ids=["closed", "stale"]
)
async def test_a_connection_lost_under_a_call_is_reported_as_ours(raised):
    """These escaped the documented mapping as raw nats classes."""
    client, nc = await a_connected_client()
    nc.request = AsyncMock(side_effect=raised)

    with pytest.raises(RpcConnectionError) as caught:
        await client._request("svc.rpc.do", b"{}", {})

    assert "svc.rpc.do" in str(caught.value), str(caught.value)
    assert isinstance(caught.value, RpcClientError)


async def test_a_working_call_is_untouched_by_the_new_handling():
    """The control for the mapping half: a normal reply still comes back."""
    client, nc = await a_connected_client()
    nc.request = AsyncMock(
        return_value=SimpleNamespace(data=json.dumps({"success": True}).encode(), headers=None)
    )

    reply = await client._request("svc.rpc.do", b"{}", {})

    assert json.loads(reply.data)["success"] is True
