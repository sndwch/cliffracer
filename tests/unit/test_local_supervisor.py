"""Bounded shipment ownership, retained outcomes and interruption-safe cleanup."""

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import ServiceConfig
from cliffracer.introspect import describe
from cliffracer.runners import LocalSupervisor, SupervisorLimits
from cliffracer.runners.contracts import (
    ActivationCapacityError,
    ActivationConflict,
    ActivationState,
    ActivationUnavailable,
    LogicalIdentity,
)
from cliffracer.runners.supervisor import ActivationTerminated
from tests.fixtures.shipment_templates import Parcel, Shipments, shipment_template

pytestmark = pytest.mark.unit

SETTINGS = {"warehouse": "north", "destinations": ["retail"]}


class ShipmentWorker(Shipments):
    def __init__(self, settings, runtime):
        super().__init__(settings, runtime)
        self.start_gate = None
        self.stop_gate = None
        self.start_entered = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.resist_start_cancel = False
        self.fail_start = False
        self.fail_stop = False
        self.drift = False

    async def on_startup(self):
        self.start_entered.set()
        if self.fail_start:
            raise RuntimeError("private-warehouse-credential")
        if self.drift:
            description = describe(ShipmentWorker).to_dict()
            description["methods"] = []
            self.nc.request.return_value = SimpleNamespace(data=json.dumps(description).encode())
        if self.start_gate is not None:
            while not self.start_gate.is_set():
                try:
                    await self.start_gate.wait()
                except asyncio.CancelledError:
                    if not self.resist_start_cancel:
                        raise

    async def on_shutdown(self):
        self.stop_entered.set()
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.fail_stop:
            raise RuntimeError("private-warehouse-credential")
        await super().on_shutdown()


