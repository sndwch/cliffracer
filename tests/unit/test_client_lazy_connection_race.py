"""Tests for concurrent lazy connection initialization in ServiceClient.

Verifies that concurrent callers to `ServiceClient._connection()` serialize
around `asyncio.Lock`, opening exactly one NATS connection.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer.client import ServiceClient


@pytest.mark.unit
async def test_concurrent_connection_calls_open_single_connection():
    """Simultaneous connection requests must open exactly one connection."""
    connect_calls = 0
    created_connections = []

    async def fake_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        # Yield to event loop so other concurrent tasks can run
        await asyncio.sleep(0.01)
        nc = AsyncMock()
        nc.is_closed = False
        created_connections.append(nc)
        return nc

    client = ServiceClient(service="test_svc", nats_url="nats://localhost:4222", verify=False)
    assert client._nc is None

    with patch("cliffracer.client.nats.connect", side_effect=fake_connect):
        conns = await asyncio.gather(*[client._connection() for _ in range(10)])

    assert connect_calls == 1
    assert len(created_connections) == 1
    assert all(c is created_connections[0] for c in conns)
    assert client._nc is created_connections[0]

    # Closing client should cleanly drain the single connection
    await client.close()
    created_connections[0].drain.assert_awaited_once()


@pytest.mark.unit
async def test_subsequent_calls_reuse_existing_connection_without_lock_contention():
    """Once connected, _connection() returns the cached connection without calling connect."""
    mock_nc = AsyncMock()
    mock_nc.is_closed = False

    client = ServiceClient(service="test_svc", verify=False)

    with patch("cliffracer.client.nats.connect", AsyncMock(return_value=mock_nc)) as mock_connect:
        c1 = await client._connection()
        c2 = await client._connection()
        c3 = await client._connection()

    assert mock_connect.call_count == 1
    assert c1 is mock_nc
    assert c2 is mock_nc
    assert c3 is mock_nc


@pytest.mark.unit
async def test_pre_supplied_connection_is_reused_without_connecting():
    """When a connection is provided to __init__, _connection() returns it immediately."""
    existing_nc = AsyncMock()
    client = ServiceClient(nc=existing_nc, service="test_svc", verify=False)

    with patch("cliffracer.client.nats.connect") as mock_connect:
        conns = await asyncio.gather(*[client._connection() for _ in range(5)])

    mock_connect.assert_not_called()
    assert all(c is existing_nc for c in conns)


@pytest.mark.unit
async def test_connection_failure_releases_lock_allowing_retry():
    """If nats.connect() fails, the lock is released so a future attempt can retry."""
    attempts = 0

    async def flaky_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.01)
        if attempts == 1:
            raise ConnectionError("Broker unreachable")
        nc = AsyncMock()
        nc.is_closed = False
        return nc

    client = ServiceClient(service="test_svc", verify=False)

    with patch("cliffracer.client.nats.connect", side_effect=flaky_connect):
        with pytest.raises(ConnectionError, match="Broker unreachable"):
            await client._connection()

        # Lock was cleanly released, so second attempt succeeds
        nc = await client._connection()

    assert attempts == 2
    assert nc is not None
    assert client._nc is nc


@pytest.mark.unit
async def test_concurrent_verify_calls_open_single_connection():
    """Concurrent public calls (like verify) that lazily connect must also share one connection."""
    connect_calls = 0

    class MockReply:
        def __init__(self, data: bytes):
            self.data = data

    async def fake_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(0.01)
        nc = AsyncMock()
        nc.is_closed = False
        desc_payload = json.dumps(
            {
                "service": "test_svc",
                "version": "1.0",
                "description_hash": "sha256:abc",
                "methods": [],
            }
        ).encode()
        nc.request.return_value = MockReply(desc_payload)
        return nc

    client = ServiceClient(service="test_svc", verify=True)

    with patch("cliffracer.client.nats.connect", side_effect=fake_connect):
        await asyncio.gather(*[client.verify() for _ in range(5)])

    assert connect_calls == 1
    assert client._verified is True
