"""Tests ensuring failed initial broker connections raise NatsError cleanly rather than exiting."""

import asyncio
from unittest.mock import patch

import pytest
from nats.errors import NoServersError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.container import BrokerConnectionState

pytestmark = pytest.mark.unit


def _cfg(**kw):
    return ServiceConfig(name="unreachable", nats_url="nats://127.0.0.1:14222", **kw)


def test_an_unreachable_broker_raises_nats_error():
    """Verify failed initial connect raises NatsError rather than terminating the process."""
    svc = CliffracerService(_cfg())
    with patch("cliffracer.core.dial.connect", side_effect=NoServersError()):
        with pytest.raises(NoServersError):
            asyncio.run(svc.connect())


def test_the_failure_is_logged_before_raising():
    """Verify connection failure details are logged before raising error."""
    svc = CliffracerService(_cfg())
    logged = []
    svc.logger.error = lambda msg, *a, **k: logged.append(str(msg))
    with patch("cliffracer.core.dial.connect", side_effect=NoServersError()):
        with pytest.raises(NoServersError):
            asyncio.run(svc.connect())
    assert any("could not reach NATS" in m for m in logged), logged
    assert any("14222" in m for m in logged), logged


def test_a_successful_connect_establishes_the_state_connect_is_for():
    """The control. A connect that succeeds leaves the service connected, with its JetStream
    context, after telling the operator and calling `on_connect`.

    Each of those is something `ConnectionManager.connect` does AFTER `self.nc = ...`, and
    `svc.nc is not None` is true of the stub whatever it does next.
    """
    connected = []
    svc = CliffracerService(
        _cfg(jetstream_enabled=True, on_connect=lambda: connected.append("on_connect"))
    )
    logged = []
    svc.logger.info = lambda msg, *a, **k: logged.append(str(msg))
    context = object()

    class _Nc:
        is_closed = False
        is_connected = True

        def jetstream(self):
            return context

    stub = _Nc()

    async def _ok(*a, **k):
        return stub

    with patch("cliffracer.core.dial.connect", _ok):
        asyncio.run(svc.connect())

    assert svc.nc is stub
    assert svc.container.connection.js is context
    assert svc.container.connection.broker_state is BrokerConnectionState.CONNECTED
    assert connected == ["on_connect"]
    assert any("connected to NATS" in m and "14222" in m for m in logged), logged


def test_CONTROL_without_jetstream_enabled_no_context_is_made():
    """The other side of the JetStream line above: it is `jetstream_enabled` that makes one."""
    svc = CliffracerService(_cfg())

    class _Nc:
        is_closed = False
        is_connected = True

        def jetstream(self):
            raise AssertionError("a JetStream context was made for a service that did not ask")

    async def _ok(*a, **k):
        return _Nc()

    with patch("cliffracer.core.dial.connect", _ok):
        asyncio.run(svc.connect())

    assert svc.container.connection.js is None
