"""cross_namespace listeners subscribe to *.{pattern}; default is namespace-local."""

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener


class Evt(BaseModel):
    id: str


@pytest.mark.unit
def test_local_listener_is_namespace_prefixed():
    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()
    assert "app1.orders.created" in svc.container.registry.event_handlers
    assert "orders.created" not in svc.container.registry.event_handlers


@pytest.mark.unit
def test_cross_namespace_listener_uses_wildcard():
    class S(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()
    assert "*.orders.created" in svc.container.registry.event_handlers


@pytest.mark.unit
def test_validated_listener_cross_namespace():
    class S(CliffracerService):
        @validated_listener("orders.created", Evt, cross_namespace=True, fanout=True)
        async def on_order(self, message: Evt):
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()
    assert "*.orders.created" in svc.container.registry.event_handlers
    # still registered in the schema map (validation still applies)
    assert any(s is Evt for s, _ in svc.container.registry.event_schemas.values())


@pytest.mark.unit
def test_backcompat_no_namespace_local_listener_unchanged():
    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc"))  # no namespace
    svc._discover_handlers()
    assert "orders.created" in svc.container.registry.event_handlers  # unchanged
