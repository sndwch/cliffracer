"""Unit tests for ServiceClient initialization and attributes."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from cliffracer.client import ServiceClient


@pytest.mark.unit
def test_client_init_defaults():
    client = ServiceClient(service="test_svc")
    assert client.service == "test_svc"
    assert client._nc is None
    assert client._owns_nc is True
    assert isinstance(client._connect_lock, asyncio.Lock)


@pytest.mark.unit
def test_client_init_with_connection():
    mock_nc = MagicMock()
    client = ServiceClient(nc=mock_nc, service="test_svc")
    assert client._nc is mock_nc
    assert client._owns_nc is False
    assert isinstance(client._connect_lock, asyncio.Lock)
