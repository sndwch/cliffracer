"""The client's first dial is bounded, retries forever, and fails as a cliffracer error.

Three promises meet on one line. ADR-0008 says `max_reconnect_attempts`
defaults to `-1`, reconnect forever. ADR-0012 says that on the FIRST dial that
setting is precisely what makes nats-py hang, so `connect_timeout` bounds it and
startup fails predictably. And the client's documented surface says a caller
catches `RpcError`.

`ServiceClient` honoured none of the three: it passed neither bound to
`nats.connect`, so nats-py's own defaults applied -- 60 reconnect attempts two
seconds apart -- and a dial against a dead broker took about two minutes to
fail with a raw `nats.errors.NoServersError`, whatever timeout the caller had
configured. `ConnectionManager` has always done it correctly, so the ADRs were
kept by services and broken by the clients that call them.

The bound is asserted by RECORDING WHAT `asyncio.wait_for` RECEIVED rather than
by timing the call. A wall-clock assertion would pass on a fast machine for the
wrong reason and flake on a loaded one, and it cannot tell a bound that was
applied from a dial that happened to fail quickly.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import pytest
from nats.errors import NoServersError

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    CliffracerError,
    RpcClientError,
    RpcConnectionError,
    RpcError,
)
from tests.conftest import broker_url

pytestmark = pytest.mark.unit


def an_address_nothing_listens_on() -> str:
    """The suite's own broker host, on a port that was bound and released.

    The host follows `$CLIFFRACER_TEST_NATS_URL` like every other test rather
    than being pinned here; only the port is chosen, and it is chosen so that
    nothing is accepting on it. Asking the OS for a free port and closing it is
    not a guarantee for all time, but it holds for the length of a test and
    beats hard-coding a number another suite may be using.
    """
    host = urlsplit(broker_url()).hostname or "localhost"
    with socket.socket() as sock:
        sock.bind((host, 0))
        return f"nats://{host}:{int(sock.getsockname()[1])}"


@pytest.fixture
def recorded_bounds(monkeypatch):
    """Every timeout handed to `asyncio.wait_for` while the fixture is active."""
    seen: list[float | None] = []
    real = asyncio.wait_for

    async def recording(aw, timeout):
        seen.append(timeout)
        return await real(aw, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", recording)
    return seen


def _dialling_client(connect):
    """The client `cliffracer.core.dial` builds, whose `connect` is `connect`: the dial helper's own
    bound and cleanup run, and only nats-py's side of it is replaced."""
    return patch(
        "cliffracer.core.dial.nats.NATS",
        return_value=MagicMock(connect=connect, close=AsyncMock()),
    )


async def test_the_configured_connect_timeout_is_the_bound_the_dial_is_given(recorded_bounds):
    """The bound reaches `wait_for`, so it is the dial that is bounded."""
    client = ServiceClient(service="svc", nats_url=broker_url(), connect_timeout=7.5)

    with _dialling_client(AsyncMock()):
        await client._connection()

    assert 7.5 in recorded_bounds, recorded_bounds


async def test_a_dial_that_outlives_the_bound_raises_a_cliffracer_error():
    """A dial that never answers is cut off and reported as ours, not asyncio's."""

    async def never_answers(*args, **kwargs):
        await asyncio.sleep(3600)

    client = ServiceClient(service="svc", nats_url=broker_url(), connect_timeout=0.05)

    with _dialling_client(never_answers):
        with pytest.raises(RpcConnectionError) as caught:
            # The outer bound is a safety net two orders of magnitude above the
            # bound under test: it turns "the fix is missing" into a failure
            # rather than a hung suite. It is not what the test asserts.
            await asyncio.wait_for(client._connection(), 5.0)

    assert "0.05" in str(caught.value), str(caught.value)


async def test_an_unreachable_broker_is_reported_as_a_cliffracer_error():
    """nats-py's `NoServersError` does not escape the client."""
    client = ServiceClient(service="svc", nats_url=broker_url(), connect_timeout=5.0)

    with patch("cliffracer.core.dial.connect", side_effect=NoServersError()):
        with pytest.raises(RpcConnectionError):
            await client._connection()


async def test_a_port_nothing_listens_on_fails_within_the_bound_rather_than_after_sixty_retries(
    caplog,
):
    """The end-to-end shape of the defect: a real dial at a dead address."""
    # nats-py's default error callback prints the refused connection with a
    # traceback. It is expected here and would otherwise read as a suite error.
    logging.getLogger("nats").setLevel(logging.CRITICAL)
    client = ServiceClient(
        service="svc", nats_url=an_address_nothing_listens_on(), connect_timeout=0.25
    )

    with pytest.raises(CliffracerError):
        await asyncio.wait_for(client._connection(), 30.0)


async def test_the_client_reconnects_indefinitely_as_the_adr_says():
    """ADR-0008: `-1`, not nats-py's default of 60 attempts."""
    connect = AsyncMock(return_value=AsyncMock())
    client = ServiceClient(service="svc", nats_url=broker_url())

    with patch("cliffracer.core.dial.connect", connect):
        await client._connection()

    assert connect.await_args.kwargs["max_reconnect_attempts"] == -1


async def test_a_connect_timeout_of_none_leaves_the_dial_unbounded(recorded_bounds):
    """`None` disables the bound, the same spelling `ServiceConfig` documents."""
    client = ServiceClient(service="svc", nats_url=broker_url(), connect_timeout=None)

    with _dialling_client(AsyncMock()):
        await client._connection()

    assert recorded_bounds == [], recorded_bounds


async def test_the_default_bound_matches_the_service_side():
    """A client left alone bounds its dial the way `ServiceConfig` does."""
    from cliffracer.core.service_config import ServiceConfig

    assert ServiceClient(service="svc").connect_timeout == ServiceConfig(name="x").connect_timeout


def test_a_connect_failure_is_catchable_as_the_family_the_docs_name():
    """A caller caught `RpcError`; a new class must not walk out of that net."""
    assert issubclass(RpcConnectionError, RpcClientError)
    assert issubclass(RpcConnectionError, RpcError)
    assert issubclass(RpcConnectionError, CliffracerError)
