"""LocalSupervisor at its edges: loops, owners, late failures, abandoned work and the monitor."""

import asyncio
import dataclasses
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import BaseModel, ConfigDict

from cliffracer import ServiceConfig
from cliffracer.core.loop_host import is_abandoned
from cliffracer.introspect import describe
from cliffracer.runners import LocalSupervisor, SupervisorLimits
from cliffracer.runners.contracts import (
    ActivationCapacityError,
    ActivationConflict,
    ActivationState,
    ActivationUnavailable,
    LogicalIdentity,
)
from cliffracer.runners.supervisor import (
    ActivationPage,
    ActivationTerminated,
    CleanupReport,
    OwnerHandle,
)
from tests.fixtures.shipment_templates import shipment_template
from tests.unit.test_local_supervisor import ShipmentWorker

pytestmark = pytest.mark.unit

SETTINGS = {"warehouse": "north", "destinations": ["retail"]}
IDENTITY = LogicalIdentity("retail", "shipments", "batch-a")
#: A cleanup budget no gated or held cleanup can meet, and a fast monitor: for the tests whose
#: cleanup ends unfinished on purpose, where the short budget is what they test.
FAST = {"startup_timeout": 1, "cleanup_timeout": 0.02, "wait_timeout": 2, "monitor_interval": 0.005}
#: The cleanup budget of a test whose cleanup is meant to finish. On the CI runner a stop here
#: takes about 3.6 ms at p99 with this file run alone, but inside a full run of the suite one took
#: longer than 0.2 s, and the test read the activation as unfinished. This is ten times that.
CLEANUP_THAT_FINISHES = 2.0


class Worker(ShipmentWorker):
    """A shipment worker that runs a test's hook at the end of startup or before shutdown."""

    def __init__(self, settings, runtime):
        super().__init__(settings, runtime)
        self.after_start = None
        self.before_stop = None

    async def on_startup(self):
        await super().on_startup()
        if self.after_start is not None:
            await self.after_start(self)

    async def on_shutdown(self):
        if self.before_stop is not None:
            await self.before_stop(self)
        await super().on_shutdown()


async def _hold(gate):
    """Wait for the gate, and go on waiting through cancellation."""
    while not gate.is_set():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            pass


@pytest.fixture
def connect(monkeypatch):
    """Stub the broker. A gate in the yielded list holds every subscription's unsubscribe."""
    unsubscribe_gates = []

    async def fake(*args, **kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False

        async def subscribe(*args, **kwargs):
            sub = AsyncMock()
            if unsubscribe_gates:
                gate = unsubscribe_gates[0]

                async def unsubscribe():
                    await _hold(gate)

                sub.unsubscribe = unsubscribe
            return sub

        async def close():
            nc.is_closed = True
            nc.is_connected = False

        nc.subscribe = subscribe
        nc.close = close
        nc.request.return_value = SimpleNamespace(
            data=json.dumps(describe(Worker).to_dict()).encode()
        )
        return nc

    monkeypatch.setattr("cliffracer.core.dial.connect", fake)
    yield unsubscribe_gates
    for gate in unsubscribe_gates:
        gate.set()


async def build(*, limits=None, start_gate=None, stop_gate=None, resist=False, configure=None):
    children = []

    def factory(settings, runtime):
        child = Worker(settings, runtime)
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
        or SupervisorLimits(
            startup_timeout=1, cleanup_timeout=CLEANUP_THAT_FINISHES, wait_timeout=2
        ),
    )
    host.register(shipment_template(service_class=Worker, factory=factory))
    await host.start()
    return host, await host.open_owner("retail"), children


@pytest.fixture
async def supervisors(_no_leaked_tasks, connect):
    """`build`, with every supervisor it built closed and drained before the leak check runs."""
    built = []

    async def make(**kwargs):
        host, owner, children = await build(**kwargs)
        built.append((host, children))
        return host, owner, children

    yield make
    for gate in connect:
        gate.set()
    for _, children in built:
        for child in children:
            for gate in (child.start_gate, child.stop_gate):
                if gate is not None:
                    gate.set()
    for host, children in built:
        await host.close()
        pending = host.unfinished_tasks
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
        if host._monitor_task is not None:
            host._monitor_task.cancel()
            await asyncio.gather(host._monitor_task, return_exceptions=True)
        for child in children:
            await child.stop()


