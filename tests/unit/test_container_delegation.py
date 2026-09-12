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
    assert svc.container._entrypoint_kinds is svc.container._entrypoint_kinds


def test_the_container_exists_before_extensions_bind():
    """Verify container is initialized before extension collection occurs."""
    svc = CliffracerService(ServiceConfig(name="s"))
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
