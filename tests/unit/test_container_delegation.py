"""Tests verifying CliffracerService delegates connection and dispatch state to Container."""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.container import Container
from tests.conftest import declared

pytestmark = pytest.mark.unit


def test_the_service_owns_a_container_and_delegates_state():
    svc = CliffracerService(ServiceConfig(name="s"))
    assert isinstance(svc.container, Container)
    assert svc.container.service is svc

    # Ensure nc property delegates to container state.
    assert svc.nc is None and svc.container.nc is None
    sentinel = object()
    svc.nc = sentinel
    assert svc.container.nc is sentinel
    assert svc.nc is sentinel

    # Ensure internal handler and extension registries share identical references.
    assert svc.container._rpc_handlers is svc.container.registry.rpc_handlers
    assert svc.container._event_handlers is svc.container.registry.event_handlers
    assert svc.container._extensions is svc.container.extensions


def test_discovery_is_handed_the_containers_own_registries(monkeypatch):
    """The registry and extensions discovery reads are the container's."""
    from cliffracer.core.discovery import HandlerDiscovery

    svc = CliffracerService(ServiceConfig(name="s"))
    handed: dict = {}
    real = HandlerDiscovery.discover

    def spy(*args, **kwargs):
        handed.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(HandlerDiscovery, "discover", staticmethod(spy))
    svc._discover_handlers()

    assert handed["registry"] is svc.container.registry
    assert handed["extensions"] is svc.container.extensions


def test_the_container_exists_before_extensions_bind(monkeypatch):
    """The container is built first, and every extension is bound through it afterwards.

    Recorded at the two calls that matter, so a service that bound an extension before its
    container existed fails HERE with the order, not as an `AttributeError` in construction.
    """
    events: list[str] = []
    real_init = Container.__init__
    real_bind = Container._bind_extension

    def recording_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        events.append("container")

    def recording_bind(self, ext, name):
        events.append(f"bind:{name}")
        return real_bind(self, ext, name)

    monkeypatch.setattr(Container, "__init__", recording_init)
    monkeypatch.setattr(Container, "_bind_extension", recording_bind)

    svc = CliffracerService(ServiceConfig(name="s"))

    assert events[0] == "container", events
    assert events[1:] == ["bind:_correlation", "bind:_validation"], events
    assert [e.name for e in svc._extensions] == ["_correlation", "_validation"]
    assert declared(svc) == []


def test_handlers_registered_through_the_service_land_in_the_container():
    """Ensure handlers registered through the service land in the container."""
    from cliffracer import rpc

    class Svc(CliffracerService):
        @rpc
        async def echo(self, value: str) -> str:
            return value

    svc = Svc(ServiceConfig(name="s"))
    svc._discover_handlers()
    assert "echo" in svc.container.registry.rpc_handlers
    assert svc.container._rpc_handlers["echo"] is svc.container.registry.rpc_handlers["echo"]
