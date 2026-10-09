"""The guard against a test binding the fixed default health port runs in every run, broker or not.

`test_starting_a_service_does_not_bind_the_default_port` (in the metrics package) is the guard that
stops a test binding the default 8000 on a shared runner, and it reads the deciding thing: the
bound socket's own port. It is marked `nats_required` because `svc.start()` connects, so on a
machine with no broker it is skipped, and the protection disappears exactly where an accidental
bind of 8000 is most likely to collide with something. Nothing about the port needs a broker. This
starts the service with the broker steps stubbed, the way the health-port tests do, and reads the
same socket.
"""

from __future__ import annotations

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


async def _without_a_broker(svc: CliffracerService) -> CliffracerService:
    async def noop(*args, **kwargs):
        return None

    svc.container.connect = noop
    svc.container._setup_subscriptions = noop
    svc.container.disconnect = noop
    return svc


async def test_a_service_left_on_the_default_port_binds_another_under_the_suites_override():
    svc = await _without_a_broker(CliffracerService(ServiceConfig(name="port-probe")))
    assert svc.config.health_port == 8000, "the default this guard exists for"

    await svc.start()
    try:
        bound = svc.health_listener.port
        assert bound not in (None, 0, 8000), f"bound the default port: {bound}"
    finally:
        await svc.stop()


async def test_the_override_is_what_moved_it(monkeypatch):
    """The same service with the suite's override taken away asks for 8000, so the test above
    passes because of the override and not because the default stopped being 8000."""
    asked: list[int | None] = []
    real = HealthListener.start

    async def recording_start(self, *args, **kwargs):
        asked.append(self._test_port_override)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(HealthListener, "start", recording_start)
    svc = await _without_a_broker(CliffracerService(ServiceConfig(name="port-probe-2")))

    await svc.start()
    try:
        assert asked == [0], "the suite's autouse fixture sets the override to 0 for every test"
        assert svc.health_listener.port not in (None, 0, 8000)
    finally:
        await svc.stop()


def test_the_suites_autouse_fixture_sets_the_override_before_any_test_runs():
    assert HealthListener._test_port_override == 0
