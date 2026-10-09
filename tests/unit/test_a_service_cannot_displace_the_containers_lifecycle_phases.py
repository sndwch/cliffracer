"""A service subclass that defines a method named like a lifecycle phase does not switch the phase off.

The container runs extension setup, extension start, subscribing, stopping timers and stopping
extensions. It used to find each of them by name on the service first, so a subclass that happened
to define `_setup_extensions` (or one of the other four) replaced the container's supervision with
its own method: its declared extensions were never set up, started or stopped, with no error and no
warning. The container now runs its own phases, and only those.
"""

from __future__ import annotations

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, SharedDependency
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit

# The name a service might collide with, and the container attribute that is the real phase.
PHASES = {
    "_setup_extensions": "_setup_extensions",
    "_start_extensions": "_start_extensions",
    "_setup_subscriptions": "setup_subscriptions",
    "_stop_timers": "_stop_timers",
    "_stop_extensions": "_stop_extensions",
}


class Recording(Extension):
    def __init__(self, log: list[str]):
        self.log = log

    async def setup(self, ctx):
        self.log.append("ext.setup")

    async def start(self):
        self.log.append("ext.start")

    async def stop(self):
        self.log.append("ext.stop")


class _FakeNats:
    is_connected = True
    is_closed = False
    is_draining = False
    is_connecting = False


def _colliding(log: list[str], names: tuple[str, ...], base: type = CliffracerService):
    """A service class defining each of `names` as a method that only records that it ran."""

    def method(name: str):
        async def recorder(self) -> None:
            log.append(f"service.{name}")

        recorder.__name__ = name
        return recorder

    body = {name: method(name) for name in names}
    body["ext"] = Recording(SharedDependency(log))  # shared: arguments are copied per service

    async def connect(self) -> None:
        self.nc = _FakeNats()

    async def disconnect(self) -> None:
        return None

    body["connect"] = connect
    body["disconnect"] = disconnect
    return type("Colliding", (base,), body)


async def _no_subscriptions() -> None:
    return None


def _spy(svc: CliffracerService, attr: str, log: list[str]) -> None:
    """Wrap a container phase so the log shows that the container ran it.

    Subscribing needs a real client, so it is replaced by a no-op first on every service here.
    """
    svc.container.setup_subscriptions = _no_subscriptions  # type: ignore[method-assign]
    real = getattr(svc.container, attr)

    async def spy() -> None:
        log.append(f"container.{attr}")
        await real()

    setattr(svc.container, attr, spy)


async def test_a_service_defining_every_phase_name_still_has_its_extensions_supervised():
    log: list[str] = []
    svc = _colliding(log, tuple(PHASES))(ServiceConfig(name="collide", health_port=0))
    _spy(svc, "setup_subscriptions", log)

    await svc.start()
    await svc.stop()

    assert [e for e in log if e.startswith("ext.")] == ["ext.setup", "ext.start", "ext.stop"]
    assert not [e for e in log if e.startswith("service.")], log


@pytest.mark.parametrize("name", PHASES)
async def test_the_container_runs_its_own_phase_whatever_the_service_defines(name):
    log: list[str] = []
    svc = _colliding(log, (name,))(ServiceConfig(name="collide", health_port=0))
    _spy(svc, PHASES[name], log)

    await svc.start()
    await svc.stop()

    assert f"container.{PHASES[name]}" in log, f"the container did not run {PHASES[name]}: {log}"
    assert f"service.{name}" not in log, f"the service's {name} ran in place of the phase: {log}"


async def test_CONTROL_the_test_tier_mixin_does_put_a_service_method_in_place_of_a_phase():
    """Otherwise the two tests above would pass if nothing could ever take the place of a phase."""
    log: list[str] = []
    cls = _colliding(
        log, ("_stop_timers",), base=type("Base", (ServicePhases, CliffracerService), {})
    )
    svc = cls(ServiceConfig(name="collide", health_port=0))
    svc.container.setup_subscriptions = _no_subscriptions  # type: ignore[method-assign]

    await svc.start()
    await svc.stop()

    assert "service._stop_timers" in log