async def ensure(host, owner, key="batch-a", settings=None, revision="warehouse-a"):
    return await host.ensure(
        owner, "shipments", key, SETTINGS if settings is None else settings, revision=revision
    )


async def wait_for_state(host, identity, state):
    async with asyncio.timeout(2):
        while True:
            snapshot = await host.inspect(identity)
            if snapshot is not None and snapshot.state == state:
                return snapshot
            await asyncio.sleep(0)


def running_task(name):
    found = [t for t in asyncio.all_tasks() if t.get_name() == name and not t.done()]
    assert len(found) == 1, [t.get_name() for t in asyncio.all_tasks()]
    return found[0]


@pytest.fixture
def lines():
    captured = []
    sink = logger.add(
        lambda message: captured.append((message.record["level"].name, str(message).rstrip("\n"))),
        level="DEBUG",
        format="{message}",
    )
    yield captured
    logger.remove(sink)


def failures(lines):
    return [text for level, text in lines if level in {"WARNING", "ERROR", "CRITICAL"}]


@pytest.mark.parametrize(
    "value, name",
    [
        (SupervisorLimits(), "max_active"),
        (OwnerHandle("incarnation", "identifier", "retail"), "scope"),
        (ActivationPage((), None), "cursor"),
        (CleanupReport(()), "activations"),
    ],
)
def test_the_supervisors_value_objects_are_frozen_and_hashable(value, name):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, name, getattr(value, name))
    assert hash(value) == hash(dataclasses.replace(value))


async def test_a_terminated_activation_says_its_state_and_reason(supervisors):
    host, owner, _ = await supervisors()
    await host.stop(await ensure(host, owner))
    with pytest.raises(ActivationTerminated) as stopped:
        await ensure(host, owner)
    assert str(ActivationState.STOPPED) in str(stopped.value)
    assert "stop requested" in str(stopped.value)


# --- start, loops and close ---------------------------------------------------------------------


async def test_a_closed_supervisor_cannot_start_again(supervisors):
    host, _, _ = await supervisors()
    await host.close()
    with pytest.raises(ActivationUnavailable, match="supervisor is closed"):
        await host.start()


async def test_starting_twice_runs_one_monitor(supervisors):
    host, _, _ = await supervisors()
    await host.start()
    running_task("service_supervisor")


def test_before_start_opening_an_owner_or_closing_is_refused_by_name():
    host = LocalSupervisor(ServiceConfig(name="shipments", health_port=0))
    for call in (lambda: host.open_owner("retail"), host.close):
        with pytest.raises(ActivationUnavailable, match="start the supervisor"):
            asyncio.run(call())


def test_a_supervisor_refuses_another_loop_and_is_unchanged_by_the_refusal():
    host = LocalSupervisor(ServiceConfig(name="shipments", health_port=0))
    loop = asyncio.new_event_loop()
    try:

        async def first():
            await host.start()
            return await host.open_owner("retail")

        owner = loop.run_until_complete(first())
        for call, refusal in (
            (host.start, "another event loop"),
            (lambda: host.open_owner("retail"), "start the supervisor on this event loop"),
            (lambda: host.close_owner(owner), "start the supervisor on this event loop"),
            (host.close, "start the supervisor on this event loop"),
        ):
            with pytest.raises(ActivationUnavailable, match=refusal):
                asyncio.run(call())
        nested = loop.run_until_complete(host.open_owner("wholesale", parent=owner))
        assert nested.scope == "wholesale"
        loop.run_until_complete(host.close())
    finally:
        loop.close()


