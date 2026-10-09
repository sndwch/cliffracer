"""Claims `docs/local-supervisor.md` makes that no other test states, each held here.

The supervisor's admission, conflict, reactivation, cancellation and cleanup behaviour is held by
`test_local_supervisor.py`, `test_service_owner_isolation.py`, `test_template_runtime_callbacks.py`
and the integration modules beside them. These are the sentences those leave open:

- the runtime a child is built from: its name, health port, callbacks and the two budgets it is
  given (its copy of the host's data is held where it is made, by `test_template_runtime_callbacks.py`);
- the smaller of the host and the template budget, from both sides;
- what `async with` does with a cleanup that did not finish;
- `retry_until`, and what retention expires and what it never does;
- what a snapshot leaves out;
- where each name the document tells a reader to import from lives;
- what a parent's `ServiceOwner` gives its `reactivate` and its `cleanup_report`.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency
from cliffracer.introspect import describe
from cliffracer.runners import (
    LocalSupervisor,
    ServiceOwner,
    SupervisorLimits,
    contracts,
    supervisor,
)
from cliffracer.runners.contracts import (
    ActivationCapacityError,
    ActivationConflict,
    ActivationState,
    ActivationUnavailable,
    TemplateError,
)
from cliffracer.runners.supervisor import ActivationTerminated, OwnerHandle
from tests.fixtures.shipment_templates import Shipments, shipment_template

pytestmark = pytest.mark.unit

SETTINGS = {"warehouse": "north", "destinations": ["retail"]}


class Worker(Shipments):
    """A shipment child whose startup and shutdown a test can hold open."""

    def __init__(self, settings, runtime):
        super().__init__(settings, runtime)
        self.start_gate = None
        self.stop_gate = None

    async def on_startup(self):
        while self.start_gate is not None and not self.start_gate.is_set():
            try:
                await self.start_gate.wait()
            except asyncio.CancelledError:
                continue

    async def on_shutdown(self):
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        await super().on_shutdown()


@pytest.fixture
async def make_host(monkeypatch):
    hosts, children, gates = [], [], []
    silent_broker = []

    async def connect(*args, **kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False

        async def subscribe(*args, **kwargs):
            return AsyncMock()

        async def close():
            nc.is_closed = True
            nc.is_connected = False

        async def drain():
            # A broker that has gone silent: the drain flushes and waits for an answer.
            await asyncio.Event().wait()

        nc.subscribe = subscribe
        nc.close = close
        if silent_broker:
            nc.drain = drain
        nc.request.return_value = SimpleNamespace(
            data=json.dumps(describe(Worker).to_dict()).encode()
        )
        return nc

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    async def make(
        *,
        runtime=None,
        limits=None,
        template=None,
        start_gate=None,
        stop_gate=None,
        start=True,
        silent=False,
    ):
        if silent:
            silent_broker.append(True)
        if start_gate is not None:
            gates.append(start_gate)
        if stop_gate is not None:
            gates.append(stop_gate)

        def factory(settings, config):
            child = Worker(settings, config)
            child.start_gate = start_gate
            child.stop_gate = stop_gate
            children.append(child)
            return child

        host = LocalSupervisor(
            runtime or ServiceConfig(name="shipping_host", health_port=0),
            # Not tight: a test that does not pass `limits` is not about a budget, and a stop or a
            # start that overruns one under load would leave a record UNFINISHED or expired and
            # fail the test for a reason it does not name. A test about a budget passes its own.
            limits=limits or SupervisorLimits(startup_timeout=5, cleanup_timeout=5),
        )
        host.register(shipment_template(service_class=Worker, factory=factory, **(template or {})))
        hosts.append(host)
        if start:
            await host.start()
        return host, children

    yield make
    for gate in gates:
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


async def stopped(host, reference):
    """Stop a child and require that its cleanup finished, so a slow stop fails by name.

    A test that goes on to reactivate, expire or list the record depends on the stop having
    completed. A stop that overruns its budget leaves the record UNFINISHED, and the test would
    then fail further on for a reason that points somewhere else.
    """
    outcome = await host.stop(reference)
    assert outcome.complete, (
        f"the child's stop did not finish inside the cleanup budget "
        f"({host.limits.cleanup_timeout}s): {outcome}"
    )
    return outcome


def advance(host, monkeypatch, seconds):
    """Move the supervisor's monotonic clock forward, as the retention clock does."""
    clock = host._clock
    monkeypatch.setattr(host, "_clock", lambda: clock() + seconds)


