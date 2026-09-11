"""Tests verifying status and nats_connected health payload contract."""

import pytest

from cliffracer import CliffracerService, ServiceConfig


# Evaluates whether the payload indicates a healthy, connected service.
def fleet_predicate(payload: dict) -> bool:
    return payload.get("status") == "healthy" and payload.get("nats_connected") is True


CONTRACT_KEYS = ("status", "nats_connected")


@pytest.mark.unit
async def test_the_two_keys_the_fleet_reads_are_present():
    svc = CliffracerService(ServiceConfig(name="probe"))
    payload = await svc.health_check()
    for key in CONTRACT_KEYS:
        assert key in payload, f"{key!r} missing from health check payload"


@pytest.mark.unit
async def test_the_fleet_predicate_says_no_during_reconnection_outage():
    """Verify healthcheck reports nats_connected False during reconnection."""
    svc = CliffracerService(ServiceConfig(name="probe"))
    svc._running = True
    svc.nc = type(
        "C",
        (),
        {"is_closed": False, "is_connected": False, "is_reconnecting": True},
    )()
    payload = await svc.health_check()
    assert payload["status"] == "connecting"
    assert payload["nats_connected"] is False
    assert fleet_predicate(payload) is False, payload


@pytest.mark.unit
async def test_the_fleet_predicate_says_yes_when_connected():
    """Verify payload status is "healthy" when connected."""
    svc = CliffracerService(ServiceConfig(name="probe"))
    svc._running = True
    svc.nc = type("C", (), {"is_closed": False, "is_connected": True})()
    payload = await svc.health_check()
    assert payload["status"] == "healthy"
    assert fleet_predicate(payload) is True


@pytest.mark.unit
async def test_the_fleet_predicate_says_no_before_connect_and_after_close():
    """Verify stopped and disconnected services are reported as unhealthy."""
    svc = CliffracerService(ServiceConfig(name="probe"))

    svc.nc = None  # stopped: before start()
    stopped = await svc.health_check()
    assert stopped["status"] == "stopped"
    assert fleet_predicate(stopped) is False, stopped

    svc._running = True
    svc.nc = type("C", (), {"is_closed": True})()  # disconnected
    disconnected = await svc.health_check()
    assert disconnected["status"] == "disconnected"
    assert fleet_predicate(disconnected) is False, disconnected
