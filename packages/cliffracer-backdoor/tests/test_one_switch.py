"""Verify backdoor enabled configuration is controlled strictly by BackdoorConfig."""

import asyncio

import pytest
from cliffracer_backdoor import BackdoorExtension

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit

# Environment configurations to verify legacy flags are ignored.
ENVIRONMENTS = [
    pytest.param({}, id="nothing-set"),
    pytest.param({"CLIFFRACER_DISABLE_BACKDOOR": "1"}, id="disable-1"),
    pytest.param({"CLIFFRACER_DISABLE_BACKDOOR": "true"}, id="disable-true"),
    pytest.param({"CLIFFRACER_ENV": "production"}, id="env-production"),
    pytest.param(
        {"CLIFFRACER_DISABLE_BACKDOOR": "yes", "CLIFFRACER_ENV": "production"},
        id="both",
    ),
]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
async def test_enabled_true_starts_the_console_whatever_else_is_set(monkeypatch, environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    # Evaluated dynamically so BackdoorConfig reads the updated environment.
    class Svc(CliffracerService):
        backdoor = BackdoorExtension(enabled=True, port=0)

    svc = Svc(ServiceConfig(name="b"))
    await svc.container._setup_extensions()
    await svc.backdoor.start()
    try:
        port = svc.backdoor.health_details()["port"]
        assert isinstance(port, int) and port > 0, svc.backdoor.health_details()
        # Bound, not merely reported: the number has to answer.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
    finally:
        await svc.backdoor.stop()


@pytest.mark.parametrize("environment", ENVIRONMENTS)
async def test_the_default_starts_nothing_whatever_else_is_set(monkeypatch, environment):
    """Verify backdoor does not start when enabled is not set."""
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    class Svc(CliffracerService):
        backdoor = BackdoorExtension()

    svc = Svc(ServiceConfig(name="b"))
    await svc.container._setup_extensions()
    await svc.backdoor.start()
    try:
        assert svc.backdoor.health_details() == {"enabled": False}
        assert svc.backdoor._server is None, "nothing may be listening"
    finally:
        await svc.backdoor.stop()


def test_the_environment_switch_is_the_prefixed_one(monkeypatch):
    """Verify CLIFFRACER_BACKDOOR_ENABLED toggles extension enablement."""
    monkeypatch.setenv("CLIFFRACER_BACKDOOR_ENABLED", "true")
    assert BackdoorExtension().config.enabled is True

    monkeypatch.setenv("CLIFFRACER_BACKDOOR_ENABLED", "false")
    assert BackdoorExtension().config.enabled is False