# -- what a child is built from ---------------------------------------------------------------


async def test_a_child_gets_a_copy_of_the_runtime_a_unique_name_and_an_ephemeral_health_port(
    make_host,
):
    runtime = ServiceConfig(
        name="shipping_host", namespace="retail", health_port=8765, nats_url="nats://broker:4222"
    )
    host, children = await make_host(runtime=runtime)
    owner = await host.open_owner("retail")
    first, second = await ensure(host, owner), await ensure(host, owner, "batch-b")

    names = [child.config.name for child in children]
    assert names == [first.address.service, second.address.service]
    assert len(set(names)) == 2
    assert all(name.startswith("activation_" + host.incarnation + "_") for name in names)
    for child in children:
        assert child.config.health_port == 0
        assert (child.config.namespace, child.config.nats_url) == ("retail", "nats://broker:4222")
        assert child.config is not runtime
    assert (runtime.name, runtime.health_port) == ("shipping_host", 8765)


async def test_a_childs_host_callbacks_keep_their_original_targets(make_host):
    class Status:
        def __init__(self):
            self.connected = 0

        def on_connect(self):
            self.connected += 1

    status = Status()
    runtime = ServiceConfig(name="shipping_host", health_port=0, on_connect=status.on_connect)
    host, children = await make_host(runtime=runtime)
    await ensure(host, await host.open_owner("retail"))

    callback = children[0].config.on_connect
    assert callback.__self__ is status
    before = status.connected
    callback()
    assert status.connected == before + 1


@pytest.mark.parametrize(
    ("host_budget", "template_budget"), [(10, 0.4), (0.4, 10)], ids=["template", "host"]
)
async def test_the_childs_stop_phase_is_a_fifth_of_the_smaller_cleanup_budget(
    make_host, host_budget, template_budget
):
    limits = SupervisorLimits(cleanup_timeout=host_budget)
    host, children = await make_host(limits=limits, template={"cleanup_timeout": template_budget})
    await ensure(host, await host.open_owner("retail"))

    assert children[0].config.shutdown_timeout == pytest.approx(0.08)


async def test_a_child_that_uses_every_stop_phase_closes_inside_the_cleanup_budget(make_host):
    """The phases run in sequence: the drain, the cancellation grace and the connection's drain,
    each given the child's `shutdown_timeout`. A task that outlasts the drain and then cleans up
    slowly, and a broker that has gone silent, make each of them take time. They add up to inside
    the budget the supervisor waits for, so the child has closed when it reads the outcome.
    """
    limits = SupervisorLimits(cleanup_timeout=1.0, monitor_interval=0.005)
    host, children = await make_host(limits=limits, silent=True)
    reference = await ensure(host, await host.open_owner("retail"))
    children[0].container.lifecycle.spawn_supervised_task(_slow_to_clean_up(), name="slow")
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING", format="{message}")

    try:
        outcome = await asyncio.wait_for(host.stop(reference), timeout=10)
    finally:
        logger.remove(sink)

    assert outcome.complete, outcome
    assert (await host.inspect(reference.identity)).state == ActivationState.STOPPED
    # The drain ran out and so did the connection's, or the stop was not the slow one under test.
    for phase in ("while draining active tasks", "could not drain its NATS"):
        assert any(phase in line for line in lines), (phase, lines)


async def test_CONTROL_a_child_that_stops_at_once_is_complete_with_the_same_budget(make_host):
    limits = SupervisorLimits(cleanup_timeout=1.0, monitor_interval=0.005)
    host, _ = await make_host(limits=limits, silent=False)
    reference = await ensure(host, await host.open_owner("retail"))

    assert (await host.stop(reference)).complete