@pytest.mark.parametrize("scenario", ["monitor", "stopping-waiter"])
def test_the_supervisor_gives_its_loop_back(scenario):
    """A loop that never yields cannot time itself out, so the bound is a separate process."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tests.fixtures.supervisor_loop_process", scenario],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{scenario}: the supervisor never gave its event loop back")
    assert result.returncode == 0, result.stderr
    assert f"DONE {scenario}" in result.stdout


async def test_close_does_not_wait_for_the_monitors_next_tick(supervisors):
    host, owner, _ = await supervisors(
        limits=SupervisorLimits(
            startup_timeout=1, cleanup_timeout=CLEANUP_THAT_FINISHES, monitor_interval=30
        )
    )
    await ensure(host, owner)
    assert (await asyncio.wait_for(host.close(), timeout=3)).complete


# --- owners ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["", "   "])
async def test_an_owner_scope_must_be_nonempty(supervisors, scope):
    host, _, _ = await supervisors()
    with pytest.raises(ValueError, match="scope"):
        await host.open_owner(scope)


async def test_nesting_depth_alone_bounds_a_child_owner(supervisors):
    host, owner, _ = await supervisors(limits=SupervisorLimits(max_depth=1))
    with pytest.raises(ActivationCapacityError, match="depth"):
        await host.open_owner("wholesale", parent=owner)


async def test_closing_an_owner_twice_returns_its_report(supervisors):
    host, owner, _ = await supervisors()
    await ensure(host, owner)
    first = await host.close_owner(owner)
    assert await host.close_owner(owner) == first


async def test_a_closed_owner_cannot_reactivate(supervisors):
    host, owner, children = await supervisors()
    first = await ensure(host, owner)
    await host.stop(first)
    await host.close_owner(owner)
    with pytest.raises(ActivationUnavailable, match="closed"):
        await host.reactivate(owner, first)
    assert len(children) == 1


async def test_an_owner_outlives_its_retention_while_its_activation_is_unfinished(
    supervisors, monkeypatch
):
    gate = asyncio.Event()
    host, owner, _ = await supervisors(limits=SupervisorLimits(**FAST), stop_gate=gate)
    await ensure(host, owner)
    report = await host.close_owner(owner)
    assert not report.complete
    clock = host._clock
    monkeypatch.setattr(host, "_clock", lambda: clock() + host.limits.retention + 1)
    assert (await host.inspect(IDENTITY)).state == ActivationState.UNFINISHED
    assert await host.close_owner(owner) == report
    monkeypatch.undo()


async def test_an_owner_closed_by_its_parent_is_pruned_after_retention(supervisors, monkeypatch):
    host, parent, _ = await supervisors()
    await host.open_owner("wholesale", parent=parent)
    assert (await host.close_owner(parent)).complete
    clock = host._clock
    monkeypatch.setattr(host, "_clock", lambda: clock() + host.limits.retention + 1)
    assert (await host.open_owner("returns")).scope == "returns"
    monkeypatch.undo()


async def test_closing_a_parent_survives_a_child_owner_expiring_during_its_wait(supervisors):
    gate = asyncio.Event()
    limits = SupervisorLimits(
        startup_timeout=1,
        cleanup_timeout=0.1,
        wait_timeout=2,
        retention=0.01,
        monitor_interval=0.002,
    )
    host, parent, _ = await supervisors(limits=limits, stop_gate=gate)
    child = await host.open_owner("wholesale", parent=parent)
    await ensure(host, parent)
    assert (await host.close_owner(child)).complete
    report = await host.close_owner(parent)
    assert not report.complete
    assert len(report.activations) == 1


async def test_closing_one_owner_keeps_watching_the_others(supervisors):
    # The fast monitor this test waits on, with room for the cleanup it expects to finish.
    limits = SupervisorLimits(**{**FAST, "cleanup_timeout": CLEANUP_THAT_FINISHES})
    host, owner, children = await supervisors(limits=limits)
    other = await host.open_owner("wholesale")
    await ensure(host, owner)
    assert (await host.close_owner(owner)).complete
    watched = await ensure(host, other)
    await asyncio.sleep(0.02)
    await children[1].stop()
    snapshot = await wait_for_state(host, watched.identity, ActivationState.FAILED)
    assert snapshot.reason == "child lifecycle terminated"


# --- ensure ---------------------------------------------------------------------------------------


class OtherSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse: str
    destinations: list[str]


async def test_another_revisions_order_is_a_conflict_before_its_settings_are_read(supervisors):
    host, owner, _ = await supervisors()
    await ensure(host, owner)
    host.register(
        shipment_template(
            revision="warehouse-b",
            service_class=Worker,
            settings_model=OtherSettings,
            factory=lambda settings, runtime: Worker(settings, runtime),
        )
    )
    with pytest.raises(ActivationConflict):
        await ensure(host, owner, revision="warehouse-b")


# --- startup --------------------------------------------------------------------------------------


async def test_a_construction_failure_is_a_failed_outcome(supervisors):
    def explode(child):
        raise RuntimeError("factory failed")

    host, owner, _ = await supervisors(configure=explode)
    with pytest.raises(ActivationTerminated) as failed:
        await ensure(host, owner)
    assert failed.value.snapshot.state == ActivationState.FAILED
    assert failed.value.snapshot.reason == "construction or startup failed"


async def test_a_startup_that_cancels_itself_says_so_and_reports_nothing_to_the_loop(supervisors):
    async def cancel(child):
        raise asyncio.CancelledError

    loop = asyncio.get_running_loop()
    reported = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: reported.append(context.get("message")))
    try:
        host, owner, _ = await supervisors(
            configure=lambda child: setattr(child, "after_start", cancel)
        )
        with pytest.raises(ActivationTerminated) as failed:
            await ensure(host, owner)
        await asyncio.sleep(0.01)
    finally:
        loop.set_exception_handler(previous)
    assert failed.value.snapshot.reason == "startup cancelled"
    assert reported == []


async def test_a_stop_accepted_before_startup_begins_constructs_nothing(supervisors):
    host, owner, children = await supervisors()
    waiting = asyncio.create_task(ensure(host, owner))
    await asyncio.sleep(0)
    snapshot = await host.inspect(IDENTITY)
    assert snapshot.state == ActivationState.STARTING
    assert not children
    outcome = await host.stop(snapshot.reference)
    with pytest.raises(ActivationTerminated):
        await waiting
    assert outcome.complete
    await asyncio.sleep(0.05)
    assert children == []


async def test_a_missed_startup_budget_names_no_exception_type(supervisors, lines):
    gate = asyncio.Event()
    limits = SupervisorLimits(startup_timeout=0.05, cleanup_timeout=0.2, wait_timeout=2)
    host, owner, _ = await supervisors(limits=limits, start_gate=gate)
    with pytest.raises(ActivationTerminated):
        await ensure(host, owner)
    named = [text for text in failures(lines) if "startup deadline expired" in text]
    assert named
    assert all(text.endswith("startup deadline expired") for text in named), named


def test_a_loop_torn_down_mid_startup_stops_the_activation(connect, lines):
    async def main():
        host, owner, children = await build(start_gate=asyncio.Event())
        asyncio.create_task(ensure(host, owner))
        while not children:
            await asyncio.sleep(0)
        await children[0].start_entered.wait()

    asyncio.run(main())
    assert any("failed: activation interrupted" in text for text in failures(lines)), lines


# --- cleanup --------------------------------------------------------------------------------------


async def test_a_late_lifecycle_cleanup_failure_is_the_outcomes_reason(supervisors):
    gate = asyncio.Event()
    host, owner, children = await supervisors(limits=SupervisorLimits(**FAST), stop_gate=gate)
    reference = await ensure(host, owner)
    children[0].fail_stop = True
    assert not (await host.stop(reference)).complete
    gate.set()
    await asyncio.gather(*host.unfinished_tasks, return_exceptions=True)
    await asyncio.sleep(0.05)
    assert (await host.inspect(reference.identity)).reason == "lifecycle cleanup failed"


async def test_a_late_cancelled_lifecycle_cleanup_is_logged_as_cancelled(supervisors, lines):
    gate = asyncio.Event()

    async def cancelled(child):
        await gate.wait()
        raise asyncio.CancelledError

    host, owner, _ = await supervisors(
        limits=SupervisorLimits(**FAST),
        configure=lambda child: setattr(child, "before_stop", cancelled),
    )
    reference = await ensure(host, owner)
    assert not (await host.stop(reference)).complete
    gate.set()
    await asyncio.sleep(0.1)
    named = [text for text in failures(lines) if "lifecycle cleanup failed" in text]
    assert named, failures(lines)
    assert "(cancelled)" in named[0]


async def test_a_stop_cancelled_inside_its_budget_returns_an_incomplete_outcome(supervisors):
    async def cancelled(child):
        raise asyncio.CancelledError

    host, owner, _ = await supervisors(
        configure=lambda child: setattr(child, "before_stop", cancelled)
    )
    reference = await ensure(host, owner)
    assert not (await host.stop(reference)).complete


async def test_a_child_whose_connection_stays_open_is_unfinished(supervisors):
    async def keep_open(child):
        async def close():
            pass

        child.nc.close = close

    host, owner, _ = await supervisors(
        limits=SupervisorLimits(**FAST),
        configure=lambda child: setattr(child, "after_start", keep_open),
    )
    reference = await ensure(host, owner)
    assert not (await host.stop(reference)).complete
    assert (await host.inspect(reference.identity)).state == ActivationState.UNFINISHED


async def test_unfinished_work_is_abandoned_and_listed(supervisors):
    gate = asyncio.Event()
    host, owner, _ = await supervisors(limits=SupervisorLimits(**FAST), stop_gate=gate)
    reference = await ensure(host, owner)
    assert not (await host.stop(reference)).complete
    pending = host.unfinished_tasks
    assert pending
    assert all(is_abandoned(task) and not task.done() for task in pending)


async def test_a_listener_still_unsubscribing_is_listed_and_abandoned(supervisors, connect):
    gate = asyncio.Event()
    connect.append(gate)
    host, owner, children = await supervisors(limits=SupervisorLimits(**FAST))
    reference = await ensure(host, owner)
    listeners = set(children[0].container.connection.subscriptions)
    assert listeners
    assert not (await host.stop(reference)).complete
    held = [task for task in listeners if not task.done()]
    assert held
    assert all(task in host.unfinished_tasks and is_abandoned(task) for task in held)


async def test_a_startup_still_running_at_an_unfinished_stop_is_abandoned_and_listed(supervisors):
    gate = asyncio.Event()
    limits = SupervisorLimits(startup_timeout=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await supervisors(limits=limits, start_gate=gate, resist=True)
    waiting = asyncio.create_task(ensure(host, owner))
    snapshot = await wait_for_state(host, IDENTITY, ActivationState.STARTING)
    while not children:
        await asyncio.sleep(0)
    await children[0].start_entered.wait()
    operation = running_task("service_activation")
    assert not (await host.stop(snapshot.reference)).complete
    assert not operation.done()
    assert is_abandoned(operation)
    assert operation in host.unfinished_tasks
    assert all(not task.done() for task in host.unfinished_tasks)
    gate.set()
    with pytest.raises(ActivationTerminated):
        await waiting


async def test_closing_over_unfinished_cleanup_abandons_a_monitor_that_ends_by_itself(
    supervisors,
):
    gate = asyncio.Event()
    host, owner, _ = await supervisors(limits=SupervisorLimits(**FAST), stop_gate=gate)
    await ensure(host, owner)
    monitor = running_task("service_supervisor")
    assert not (await host.close()).complete
    assert is_abandoned(monitor)
    gate.set()
    await wait_for_state(host, IDENTITY, ActivationState.STOPPED)
    await asyncio.wait_for(monitor, timeout=1)


async def test_an_unfinished_cleanup_is_reported_once_when_the_supervisor_closes(
    supervisors, lines
):
    gate = asyncio.Event()
    host, owner, _ = await supervisors(limits=SupervisorLimits(**FAST), stop_gate=gate)
    await ensure(host, owner)
    assert not (await host.close()).complete
    named = [text for text in failures(lines) if "cleanup did not finish" in text]
    assert len(named) == 1, named


# --- reads ----------------------------------------------------------------------------------------


async def test_a_successor_stays_current_after_its_predecessor_expires(supervisors, monkeypatch):
    host, owner, _ = await supervisors()
    first = await ensure(host, owner)
    await host.stop(first)
    second = await host.reactivate(owner, first)
    clock = host._clock
    monkeypatch.setattr(host, "_clock", lambda: clock() + host.limits.retention + 1)
    assert (await host.inspect(first.identity)).reference == second
    monkeypatch.undo()


async def test_a_ready_activation_reports_its_broker_state(supervisors):
    host, owner, _ = await supervisors()
    await ensure(host, owner)
    assert (await host.inspect(IDENTITY)).broker_state is not None


async def test_a_page_may_be_as_large_as_max_page(supervisors):
    host, owner, _ = await supervisors()
    await ensure(host, owner)
    page = await host.list_activations("retail", limit=host.limits.max_page)
    assert len(page.items) == 1
