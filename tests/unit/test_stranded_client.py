"""Unit tests verifying service health status and shutdown behavior when NATS connection closes."""

import asyncio

import pytest

from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


class _FakeNats:
    """Stands in for nats-py's client. Only `is_closed` is read here."""

    def __init__(self, is_closed: bool = False, is_connected: bool = True):
        self.is_connected = is_connected
        self.is_closed = is_closed


def _service(**cfg) -> CliffracerService:
    return CliffracerService(ServiceConfig(name="stranded-probe", **cfg))


# ---- status vocabulary -----------------------------------------------------


def test_a_running_service_with_an_open_connection_is_healthy():
    svc = _service()
    svc._running = True
    svc.nc = _FakeNats(is_closed=False, is_connected=True)

    health = asyncio.run(svc.health_check())

    assert health["status"] == "healthy"
    assert health["nats_connected"] is True


def test_a_running_service_whose_connection_is_closed_is_not_healthy():
    """Verify a running service reports disconnected status when NATS connection closes."""
    svc = _service()
    svc._running = True
    svc.nc = _FakeNats(is_closed=True)

    health = asyncio.run(svc.health_check())

    assert health["status"] == "disconnected"
    assert health["nats_connected"] is False


def test_a_stopped_service_still_reports_stopped_not_disconnected():
    """Verify an unstarted service reports stopped status."""
    svc = _service()
    svc._running = False
    svc.nc = None

    assert asyncio.run(svc.health_check())["status"] == "stopped"


def test_a_stopped_service_with_a_closed_connection_reports_stopped():
    """Verify a stopped service with closed connection reports stopped status."""
    svc = _service()
    svc._running = False
    svc.nc = _FakeNats(is_closed=True)

    assert asyncio.run(svc.health_check())["status"] == "stopped"


# ---- the closed callback ---------------------------------------------------


def test_a_close_while_running_stops_the_service_without_exiting():
    """Verify unexpected connection loss while running stops the service cleanly without process exit."""
    svc = _service()
    svc._running = True
    svc.nc = _FakeNats(is_closed=True)

    asyncio.run(svc.container.connection._closed_callback())

    assert not svc._running
    assert asyncio.run(svc.health_check())["status"] == "stopped"


def test_a_close_during_stop_does_nothing():
    """Verify intentional connection closure during stop does nothing."""
    svc = _service()
    svc._running = False
    svc.nc = _FakeNats(is_closed=True)

    asyncio.run(svc.container.connection._closed_callback())

    assert not svc._running


def test_exit_on_closed_false_changes_status_and_leaves_service_up():
    """Verify exit_on_closed=False updates health status to disconnected and keeps service running."""
    svc = _service(exit_on_closed=False)
    svc._running = True
    svc.nc = _FakeNats(is_closed=True)

    asyncio.run(svc.container.connection._closed_callback())

    assert svc._running
    assert asyncio.run(svc.health_check())["status"] == "disconnected"


# ---- defaults --------------------------------------------------------------


def test_reconnect_is_unlimited_by_default():
    """Verify default reconnect attempts is unlimited (-1)."""
    assert ServiceConfig(name="d").max_reconnect_attempts == -1


def test_reconnect_time_wait_is_unchanged():
    """Verify default reconnect interval is 2 seconds."""
    assert ServiceConfig(name="d").reconnect_time_wait == 2


def test_exit_on_closed_defaults_to_true():
    assert ServiceConfig(name="d").exit_on_closed is True