@pytest.fixture
async def shipment_host(monkeypatch):
    hosts = []
    children = []

    async def connect(*args, **kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False

        async def subscribe(*args, **kwargs):
            return AsyncMock()

        async def close():
            nc.is_closed = True
            nc.is_connected = False

        nc.subscribe = subscribe
        nc.close = close
        nc.request.return_value = SimpleNamespace(
            data=json.dumps(describe(ShipmentWorker).to_dict()).encode()
        )
        return nc

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    async def make(*, limits=None, start_gate=None, stop_gate=None, resist=False, configure=None):
        def factory(settings, runtime):
            child = ShipmentWorker(settings, runtime)
            child.start_gate = start_gate
            child.stop_gate = stop_gate
            child.resist_start_cancel = resist
            if configure is not None:
                configure(child)
            children.append(child)
            return child

        host = LocalSupervisor(
            ServiceConfig(name="shipments", health_port=0),
            limits=limits
            or SupervisorLimits(startup_timeout=1, cleanup_timeout=0.2, wait_timeout=2),
        )
        host.register(shipment_template(service_class=ShipmentWorker, factory=factory))
        await host.start()
        hosts.append(host)
        return host, await host.open_owner("retail"), children

    yield make
    for child in children:
        for gate in (child.start_gate, child.stop_gate):
            if gate is not None:
                gate.set()
    for host in hosts:
        await host.close()
        pending = host.unfinished_tasks
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
        if host._monitor_task is not None:
            host._monitor_task.cancel()
            await asyncio.gather(host._monitor_task, return_exceptions=True)
    for child in children:
        await child.stop()


async def ensure(host, owner, key="batch-a", settings=None):
    return await host.ensure(
        owner, "shipments", key, SETTINGS if settings is None else settings, revision="warehouse-a"
    )


async def wait_for_state(host, identity, state):
    async with asyncio.timeout(2):
        while True:
            snapshot = await host.inspect(identity)
            if snapshot is not None and snapshot.state == state:
                return snapshot
            await asyncio.sleep(0)


async def test_matching_orders_share_startup_after_one_requester_is_cancelled(shipment_host):
    gate = asyncio.Event()
    host, owner, children = await shipment_host(start_gate=gate)
    first = asyncio.create_task(ensure(host, owner))
    identity = LogicalIdentity("retail", "shipments", "batch-a")
    await wait_for_state(host, identity, ActivationState.STARTING)
    second = asyncio.create_task(ensure(host, owner))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    reference = await second
    assert len(children) == 1
    assert await ensure(host, owner) == reference
    assert (await children[0].ship(Parcel(sku="bolts", quantity=2))).quantity == 2
    assert (await children[0].ship(Parcel(sku="nuts", quantity=3))).quantity == 5


async def test_owner_settings_and_revision_conflicts_create_no_shipments(shipment_host):
    host, owner, children = await shipment_host()
    reference = await ensure(host, owner)
    other_owner = await host.open_owner("retail")
    with pytest.raises(ActivationConflict):
        await ensure(host, other_owner)
    with pytest.raises(ActivationConflict):
        await ensure(host, owner, settings={**SETTINGS, "warehouse": "south"})
    host.register(
        shipment_template(
            revision="warehouse-b",
            service_class=ShipmentWorker,
            factory=lambda settings, runtime: ShipmentWorker(settings, runtime),
        )
    )
    with pytest.raises(ActivationConflict):
        await host.ensure(owner, "shipments", "batch-a", SETTINGS, revision="warehouse-b")
    assert len(children) == 1
    assert (await host.inspect(reference.identity)).state == ActivationState.READY


async def test_stopped_batch_is_retained_and_only_explicit_reactivation_creates_successor(
    shipment_host,
):
    host, owner, children = await shipment_host()
    first = await ensure(host, owner)
    assert (await host.stop(first)).complete
    with pytest.raises(ActivationTerminated) as stopped:
        await ensure(host, owner)
    assert stopped.value.snapshot.state == ActivationState.STOPPED
    assert stopped.value.snapshot.cleanup.complete
    assert stopped.value.snapshot.retry_until is not None
    second, retry = await asyncio.gather(
        host.reactivate(owner, first), host.reactivate(owner, first)
    )
    assert second == retry
    assert second.generation == first.generation + 1
    assert second.address != first.address
    assert await ensure(host, owner) == second
    with pytest.raises(ActivationUnavailable, match="superseded"):
        await host.stop(first)
    with pytest.raises(ActivationUnavailable):
        await host.stop(replace(second, generation=first.generation))
    assert len(children) == 2
    assert children[0].stops == 1
    assert children[1].stops == 0


async def test_starting_batches_reserve_capacity_until_clean_stop(shipment_host):
    gate = asyncio.Event()
    host, owner, children = await shipment_host(
        limits=SupervisorLimits(max_active=1), start_gate=gate
    )
    starting = asyncio.create_task(ensure(host, owner))
    snapshot = await wait_for_state(
        host, LogicalIdentity("retail", "shipments", "batch-a"), ActivationState.STARTING
    )
    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")
    assert (await host.stop(snapshot.reference)).complete
    with pytest.raises(ActivationTerminated):
        await starting
    gate.set()
    successor = await ensure(host, owner, "batch-b")
    assert successor.identity.key == "batch-b"
    assert sum(child.container.is_running for child in children) == 1


async def test_cancelled_wait_does_not_cancel_accepted_cleanup(shipment_host):
    gate = asyncio.Event()
    host, owner, children = await shipment_host(stop_gate=gate)
    reference = await ensure(host, owner)
    stop = asyncio.create_task(host.stop(reference))
    await children[0].stop_entered.wait()
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert (await host.inspect(reference.identity)).state == ActivationState.STOPPING
    gate.set()
    snapshot = await wait_for_state(host, reference.identity, ActivationState.STOPPED)
    assert snapshot.cleanup.complete
    assert children[0].stops == 1


async def test_unfinished_batch_keeps_capacity_and_cannot_be_replaced(shipment_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, owner)
    outcome = await asyncio.wait_for(host.stop(reference), timeout=1)
    assert not outcome.complete
    assert outcome.unfinished_tasks >= 1
    assert host.unfinished_tasks
    with pytest.raises(ActivationTerminated) as unfinished:
        await ensure(host, owner)
    assert unfinished.value.snapshot.state == ActivationState.UNFINISHED
    with pytest.raises(ActivationConflict, match="cleanup"):
        await host.reactivate(owner, reference)
    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")
    gate.set()
    snapshot = await wait_for_state(host, reference.identity, ActivationState.STOPPED)
    assert snapshot.cleanup.complete
    assert not host.unfinished_tasks
    assert (await ensure(host, owner, "batch-b")).identity.key == "batch-b"


async def test_owner_close_prevents_late_readiness_and_new_descendants(shipment_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits, start_gate=gate, resist=True)
    nested = await host.open_owner("wholesale", parent=owner)
    starting = asyncio.create_task(ensure(host, nested))
    identity = LogicalIdentity("wholesale", "shipments", "batch-a")
    await wait_for_state(host, identity, ActivationState.STARTING)
    while not children:
        await asyncio.sleep(0)
    await children[0].start_entered.wait()
    report = await host.close_owner(owner)
    assert not report.complete
    with pytest.raises(ActivationUnavailable, match="closed"):
        await ensure(host, nested, "batch-b")
    with pytest.raises(ActivationUnavailable, match="closed"):
        await host.open_owner("returns", parent=owner)
    with pytest.raises(ActivationTerminated):
        await starting
    gate.set()
    snapshot = await wait_for_state(host, identity, ActivationState.STOPPED)
    assert snapshot.cleanup.complete
    assert not children[0].container.is_running


async def test_retained_terminal_outcomes_consume_records_until_guarantee_expires(
    shipment_host, monkeypatch
):
    host, owner, children = await shipment_host(limits=SupervisorLimits(max_records=1))
    first = await ensure(host, owner)
    await host.stop(first)
    with pytest.raises(ActivationCapacityError, match="retained"):
        await ensure(host, owner, "batch-b")
    with pytest.raises(ActivationCapacityError, match="retained"):
        await host.reactivate(owner, first)
    clock = host._clock
    monkeypatch.setattr(host, "_clock", lambda: clock() + host.limits.retention + 1)
    assert await host.inspect(first.identity) is None
    with pytest.raises(ActivationUnavailable, match="expired"):
        await host.stop(first)
    assert (await ensure(host, owner, "batch-b")).identity.key == "batch-b"
    assert len(children) == 2


async def test_listing_pins_admission_ceiling_and_reads_current_outcomes(shipment_host):
    host, owner, _ = await shipment_host()
    references = [await ensure(host, owner, key) for key in ("batch-a", "batch-b", "batch-c")]
    first = await host.list_activations("retail", limit=1)
    assert [item.reference for item in first.items] == references[:1]
    assert first.cursor
    await ensure(host, owner, "batch-d")
    await host.stop(references[1])
    rest = await host.list_activations("retail", cursor=first.cursor, limit=2)
    assert [item.reference for item in rest.items] == references[1:]
    assert rest.items[0].state == ActivationState.STOPPED
    assert rest.cursor is None
    with pytest.raises(ActivationUnavailable, match="cursor"):
        await host.list_activations("wholesale", cursor=first.cursor)
    with pytest.raises(ValueError, match="limit"):
        await host.list_activations("retail", limit=host.limits.max_page + 1)


async def test_owner_count_and_depth_are_bounded(shipment_host):
    host, owner, _ = await shipment_host(limits=SupervisorLimits(max_owners=2, max_depth=2))
    child = await host.open_owner("wholesale", parent=owner)
    with pytest.raises(ActivationCapacityError):
        await host.open_owner("returns", parent=child)
    with pytest.raises(ActivationCapacityError):
        await host.open_owner("returns")
    assert (await host.close_owner(owner)).complete
    with pytest.raises(ActivationUnavailable, match="closed"):
        await ensure(host, child)


async def test_closed_owner_cannot_construct_another_shipment(shipment_host):
    host, owner, children = await shipment_host()
    await ensure(host, owner)
    assert (await host.close_owner(owner)).complete
    with pytest.raises(ActivationUnavailable):
        await ensure(host, owner, "batch-b")
    assert len(children) == 1
    assert children[0].stops == 1


async def test_stale_stop_does_not_interrupt_replacement_shipments(shipment_host):
    host, owner, children = await shipment_host()
    first = await ensure(host, owner)
    await host.stop(first)
    replacement = await host.reactivate(owner, first)
    try:
        await host.stop(first)
    except ActivationUnavailable:
        pass
    assert children[1].stops == 0
    assert children[1].container.is_running
    assert (await children[1].ship(Parcel(sku="bolts", quantity=2))).quantity == 2
    assert (await host.inspect(replacement.identity)).state == ActivationState.READY


async def test_close_releases_warehouse_connection_and_health_listener(shipment_host):
    host, owner, children = await shipment_host()
    await ensure(host, owner)
    await host.close()
    assert children[0].nc.is_closed
    assert children[0].health_listener.port is None
    assert children[0].stops == 1


@pytest.mark.parametrize("failure", ["fail_start", "drift"])
async def test_failed_startup_and_contract_drift_release_resources_before_outcome(
    shipment_host, failure
):
    host, owner, children = await shipment_host(
        configure=lambda child: setattr(child, failure, True)
    )
    with pytest.raises(ActivationTerminated) as failed:
        await ensure(host, owner, settings={**SETTINGS, "destinations": ["private-destination"]})
    snapshot = failed.value.snapshot
    assert snapshot.state == ActivationState.FAILED
    assert snapshot.cleanup.complete
    assert children[0].nc.is_closed
    assert children[0].health_listener.port is None
    assert not host.unfinished_tasks
    assert "private-warehouse-credential" not in repr(snapshot)
    assert "private-destination" not in repr(snapshot)
    with pytest.raises(ActivationTerminated):
        await ensure(host, owner, settings={**SETTINGS, "destinations": ["private-destination"]})
    assert len(children) == 1


async def test_startup_budget_expires_without_waiting_forever_for_cancellation(shipment_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(startup_timeout=0.02, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits, start_gate=gate, resist=True)
    with pytest.raises(ActivationTerminated) as failed:
        await asyncio.wait_for(ensure(host, owner), timeout=1)
    assert failed.value.snapshot.state == ActivationState.UNFINISHED
    assert failed.value.snapshot.reason == "startup deadline expired"
    assert host.unfinished_tasks
    gate.set()
    snapshot = await wait_for_state(
        host, failed.value.snapshot.reference.identity, ActivationState.FAILED
    )
    assert snapshot.cleanup.complete
    assert not children[0].container.is_running


async def test_requester_wait_budget_preserves_accepted_startup(shipment_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(wait_timeout=0.02)
    host, owner, children = await shipment_host(limits=limits, start_gate=gate)
    identity = LogicalIdentity("retail", "shipments", "batch-a")
    with pytest.raises(ActivationUnavailable, match="caller wait expired"):
        await ensure(host, owner)
    assert len(children) == 1
    assert not children[0].stop_entered.is_set()
    assert (await host.inspect(identity)).state == ActivationState.STARTING

    gate.set()
    ready = await wait_for_state(host, identity, ActivationState.READY)

    assert ready.reference.generation == 1
    assert await ensure(host, owner) == ready.reference
    assert len(children) == 1


async def test_late_cleanup_failure_never_releases_a_shipment_slot(shipment_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, owner)
    children[0].fail_stop = True
    assert not (await host.stop(reference)).complete
    gate.set()
    pending = host.unfinished_tasks
    await asyncio.gather(*pending, return_exceptions=True)
    # A completed stop coroutine cannot prove that its failed cleanup released resources.
    assert not host._resources_closed(host._reference(reference))
    snapshot = await host.inspect(reference.identity)
    assert snapshot.state == ActivationState.UNFINISHED
    assert snapshot.cleanup.unfinished_tasks == 0
    assert not snapshot.cleanup.complete
    assert "private-warehouse-credential" not in repr(snapshot)
    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")


async def test_owner_close_starts_all_child_cleanup_under_one_deadline(shipment_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(cleanup_timeout=0.02)
    host, owner, children = await shipment_host(limits=limits, stop_gate=gate)
    for key in ("batch-a", "batch-b", "batch-c"):
        await ensure(host, owner, key)
    report = await asyncio.wait_for(host.close_owner(owner), timeout=1)
    assert not report.complete
    assert len(report.activations) == 3
    assert all(child.stop_entered.is_set() for child in children)
    assert all(item.state == ActivationState.UNFINISHED for item in report.activations)
    with pytest.raises(ActivationUnavailable, match="closed"):
        await ensure(host, owner, "batch-d")


def test_host_exits_with_cancellation_resistant_packing_reported():
    result = subprocess.run(
        [sys.executable, "-m", "tests.fixtures.shipment_shutdown_process"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "PACKING STARTED" in result.stdout
    assert "UNFINISHED SHIPMENT REPORTED" in result.stdout
    assert "HOST EXITED" in result.stdout
    assert "unfinished-packing" in result.stderr


@pytest.mark.parametrize(
    "field", ["max_active", "max_records", "max_owners", "max_depth", "max_page"]
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_capacity_configuration_is_finite(field, value):
    with pytest.raises(ValueError, match="positive integers"):
        SupervisorLimits(**{field: value})


@pytest.mark.parametrize(
    "field", ["startup_timeout", "cleanup_timeout", "wait_timeout", "retention", "monitor_interval"]
)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_time_budgets_are_finite(field, value):
    with pytest.raises(ValueError, match="finite and positive"):
        SupervisorLimits(**{field: value})
