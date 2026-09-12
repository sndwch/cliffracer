"""Tests ensuring failed initial broker connections raise NatsError cleanly rather than exiting."""

import asyncio
from unittest.mock import patch

import pytest
from nats.errors import NoServersError

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def _cfg(**kw):
    return ServiceConfig(name="unreachable", nats_url="nats://127.0.0.1:14222", **kw)


def test_an_unreachable_broker_raises_nats_error():
    """Verify failed initial connect raises NatsError rather than terminating the process."""
    svc = CliffracerService(_cfg())
    with patch("cliffracer.core.connection.nats.connect", side_effect=NoServersError()):
        with pytest.raises(NoServersError):
            asyncio.run(svc.connect())


def test_the_failure_is_logged_before_raising():
    """Verify connection failure details are logged before raising error."""
    svc = CliffracerService(_cfg())
    logged = []
    svc.logger.error = lambda msg, *a, **k: logged.append(str(msg))
    with patch("cliffracer.core.connection.nats.connect", side_effect=NoServersError()):
        with pytest.raises(NoServersError):
            asyncio.run(svc.connect())
    assert any("could not reach NATS" in m for m in logged), logged
    assert any("14222" in m for m in logged), logged


def test_exit_on_closed_false_raises_cleanly():
    """Verify exit_on_closed=False also raises cleanly."""
    svc = CliffracerService(_cfg(exit_on_closed=False))
    with patch("cliffracer.core.connection.nats.connect", side_effect=NoServersError()):
        with pytest.raises(NoServersError):
            asyncio.run(svc.connect())


def test_a_successful_connect_does_not_raise():
    """The control. Verifies a successful connection establishes state."""
    svc = CliffracerService(_cfg())

    class _Nc:
        is_closed = False

        def jetstream(self):
            return object()

    async def _ok(*a, **k):
        return _Nc()

    with patch("cliffracer.core.connection.nats.connect", _ok):
        asyncio.run(svc.connect())
    assert svc.nc is not None
