"""cross_namespace listeners subscribe to *.{pattern}; default is namespace-local."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.dispatch.events import DispatchOutcome

pytestmark = pytest.mark.unit


class Evt(BaseModel):
    id: str


def test_local_listener_is_namespace_prefixed():
    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()
    assert "app1.orders.created" in svc.container.registry.event_handlers
    assert "orders.created" not in svc.container.registry.event_handlers


def test_cross_namespace_listener_uses_wildcard():
    class S(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()
    assert "*.orders.created" in svc.container.registry.event_handlers


def test_validated_listener_cross_namespace():
    class S(CliffracerService):
        @validated_listener("orders.created", Evt, cross_namespace=True, fanout=True)
        async def on_order(self, message: Evt):
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()
    assert "*.orders.created" in svc.container.registry.event_handlers
    # Registered under that wildcard subject, so it is that subject's schema the dispatcher reads.
    # (Keyed by the subject, and written whether or not the listener is cross-namespace: that a
    # schema exists says nothing about validation. The tests below send payloads.)
    assert svc.container.registry.event_schemas["*.orders.created"][0] is Evt


def _validated_service(on_invalid):
    class S(CliffracerService):
        received: list

        @validated_listener(
            "orders.created", Evt, cross_namespace=True, fanout=True, on_invalid=on_invalid
        )
        async def on_order(self, message: Evt):
            self.received.append(message.id)

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc.received = []
    svc.nc = AsyncMock()
    svc._discover_handlers()
    return svc


def _message(subject: str, data: bytes):
    msg = AsyncMock()
    msg.subject = subject
    msg.data = data
    msg.headers = {"Content-Type": "application/json"}
    return msg


@pytest.mark.asyncio
async def test_a_valid_payload_from_another_namespace_reaches_the_handler_as_the_model():
    svc = _validated_service("deadletter")

    outcome = await svc.container.event_dispatcher.handle_event(
        _message("app2.orders.created", b'{"id": "o-1"}')
    )

    assert outcome == DispatchOutcome.OK
    assert svc.received == ["o-1"]
    svc.nc.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_invalid_payload_from_another_namespace_is_dead_lettered_not_handled():
    svc = _validated_service("deadletter")

    outcome = await svc.container.event_dispatcher.handle_event(
        _message("app2.orders.created", b'{"id": ["not", "a", "string"]}')
    )

    assert outcome == DispatchOutcome.INVALID
    assert svc.received == []
    svc.nc.publish.assert_awaited_once()
    envelope = json.loads(svc.nc.publish.await_args.args[1])
    assert envelope["original_subject"] == "app2.orders.created"
    assert [e["loc"] for e in envelope["errors"]] == [["id"]]


@pytest.mark.asyncio
async def test_an_invalid_payload_is_dropped_when_the_listener_says_drop():
    svc = _validated_service("drop")

    outcome = await svc.container.event_dispatcher.handle_event(
        _message("app2.orders.created", b'{"id": ["not", "a", "string"]}')
    )

    assert outcome == DispatchOutcome.INVALID
    assert svc.received == []
    svc.nc.publish.assert_not_awaited()


def test_backcompat_no_namespace_local_listener_unchanged():
    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc"))  # no namespace
    svc._discover_handlers()
    assert "orders.created" in svc.container.registry.event_handlers  # unchanged