async def _slow_to_clean_up():
    """Runs until it is cancelled and then takes 150 ms to finish, inside the child's grace."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(0.15)
        raise


@pytest.mark.parametrize(
    ("host_budget", "template_budget"), [(30, 0.02), (0.02, 30)], ids=["template", "host"]
)
async def test_startup_uses_the_smaller_of_the_host_and_the_template_budget(
    make_host, host_budget, template_budget
):
    gate = asyncio.Event()
    limits = SupervisorLimits(
        startup_timeout=host_budget, cleanup_timeout=0.02, monitor_interval=0.005
    )
    host, _ = await make_host(
        limits=limits, template={"startup_timeout": template_budget}, start_gate=gate
    )
    owner = await host.open_owner("retail")

    with pytest.raises(ActivationTerminated) as expired:
        await asyncio.wait_for(ensure(host, owner), timeout=2)

    assert expired.value.snapshot.reason == "startup deadline expired"


async def test_CONTROL_a_startup_inside_both_budgets_is_ready(make_host):
    # The twin of the test above with nothing holding the startup. Its budget is the point of the
    # twin only in being a budget the startup fits inside, so it is one a loaded host cannot spend:
    # 20 ms was the budget the held startups expire under, and an unheld one overran it at load.
    limits = SupervisorLimits(startup_timeout=30, cleanup_timeout=5)
    host, _ = await make_host(limits=limits, template={"startup_timeout": 5})

    reference = await ensure(host, await host.open_owner("retail"))

    assert (await host.inspect(reference.identity)).state == ActivationState.READY


@pytest.mark.parametrize(
    ("host_budget", "template_budget"), [(30, 0.02), (0.02, 30)], ids=["template", "host"]
)
async def test_cleanup_uses_the_smaller_of_the_host_and_the_template_budget(
    make_host, host_budget, template_budget
):
    gate = asyncio.Event()
    limits = SupervisorLimits(cleanup_timeout=host_budget, monitor_interval=0.005)
    host, _ = await make_host(
        limits=limits, template={"cleanup_timeout": template_budget}, stop_gate=gate
    )
    reference = await ensure(host, await host.open_owner("retail"))

    outcome = await asyncio.wait_for(host.stop(reference), timeout=2)

    assert not outcome.complete


async def test_CONTROL_a_cleanup_inside_both_budgets_completes(make_host):
    # The twin of the test above with nothing holding the stop, so its budget is one a stop fits
    # inside with room to spare: the held stops expire under their 20 ms budgets, and this one is
    # about a cleanup that finishes.
    limits = SupervisorLimits(cleanup_timeout=30)
    host, _ = await make_host(limits=limits, template={"cleanup_timeout": 5})
    reference = await ensure(host, await host.open_owner("retail"))

    assert (await host.stop(reference)).complete


async def test_CONTROL_a_stop_that_overruns_its_budget_fails_the_helper_by_name(make_host):
    """The precondition the other tests assert can fail, and says which budget was overrun."""
    gate = asyncio.Event()
    limits = SupervisorLimits(cleanup_timeout=0.02, monitor_interval=0.005)
    host, _ = await make_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, await host.open_owner("retail"))

    with pytest.raises(
        AssertionError, match=r"did not finish inside the cleanup budget \(0\.02s\)"
    ):
        await stopped(host, reference)


# -- async with, and what close reports -------------------------------------------------------


async def test_async_with_leaves_incomplete_cleanup_to_the_explicit_close_report(make_host):
    gate = asyncio.Event()
    limits = SupervisorLimits(cleanup_timeout=0.02, monitor_interval=0.005)
    host, _ = await make_host(limits=limits, stop_gate=gate, start=False)

    async with host:
        owner = await host.open_owner("retail")
        await ensure(host, owner)

    report = await host.close()
    assert not report.complete
    assert [item.state for item in report.activations] == [ActivationState.UNFINISHED]
    assert host.unfinished_tasks


async def test_CONTROL_async_with_over_finished_cleanup_reports_it_complete(make_host):
    host, _ = await make_host(start=False)

    async with host:
        await ensure(host, await host.open_owner("retail"))

    assert (await host.close()).complete


# -- retry_until and retention ----------------------------------------------------------------


async def test_a_clean_terminal_snapshot_advertises_retry_until_in_utc(make_host):
    host, _ = await make_host(limits=SupervisorLimits(retention=300))
    reference = await ensure(host, await host.open_owner("retail"))
    assert (await host.inspect(reference.identity)).retry_until is None

    before = datetime.now(UTC)
    await stopped(host, reference)
    after = datetime.now(UTC)

    retry_until = (await host.inspect(reference.identity)).retry_until
    assert retry_until.utcoffset() == timedelta(0)
    assert before + timedelta(seconds=300) <= retry_until <= after + timedelta(seconds=300)


async def test_a_key_is_accepted_again_with_a_fresh_address_once_its_terminal_record_expires(
    make_host, monkeypatch
):
    host, children = await make_host()
    owner = await host.open_owner("retail")
    first = await ensure(host, owner)
    await stopped(host, first)
    with pytest.raises(ActivationTerminated):
        await ensure(host, owner)

    advance(host, monkeypatch, host.limits.retention + 1)
    second = await ensure(host, owner)

    assert second.identity == first.identity
    assert second.address != first.address
    assert len(children) == 2


async def test_a_reactivation_retry_ends_when_the_terminal_record_expires(make_host, monkeypatch):
    host, _ = await make_host()
    owner = await host.open_owner("retail")
    first = await ensure(host, owner)
    await stopped(host, first)
    second = await host.reactivate(owner, first)
    assert await host.reactivate(owner, first) == second

    advance(host, monkeypatch, host.limits.retention + 1)

    with pytest.raises(ActivationUnavailable, match="expired"):
        await host.reactivate(owner, first)


async def test_a_closed_owner_expires_and_releases_its_capacity(make_host, monkeypatch):
    host, _ = await make_host(limits=SupervisorLimits(max_owners=1))
    owner = await host.open_owner("retail")
    assert (await host.close_owner(owner)).complete
    with pytest.raises(ActivationCapacityError):
        await host.open_owner("returns")

    advance(host, monkeypatch, host.limits.retention + 1)

    assert (await host.open_owner("returns")).scope == "returns"
    with pytest.raises(ActivationUnavailable, match="unknown or expired"):
        await host.close_owner(owner)


async def test_retention_never_expires_active_or_unfinished_work(make_host, monkeypatch):
    gate = asyncio.Event()
    limits = SupervisorLimits(cleanup_timeout=0.02, monitor_interval=0.005)
    host, _ = await make_host(limits=limits, stop_gate=gate)
    owner = await host.open_owner("retail")
    ready = await ensure(host, owner, "batch-a")
    stuck = await ensure(host, owner, "batch-b")
    assert not (await host.stop(stuck)).complete

    advance(host, monkeypatch, 10 * host.limits.retention)

    assert (await host.inspect(ready.identity)).state == ActivationState.READY
    assert (await host.inspect(stuck.identity)).state == ActivationState.UNFINISHED


async def test_a_later_page_omits_a_record_that_expired_and_repeats_none(make_host, monkeypatch):
    host, _ = await make_host()
    owner = await host.open_owner("retail")
    references = [await ensure(host, owner, key) for key in ("batch-a", "batch-b", "batch-c")]
    first = await host.list_activations("retail", limit=1)
    await stopped(host, references[1])

    advance(host, monkeypatch, host.limits.retention + 1)
    rest = await host.list_activations("retail", cursor=first.cursor, limit=5)

    assert [item.reference for item in rest.items] == [references[2]]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
async def test_a_listing_limit_below_one_or_not_an_integer_is_refused(make_host, limit):
    host, _ = await make_host()

    with pytest.raises(ValueError, match="limit"):
        await host.list_activations("retail", limit=limit)


async def test_a_cursor_belongs_to_the_supervisor_that_issued_it(make_host):
    issuer, _ = await make_host()
    other, _ = await make_host()
    for host in (issuer, other):
        owner = await host.open_owner("retail")
        for key in ("batch-a", "batch-b"):
            await ensure(host, owner, key)
    page = await issuer.list_activations("retail", limit=1)
    assert page.cursor

    with pytest.raises(ActivationUnavailable, match="cursor"):
        await other.list_activations("retail", cursor=page.cursor)
    assert (await issuer.list_activations("retail", cursor=page.cursor)).cursor is None


# -- capacity and the host's own close --------------------------------------------------------


async def test_ready_and_stopping_activations_count_toward_max_active(make_host):
    gate = asyncio.Event()
    host, _ = await make_host(
        limits=SupervisorLimits(max_active=1, cleanup_timeout=5), stop_gate=gate
    )
    owner = await host.open_owner("retail")
    reference = await ensure(host, owner)
    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")

    stopping = asyncio.create_task(host.stop(reference))
    identity = reference.identity
    async with asyncio.timeout(2):
        while (await host.inspect(identity)).state != ActivationState.STOPPING:
            await asyncio.sleep(0)
    with pytest.raises(ActivationCapacityError, match="active"):
        await ensure(host, owner, "batch-b")

    gate.set()
    assert (await stopping).complete
    assert (await ensure(host, owner, "batch-b")).identity.key == "batch-b"


async def test_closing_the_host_alone_closes_every_remaining_owner(make_host):
    host, children = await make_host()
    chosen = await host.supervisor_owner("retail")
    nested = await host.open_owner("wholesale", parent=chosen)
    await ensure(host, nested)

    report = await host.close()

    assert report.complete and children[0].stops == 1
    for handle in (chosen, nested):
        with pytest.raises(ActivationUnavailable, match="closed"):
            await ensure(host, handle, "batch-b")
    with pytest.raises(ActivationUnavailable, match="closed"):
        await host.open_owner("returns")


# -- what an inspection leaves out ------------------------------------------------------------


async def test_a_ready_snapshot_carries_no_settings_and_no_broker_credential(make_host):
    runtime = ServiceConfig(
        name="shipping_host", health_port=0, nats_user="private-user", nats_password="private-pass"
    )
    host, _ = await make_host(runtime=runtime)
    owner = await host.open_owner("retail")
    reference = await ensure(host, owner, settings={**SETTINGS, "warehouse": "private-warehouse"})

    snapshot = await host.inspect(reference.identity)
    page = await host.list_activations("retail")

    assert snapshot.state == ActivationState.READY
    for observed in (repr(snapshot), repr(page), repr(reference)):
        for secret in ("private-warehouse", "private-user", "private-pass"):
            assert secret not in observed


# -- where the document tells a reader to import from -----------------------------------------


def test_the_names_the_document_imports_live_where_it_says():
    from cliffracer import runners

    assert {"LocalSupervisor", "SupervisorLimits", "ServiceOwner"} <= set(runners.__all__)
    for name in ("OwnerHandle", "ActivationPage", "ActivationTerminated", "CleanupReport"):
        assert getattr(supervisor, name).__module__ == supervisor.__name__
        assert not hasattr(contracts, name)
    for name in (
        "ActivationReference",
        "ActivationSnapshot",
        "ActivationConflict",
        "ActivationCapacityError",
        "ActivationUnavailable",
        "CleanupOutcome",
        "LogicalIdentity",
    ):
        assert getattr(contracts, name).__module__ == contracts.__name__


# -- a parent's ServiceOwner ------------------------------------------------------------------


async def test_a_parents_owner_issues_its_handle_reactivates_for_itself_and_keeps_the_report(
    make_host,
):
    host, _ = await make_host()

    class Orders(CliffracerService):
        children = ServiceOwner(SharedDependency(host), scope="retail")

    north = Orders(ServiceConfig(name="orders_north", health_port=0))
    south = Orders(ServiceConfig(name="orders_south", health_port=0))
    await north.start()
    await south.start()
    try:
        assert isinstance(north.children.owner, OwnerHandle)
        assert north.children.owner != south.children.owner
        first = await north.children.ensure(
            "shipments", "batch-a", SETTINGS, revision="warehouse-a"
        )
        await stopped(host, first)

        with pytest.raises(ActivationConflict, match="another owner"):
            await south.children.reactivate(first)
        second = await north.children.reactivate(first)
        assert second.generation == first.generation + 1
    finally:
        await north.stop()
        await south.stop()

    report = north.children.cleanup_report
    assert report.complete
    assert [item.reference for item in report.activations] == [first, second]
    assert south.children.cleanup_report.activations == ()
    assert (await host.inspect(first.identity)).state == ActivationState.STOPPED


def test_a_template_cannot_declare_the_owner_extension_that_belongs_on_a_parent():
    supervisor = LocalSupervisor(ServiceConfig(name="shipping_host", health_port=0))

    class Nesting(Shipments):
        children = ServiceOwner(SharedDependency(supervisor), scope="retail")

    with pytest.raises(TemplateError, match="unsupported template declaration children.stop"):
        supervisor.register(shipment_template(service_class=Nesting))
    supervisor.register(shipment_template())
