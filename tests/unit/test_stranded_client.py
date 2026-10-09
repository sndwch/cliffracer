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


def _record_the_close_handler(svc) -> list[str]:
    """Replace what a close stops the service with, so a test reads whether it ran."""
    ran: list[str] = []

    async def recorded() -> None:
        ran.append("stop")

    svc.container.connection.on_closed_handler = recorded
    return ran


def test_a_close_during_stop_does_nothing():
    """A close while the service is already stopping does not stop it again.

    nats-py fires the closed callback from the `close()` that stop() itself
    calls. Re-entering stop() from there would wait on the lifecycle lock the
    running stop holds until the callback's own timeout gave up.
    """
    svc = _service()
    svc._running = False
    svc.nc = _FakeNats(is_closed=True)
    ran = _record_the_close_handler(svc)

    asyncio.run(svc.container.connection._closed_callback())

    assert ran == []


def test_CONTROL_a_close_while_running_does_reach_the_handler():
    """The recorder is wired: the same close on a running service runs it once."""
    svc = _service()
    svc._running = True
    svc.nc = _FakeNats(is_closed=True)
    ran = _record_the_close_handler(svc)

    asyncio.run(svc.container.connection._closed_callback())

    assert ran == ["stop"]


def test_the_stop_a_close_waits_for_is_bounded_at_ten_seconds():
    """The value the closed callback gives the service to stop in, before it gives up."""
    from cliffracer.core import connection

    assert connection._CLOSED_STOP_TIMEOUT == 10.0


async def test_a_close_whose_stop_hangs_is_abandoned_at_the_bound_and_logged(monkeypatch):
    """The broker dies while a shutdown is wedged. The callback must not wait for ever on a stop
    that never returns: it abandons it at the bound, cancels it, says so, and returns. With a bare
    `await` instead, this callback would be stuck for as long as the stop was."""
    from loguru import logger

    from cliffracer.core import connection

    monkeypatch.setattr(connection, "_CLOSED_STOP_TIMEOUT", 0.05)
    svc = _service()
    svc._running = True
    svc.nc = _FakeNats(is_closed=True)
    cancelled = False

    async def wedged_stop() -> None:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()  # never set
        except asyncio.CancelledError:
            cancelled = True
            raise

    svc.container.connection.on_closed_handler = wedged_stop
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        await asyncio.wait_for(svc.container.connection._closed_callback(), timeout=2)
    finally:
        logger.remove(sink)

    assert cancelled, "the wedged stop was abandoned but not cancelled"
    assert any("could not stop cleanly on connection close" in line for line in lines), lines


async def test_a_close_whose_stop_raises_is_logged_and_does_not_escape():
    from loguru import logger

    svc = _service()
    svc._running = True
    svc.nc = _FakeNats(is_closed=True)

    async def broken_stop() -> None:
        raise RuntimeError("teardown wedged")

    svc.container.connection.on_closed_handler = broken_stop
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        await svc.container.connection._closed_callback()  # must not raise
    finally:
        logger.remove(sink)

    assert any(
        "could not stop cleanly on connection close" in line and "teardown wedged" in line
        for line in lines
    ), lines


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
