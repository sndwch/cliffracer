"""Claims `docs/virtual-services.md` makes about the code that no other test holds.

Every other claim in that document is held by `test_local_supervisor.py`,
`test_service_templates.py`, `test_activation_references.py`, `test_service_owner_isolation.py`,
`test_template_runtime_callbacks.py` or the integration tests beside them.

Supervisor incarnations: each is distinct, so a restarted host holds none of the old references.
Routing: a dedicated activation subscribes under a queue group of its own address.
Registration: a validated listener and a broadcast declaration are event listeners, and are refused.
The state table: ready and stopping hold an activation slot, and failed releases it.
Lifetimes: closing the supervisor closes a supervisor-owned lifetime, a closed owner is retained for
the retention window and then expires, and a terminal record advertises the end of its guarantee.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import ServiceConfig, broadcast, validated_listener
from cliffracer.introspect import describe
from cliffracer.runners import LocalSupervisor, SupervisorLimits, TemplateCatalog
from cliffracer.runners.contracts import (
    ActivationCapacityError,
    ActivationState,
    ActivationUnavailable,
    TemplateError,
)
from cliffracer.runners.supervisor import ActivationTerminated
from tests.fixtures.shipment_templates import Shipments, shipment_template

pytestmark = pytest.mark.unit

SETTINGS = {"warehouse": "north", "destinations": ["retail"]}


class Worker(Shipments):
    """A shipment worker whose startup and shutdown a test can hold or fail."""

    def __init__(self, settings, runtime):
        super().__init__(settings, runtime)
        self.stop_gate = None
        self.stop_entered = asyncio.Event()
        self.fail_start = False

    async def on_startup(self):
        if self.fail_start:
            raise RuntimeError("startup refused")

    async def on_shutdown(self):
        self.stop_entered.set()
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        await super().on_shutdown()


@pytest.fixture
async def make_host(monkeypatch):
    """Build started supervisors over a connection that records every subscription."""
    hosts = []
    children = []
    subscriptions: dict[str, list[tuple[str, str | None]]] = {}

    async def connect(*args, **kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False
        made: list[tuple[str, str | None]] = []

        async def subscribe(subject, *args, queue=None, **kwargs):
            made.append((subject, queue))
            return AsyncMock()

        async def close():
            nc.is_closed = True
            nc.is_connected = False

        nc.subscribe = subscribe
        nc.close = close
        nc.request.return_value = SimpleNamespace(
            data=json.dumps(describe(Worker).to_dict()).encode()
        )
        subscriptions[f"connection-{len(subscriptions)}"] = made
        return nc

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    async def make(*, limits=None, configure=None):
        def factory(settings, runtime):
            child = Worker(settings, runtime)
            if configure is not None:
                configure(child)
            children.append(child)
            return child

        host = LocalSupervisor(
            ServiceConfig(name="shipments", health_port=0),
            limits=limits or SupervisorLimits(startup_timeout=1, cleanup_timeout=1, wait_timeout=2),
        )
        host.register(shipment_template(service_class=Worker, factory=factory))
        await host.start()
        hosts.append(host)
        return host, children

    yield make, subscriptions
    for child in children:
        if child.stop_gate is not None:
            child.stop_gate.set()
    for host in hosts:
        await host.close()
        if host._monitor_task is not None:
            host._monitor_task.cancel()
            await asyncio.gather(host._monitor_task, return_exceptions=True)
    for child in children:
        await child.stop()


async def ensure(host, owner, key="batch-a"):
    return await host.ensure(owner, "shipments", key, SETTINGS, revision="warehouse-a")


# -- incarnations -----------------------------------------------------------------------------


async def test_a_second_supervisor_holds_none_of_the_first_ones_references(make_host):
    """A host restart is a new supervisor: the same key starts again and old references are void."""
    make, _ = make_host
    old, children = await make()
    new, _ = await make()
    assert old.incarnation != new.incarnation

    old_owner = await old.open_owner("retail")
    new_owner = await new.open_owner("retail")
    before = await ensure(old, old_owner)
    after = await ensure(new, new_owner)

    assert before.incarnation == old.incarnation
    assert after.incarnation == new.incarnation
    assert before.identity == after.identity
    assert before.generation == after.generation == 1
    assert before.address != after.address
    assert len(children) == 2

    with pytest.raises(ActivationUnavailable, match="unknown or expired"):
        await new.stop(before)
    with pytest.raises(ActivationUnavailable, match="unknown or expired"):
        await ensure(new, old_owner)
    assert (await new.inspect(before.identity)).reference == after
    assert [item.reference for item in (await new.list_activations("retail")).items] == [after]
    assert (await old.inspect(before.identity)).state == ActivationState.READY


# -- routing ----------------------------------------------------------------------------------


async def test_a_dedicated_activation_subscribes_under_a_queue_group_of_its_own(make_host):
    make, subscriptions = make_host
    host, _ = await make()
    owner = await host.open_owner("retail")
    first = await ensure(host, owner, "batch-a")
    second = await ensure(host, owner, "batch-b")

    groups = [
        {queue for subject, queue in made if subject.endswith(".rpc.*")}
        for made in subscriptions.values()
    ]
    assert groups == [{f"{first.address.service}.rpc"}, {f"{second.address.service}.rpc"}]
    assert first.address.service != second.address.service


# -- registration -----------------------------------------------------------------------------


class Order(BaseModel):
    sku: str


@pytest.mark.parametrize("declaration", ["validated_listener", "broadcast"])
def test_registration_refuses_a_validated_listener_and_a_broadcast_declaration(declaration):
    class Freight(Shipments):
        pass

    async def dispatch(self, subject: str, sku: str = "") -> None:
        pass

    if declaration == "validated_listener":
        Freight.dispatch = validated_listener("shipment.created", Order)(dispatch)
    else:
        Freight.dispatch = broadcast("shipment.alerts")(dispatch)

    with pytest.raises(TemplateError, match="unsupported template declaration dispatch"):
        TemplateCatalog().register(shipment_template(service_class=Freight))


# -- the state table --------------------------------------------------------------------------


async def test_a_ready_activation_holds_its_slot(make_host):
    make, _ = make_host
    host, _ = await make(limits=SupervisorLimits(max_active=1))
    owner = await host.open_owner("retail")
    ready = await ensure(host, owner, "batch-a")
    assert (await host.inspect(ready.identity)).state == ActivationState.READY

    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")


async def test_a_stopping_activation_holds_its_slot_until_cleanup_ends(make_host):
    make, _ = make_host
    gate = asyncio.Event()
    host, children = await make(
        limits=SupervisorLimits(max_active=1, startup_timeout=1, cleanup_timeout=5, wait_timeout=2),
        configure=lambda child: setattr(child, "stop_gate", gate),
    )
    owner = await host.open_owner("retail")
    reference = await ensure(host, owner, "batch-a")
    stopping = asyncio.create_task(host.stop(reference))
    await children[0].stop_entered.wait()
    assert (await host.inspect(reference.identity)).state == ActivationState.STOPPING

    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")

    gate.set()
    assert (await stopping).complete
    assert (await ensure(host, owner, "batch-b")).identity.key == "batch-b"


async def test_a_failed_activation_releases_its_slot(make_host):
    make, _ = make_host
    built = []

    def fail_the_first(child):
        built.append(child)
        child.fail_start = len(built) == 1

    host, children = await make(
        limits=SupervisorLimits(max_active=1, startup_timeout=1, cleanup_timeout=1, wait_timeout=2),
        configure=fail_the_first,
    )
    owner = await host.open_owner("retail")
    with pytest.raises(ActivationTerminated) as failed:
        await ensure(host, owner, "batch-a")
    assert failed.value.snapshot.state == ActivationState.FAILED
    assert failed.value.snapshot.cleanup.complete

    assert (await ensure(host, owner, "batch-b")).identity.key == "batch-b"
    assert len(children) == 2


# -- lifetimes --------------------------------------------------------------------------------


async def test_closing_the_supervisor_closes_a_supervisor_owned_lifetime(make_host):
    make, _ = make_host
    host, children = await make()
    owner = await host.supervisor_owner("retail")
    await ensure(host, owner)

    assert (await host.close()).complete
    assert children[0].stops == 1
    with pytest.raises(ActivationUnavailable, match="closed"):
        await ensure(host, owner, "batch-b")
    with pytest.raises(ActivationUnavailable, match="closed"):
        await host.open_owner("returns")
    assert len(children) == 1


async def test_a_closed_owner_is_retained_for_the_retention_window_then_expires(
    make_host, monkeypatch
):
    make, _ = make_host
    host, _ = await make(limits=SupervisorLimits(max_owners=1, retention=60))
    owner = await host.open_owner("retail")
    assert (await host.close_owner(owner)).complete

    with pytest.raises(ActivationCapacityError, match="owner capacity"):
        await host.open_owner("returns")
    with pytest.raises(ActivationUnavailable, match="closed"):
        await ensure(host, owner)

    clock = host._clock
    monkeypatch.setattr(host, "_clock", lambda: clock() + 61)
    assert await host.open_owner("returns")
    with pytest.raises(ActivationUnavailable, match="unknown or expired"):
        await ensure(host, owner)


async def test_a_terminal_record_advertises_the_end_of_its_retry_guarantee(make_host):
    make, _ = make_host
    host, _ = await make(
        limits=SupervisorLimits(startup_timeout=1, cleanup_timeout=1, wait_timeout=2, retention=120)
    )
    owner = await host.open_owner("retail")
    reference = await ensure(host, owner)

    assert (await host.stop(reference)).complete
    remaining = (await host.inspect(reference.identity)).retry_until - datetime.now(UTC)

    assert 100 <= remaining.total_seconds() <= 120
