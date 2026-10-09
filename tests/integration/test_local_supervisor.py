"""Generated shipment clients exercise owned activations on a real broker."""

import asyncio
import signal

import pytest

from cliffracer import ServiceConfig, rpc
from cliffracer.client import RpcServerError
from cliffracer.core.connection import BrokerConnectionState
from cliffracer.core.exceptions import RpcNoRespondersError, RpcTimeoutError
from cliffracer.runners import LocalSupervisor, SupervisorLimits
from cliffracer.runners.contracts import ActivationConflict, ActivationState, ActivationUnavailable
from tests.fixtures.shipment_templates import (
    Parcel,
    ShipmentReceipt,
    Shipments,
    shipment_client_class,
    shipment_template,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SETTINGS = {"warehouse": "north", "destinations": ["retail"]}


class ShipmentWorker(Shipments):
    def __init__(self, settings, runtime):
        super().__init__(settings, runtime)
        self.start_gate = asyncio.Event()
        self.start_gate.set()
        self.entered = asyncio.Event()
        self.ship_gate = asyncio.Event()
        self.ship_entered = asyncio.Event()
        self.attempts = 0

    async def on_startup(self):
        self.entered.set()
        await self.start_gate.wait()

    @rpc
    async def ship(self, parcel: Parcel) -> ShipmentReceipt:
        self.attempts += 1
        if parcel.sku == "unavailable":
            raise ValueError("shipment stock is unavailable")
        if parcel.sku == "delayed":
            self.ship_entered.set()
            while not self.ship_gate.is_set():
                try:
                    await self.ship_gate.wait()
                except asyncio.CancelledError:
                    continue
        return await super().ship(parcel)


async def ready(host, owner, key="batch-a", settings=None):
    return await host.ensure(
        owner, "shipments", key, SETTINGS if settings is None else settings, revision="warehouse-a"
    )


async def state(host, reference, expected):
    async with asyncio.timeout(3):
        while True:
            snapshot = await host.inspect(reference.identity)
            if snapshot.state == expected:
                return snapshot
            await asyncio.sleep(0)


def make_host(*, limits=None, configure=None, **runtime):
    children = []

    def factory(settings, assigned):
        child = ShipmentWorker(settings, assigned)
        if configure is not None:
            configure(child)
        children.append(child)
        return child

    host = LocalSupervisor(
        ServiceConfig(name="shipping_host", health_port=0, **runtime), limits=limits
    )
    host.register(shipment_template(service_class=ShipmentWorker, factory=factory))
    return host, children


async def test_matching_batch_requests_share_one_live_worker_after_caller_cancellation(
    nats_connection,
):
    client_type = shipment_client_class()
    gate = asyncio.Event()
    host, children = make_host(configure=lambda child: setattr(child, "start_gate", gate))
    async with host:
        owner = await host.open_owner("retail")
        first = asyncio.create_task(ready(host, owner))
        async with asyncio.timeout(2):
            while not children:
                await asyncio.sleep(0)
            await children[0].entered.wait()
        second = asyncio.create_task(ready(host, owner))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        gate.set()
        reference = await second
        assert reference == await ready(host, owner)
        client = reference.bind(client_type, nc=nats_connection)
        async with client:
            receipt = await client.ship(Parcel(sku="bolts", quantity=2))
            assert (receipt.warehouse, receipt.quantity) == ("north", 2)
        assert len(children) == 1
    assert children[0].stops == 1
    assert children[0].nc.is_closed


async def test_separate_batches_use_isolated_state_and_health_listeners_without_signal_changes(
    nats_connection,
):
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    client_type = shipment_client_class()
    host, children = make_host(namespace="wholesale")
    async with host:
        owner = await host.supervisor_owner("retail")
        north = await ready(host, owner)
        south = await ready(host, owner, "batch-b", {**SETTINGS, "warehouse": "south"})
        async with (
            north.bind(client_type, nc=nats_connection) as a,
            south.bind(client_type, nc=nats_connection) as b,
        ):
            assert (await a.ship(Parcel(sku="bolts", quantity=2))).warehouse == "north"
            assert (await b.ship(Parcel(sku="bolts", quantity=3))).warehouse == "south"
            assert (await a.ship(Parcel(sku="nuts", quantity=5))).quantity == 7
        assert len({child.health_listener.port for child in children}) == 2
        assert all(child.health_listener.port for child in children)
        assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    assert all(child.health_listener.port is None and child.nc.is_closed for child in children)
    assert not host.unfinished_tasks
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


async def test_old_business_address_and_stop_reference_cannot_touch_replacement(nats_connection):
    client_type = shipment_client_class()
    host, children = make_host()
    async with host:
        owner = await host.open_owner("retail")
        first = await ready(host, owner)
        old = first.bind(client_type, nc=nats_connection)
        async with old:
            assert (await old.ship(Parcel(sku="bolts", quantity=2))).quantity == 2
            assert (await host.stop(first)).complete
            second = await host.reactivate(owner, first)
            with pytest.raises(ActivationUnavailable, match="superseded"):
                await host.stop(first)
            with pytest.raises(RpcNoRespondersError):
                await old.ship(Parcel(sku="nuts", quantity=5))
            async with second.bind(client_type, nc=nats_connection) as new:
                assert (await new.ship(Parcel(sku="nuts", quantity=3))).quantity == 3
        assert children[0].shipped == 2
        assert children[1].shipped == 3


async def test_broker_reconnection_keeps_generation_and_permanent_loss_cleans_up(nats_connection):
    disconnected = asyncio.Event()
    allow_reconnect = asyncio.Event()
    connected = asyncio.Event()

    async def on_disconnect():
        disconnected.set()
        await allow_reconnect.wait()

    host, children = make_host(
        exit_on_closed=False,
        on_disconnect=on_disconnect,
        on_connect=connected.set,
        reconnect_time_wait=0,
    )
    try:
        async with host:
            owner = await host.open_owner("retail")
            reference = await ready(host, owner)
            connected.clear()
            children[0].nc._transport.close()
            await asyncio.wait_for(disconnected.wait(), timeout=2)
            observation = await host.inspect(reference.identity)
            assert observation.state == ActivationState.READY
            assert observation.broker_state == BrokerConnectionState.CONNECTING
            allow_reconnect.set()
            await asyncio.wait_for(connected.wait(), timeout=2)
            assert await ready(host, owner) == reference
            async with reference.bind(shipment_client_class(), nc=nats_connection) as client:
                assert (await client.ship(Parcel(sku="bolts", quantity=2))).quantity == 2
            await children[0].nc.close()
            failed = await state(host, reference, ActivationState.FAILED)
            assert failed.cleanup.complete
            assert children[0].health_listener.port is None
            assert children[0].stops == 1
    finally:
        allow_reconnect.set()


async def test_business_error_leaves_worker_ready_and_uncertain_order_is_never_replayed(
    nats_connection,
):
    host, children = make_host(
        limits=SupervisorLimits(cleanup_timeout=0.04, monitor_interval=0.005)
    )
    await host.start()
    client = None
    try:
        owner = await host.open_owner("retail")
        reference = await ready(host, owner)
        client = reference.bind(shipment_client_class(), nc=nats_connection, timeout=0.03)
        with pytest.raises(RpcServerError):
            await client.ship(Parcel(sku="unavailable", quantity=1))
        assert (await host.inspect(reference.identity)).state == ActivationState.READY
        with pytest.raises(RpcTimeoutError):
            await client.ship(Parcel(sku="delayed", quantity=2))
        assert children[0].ship_entered.is_set()
        assert children[0].attempts == 2
        assert await ready(host, owner) == reference
        outcome = await host.stop(reference)
        assert not outcome.complete
        with pytest.raises(ActivationConflict, match="cleanup"):
            await host.reactivate(owner, reference)
        children[0].ship_gate.set()
        terminal = await state(host, reference, ActivationState.STOPPED)
        assert terminal.cleanup.complete
        assert children[0].shipped == 2
        assert children[0].attempts == 2
    finally:
        for child in children:
            child.ship_gate.set()
        if client is not None:
            await client.close()
        await host.close()
