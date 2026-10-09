"""`cross_namespace=True` on a service with no namespace is refused when discovery runs.

It subscribes `*.<pattern>`, and `*` is exactly one token: it matches a publisher in any namespace
and never one with no namespace, which publishes plain `<pattern>`. A service with no namespace
reads only publishers like that, so the listener started, held a subscription and never received an
event, and nothing said so. Discovery now refuses it, naming the handler, the subject and the two
ways out (set a namespace, or drop `cross_namespace`).
"""

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.decorators import validated_listener
from cliffracer.core.jetstream import subject_matches

pytestmark = pytest.mark.unit


class Evt(BaseModel):
    n: int


def _refused(service_class, **config) -> str:
    svc = service_class(ServiceConfig(name="svc", **config))
    with pytest.raises(ConfigurationError) as caught:
        svc._discover_handlers()
    return str(caught.value)


def test_a_cross_namespace_listener_without_a_namespace_is_refused_with_the_way_out():
    class S(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    message = _refused(S)

    assert "S.on_order" in message and "'orders.created'" in message, message
    assert "Set a namespace" in message and "drop cross_namespace=True" in message, message


def test_a_cross_namespace_validated_listener_without_a_namespace_is_refused_too():
    class S(CliffracerService):
        @validated_listener("orders.created", Evt, cross_namespace=True, fanout=True)
        async def on_order(self, message: Evt) -> None:
            pass

    assert "S.on_order" in _refused(S)


def test_a_subject_prefix_alone_is_not_a_namespace():
    class S(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    assert "has no namespace to span" in _refused(S, subject_prefix="prod")


def test_the_wildcard_is_why_it_could_never_work():
    """`*` is one token: the subscription matches any namespace and never a bare publisher."""
    assert subject_matches("*.orders.created", "east.orders.created")
    assert not subject_matches("*.orders.created", "orders.created")


def test_CONTROL_with_a_namespace_the_same_listener_subscribes_the_wildcard():
    class S(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()

    assert list(svc.container.registry.event_handlers) == ["*.orders.created"]


def test_CONTROL_a_local_listener_without_a_namespace_is_unchanged():
    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc"))
    svc._discover_handlers()

    assert list(svc.container.registry.event_handlers) == ["orders.created"]
