"""Services started over the in-memory broker talk to each other as they would over a server.

Each service here runs its own `start()` and `stop()` through `ServiceTestHarness(broker=...)`,
over its own connection to one broker: the RPC one service makes reaches the other by subject, the
event the other publishes reaches the first by subscription, and nothing is dispatched by hand.
"""

from __future__ import annotations

import socket
from typing import Any

import nats.errors
import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import ServiceTestHarness, wait_until

from .conftest import Connection, started

pytestmark = pytest.mark.unit


class Billing(CliffracerService):
    charged: list[str]

    @rpc
    async def charge(self, order_id: str, amount: float) -> dict[str, str]:
        self.charged.append(order_id)
        await self.publish_event("billing.charged", order_id=order_id, amount=amount)
        return {"order_id": order_id, "charged": str(amount)}


class Orders(CliffracerService):
    confirmed: list[str]

    @rpc
    async def place(self, order_id: str) -> dict[str, str]:
        result: dict[str, str] = await self.call_rpc(
            "billing", "charge", order_id=order_id, amount=9.5
        )
        return result

    @listener("billing.charged", fanout=True)
    async def on_charged(self, order_id: str, amount: float) -> None:
        self.confirmed.append(order_id)


def _accepts_a_connection(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def test_an_rpc_and_the_event_it_causes_cross_between_two_started_services(
    transport_service_factory: Any, mock_transport: Connection
) -> None:
    """Orders calls Billing by subject; the event Billing publishes reaches Orders' listener."""
    billing = transport_service_factory(Billing, name="billing")
    orders = transport_service_factory(Orders, name="orders")
    billing.charged, orders.confirmed = [], []

    async with started(mock_transport, billing, orders):
        reply = await mock_transport.request(
            "orders.rpc.place", b'{"order_id": "o-1"}', timeout=5.0
        )
        await wait_until(lambda: orders.confirmed, within=5.0, reason="Orders to hear the charge")

    assert billing.charged == ["o-1"]
    assert orders.confirmed == ["o-1"]
    assert b'"charged":"9.5"' in reply.data.replace(b" ", b""), reply.data
    # Each service dialled the bus through the harness, on a connection of its own.
    assert billing.nc is not orders.nc


async def test_two_replicas_of_a_service_share_its_rpc_queue_group(
    transport_service_factory: Any, mock_transport: Connection
) -> None:
    """Every RPC reaches one replica, as a broker's queue group hands it to one member."""
    first = transport_service_factory(Billing, name="billing")
    second = transport_service_factory(Billing, name="billing")
    first.charged, second.charged = [], []

    async with started(mock_transport, first, second):
        for n in range(6):
            await mock_transport.request(
                "billing.rpc.charge", f'{{"order_id": "o-{n}", "amount": 1}}'.encode(), timeout=5.0
            )

    assert sorted(first.charged + second.charged) == [f"o-{n}" for n in range(6)]
    assert first.charged and second.charged, (first.charged, second.charged)


async def test_a_started_service_holds_one_health_socket_and_teardown_releases_it(
    transport_service_factory: Any, mock_transport: Connection
) -> None:
    """A harness over a broker runs the health listener too: one ephemeral port per service,
    open while it runs, and none open once the harnesses are torn down."""
    services = [
        transport_service_factory(Billing, name="billing"),
        transport_service_factory(Orders, name="orders"),
    ]
    for svc in services:
        svc.charged = svc.confirmed = []

    async with started(mock_transport, *services):
        ports = [svc.health_listener.port for svc in services]
        assert all(ports) and len(set(ports)) == 2, ports
        assert [p for p in ports if _accepts_a_connection(p)] == ports

    assert [p for p in ports if _accepts_a_connection(p)] == []


async def test_a_stopped_service_releases_its_connection_and_subscriptions(
    transport_service_factory: Any, mock_transport: Connection
) -> None:
    """Stopping leaves the service's own connection closed and the broker with nothing it held."""
    svc = transport_service_factory(Billing, name="billing")
    svc.charged = []

    async with started(mock_transport, svc):
        held = svc.nc
        assert held.subscriptions, "the started service subscribed nothing"
        assert held.broker is mock_transport.broker

    assert held.is_closed
    assert held.subscriptions == ()
    # Nothing answers the subject any more: the broker says so at once, as a server does.
    with pytest.raises(nats.errors.NoRespondersError):
        await mock_transport.request("billing.rpc.charge", b"{}", timeout=5.0)


async def test_a_jetstream_service_cannot_start_on_the_broker_and_says_why(
    mock_transport: Connection,
) -> None:
    """The broker does not model JetStream, so a service that asks for it fails to start, by name."""

    class Streams(CliffracerService):
        @rpc
        async def ping(self) -> str:
            return "pong"

    config = ServiceConfig(
        name="streams",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.>"])],
    )
    with pytest.raises(NotImplementedError, match="does not model JetStream"):
        async with ServiceTestHarness(Streams, config=config, broker=mock_transport.broker):
            pass
