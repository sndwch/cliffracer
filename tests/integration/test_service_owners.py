"""Parent lifetimes contain shipment children without interrupting another parent."""

import asyncio
import importlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import nats.errors
import pytest

import cliffracer
from cliffracer import ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.runners import LocalSupervisor, SupervisorLimits
from cliffracer.runners.contracts import ActivationState, ActivationUnavailable, LogicalIdentity
from tests.conftest import console_script

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "virtual_services"

#: The `src` directory this process imported `cliffracer` from. A child is pointed at it, after the
#: examples' root, so it runs the code under test: without it the child imports whatever the
#: interpreter has installed, which in a scratch copy of the tree is another tree's code.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])


@pytest.fixture
async def shipping(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE.parents[1]))
    orders = importlib.import_module("examples.virtual_services.orders")
    shipments = importlib.import_module("examples.virtual_services.shipments")
    children, parents = [], []
    gates = []
    hosts = []

    async def make(*, startup=None, shutdown=None, cleanup_timeout=1):
        def factory(settings, runtime):
            child = shipments.Shipments(settings, runtime)
            if startup is not None:
                child.on_startup = startup
            if shutdown is not None:
                child.on_shutdown = shutdown
            children.append(child)
            return child

        from dataclasses import replace

        host = LocalSupervisor(
            ServiceConfig(name="shipping_host", namespace="retail", health_port=0),
            limits=SupervisorLimits(cleanup_timeout=cleanup_timeout, monitor_interval=0.005),
        )
        host.register(replace(shipments.template(), factory=factory))
        await host.start()
        hosts.append(host)
        Orders = orders.order_service(host)

        def parent(parent_type=Orders):
            instance = parent_type(
                ServiceConfig(
                    name="orders_" + uuid.uuid4().hex,
                    namespace="retail",
                    health_port=0,
                    shutdown_timeout=0.02,
                )
            )
            parents.append(instance)
            return instance

        return host, Orders, parent

    yield make, children, gates, orders, shipments
    for gate in gates:
        gate.set()
    for parent in parents:
        await parent.stop()
    for host in hosts:
        await host.close()
        if host.unfinished_tasks:
            await asyncio.wait_for(
                asyncio.gather(*host.unfinished_tasks, return_exceptions=True), timeout=2
            )
        if host._monitor_task is not None:
            host._monitor_task.cancel()
            await asyncio.gather(host._monitor_task, return_exceptions=True)


async def dispatch(nc, parent, batch, warehouse="north", quantity=2):
    subject = HandlerDiscovery.outbound_subject(
        parent.config, parent.config.name, "rpc", "dispatch"
    )
    payload = {"batch": batch, "warehouse": warehouse, "quantity": quantity}
    reply = await nc.request(subject, json.dumps(payload).encode(), timeout=2)
    body = json.loads(reply.data)
    assert body["success"], body
    return body["result"]


async def snapshot(host, key):
    return await host.inspect(LogicalIdentity("retail", "shipments", key))


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def test_parent_shutdown_leaves_no_orphan_shipment(nats_connection, shipping):
    make, children, gates, orders, shipments = shipping
    host, Orders, parent = await make()
    north = parent()
    await north.start()
    assert (await dispatch(nats_connection, north, "batch-a"))["total"] == 2
    reference = (await snapshot(host, "batch-a")).reference
    await north.stop()
    subject = HandlerDiscovery.with_namespace(
        children[0].config, f"{reference.address.service}.describe"
    )
    with pytest.raises(nats.errors.NoRespondersError):
        await nats_connection.request(subject, b"", timeout=1)
    assert north.children.cleanup_report.complete
    assert children[0].health_listener.port is None
    assert children[0].nc.is_closed
    assert not children[0].container._subscriptions
    assert not host.unfinished_tasks


async def test_closing_north_parent_keeps_south_shipments_running(nats_connection, shipping):
    make, children, gates, orders, shipments = shipping
    host, Orders, parent = await make()
    north, south = parent(), parent()
    await north.start()
    await south.start()
    assert (await dispatch(nats_connection, north, "batch-a"))["total"] == 2
    assert (await dispatch(nats_connection, south, "batch-b", "south", 3))["total"] == 3
    await north.stop()
    receipt = await dispatch(nats_connection, south, "batch-b", "south", 2)
    assert (receipt["warehouse"], receipt["total"]) == ("south", 5)
    assert north.children.supervisor is south.children.supervisor is host
    assert north.children.owner != south.children.owner
    assert (await snapshot(host, "batch-a")).state == ActivationState.STOPPED
    assert (await snapshot(host, "batch-b")).state == ActivationState.READY


@pytest.mark.parametrize("interrupt", ["failure", "cancel"])
async def test_parent_startup_interruption_closes_already_created_shipments(
    nats_connection, shipping, interrupt
):
    make, children, gates, orders, shipments = shipping
    host, Orders, parent = await make()
    accepted = asyncio.Event()

    class PreparingOrders(Orders):
        async def on_startup(self):
            await self.children.ensure(
                "shipments",
                "batch-a",
                {"warehouse": "north", "batch": "batch-a"},
                revision="warehouse-a",
            )
            accepted.set()
            if interrupt == "failure":
                raise RuntimeError("order preparation failed")
            await asyncio.Event().wait()

    north = parent(PreparingOrders)
    starting = asyncio.create_task(north.start())
    await asyncio.wait_for(accepted.wait(), timeout=2)
    if interrupt == "cancel":
        starting.cancel()
    with pytest.raises(RuntimeError if interrupt == "failure" else asyncio.CancelledError):
        await starting
    assert north.children.cleanup_report.complete
    assert (await snapshot(host, "batch-a")).state == ActivationState.STOPPED
    assert children[0].nc.is_closed
    assert children[0].health_listener.port is None
    with pytest.raises(ActivationUnavailable, match="closed"):
        await north.children.ensure(
            "shipments",
            "batch-b",
            {"warehouse": "north", "batch": "batch-b"},
            revision="warehouse-a",
        )


async def test_parent_cancellation_preserves_unfinished_child_cleanup_report(
    nats_connection, shipping
):
    make, children, gates, orders, shipments = shipping
    entered, release = asyncio.Event(), asyncio.Event()
    gates.append(release)

    async def finish_shipments():
        entered.set()
        await release.wait()

    host, Orders, parent = await make(shutdown=finish_shipments, cleanup_timeout=0.03)
    north = parent()
    await north.start()
    await dispatch(nats_connection, north, "batch-a")
    stopping = asyncio.create_task(north.stop())
    await asyncio.wait_for(entered.wait(), timeout=2)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    await wait_for(lambda: north.children.cleanup_report is not None)
    report = north.children.cleanup_report
    assert not report.complete
    assert report.activations[0].state == ActivationState.UNFINISHED
    assert host.unfinished_tasks
    assert north.nc.is_closed
    release.set()
    await asyncio.gather(*host.unfinished_tasks, return_exceptions=True)
    async with asyncio.timeout(2):
        while (await snapshot(host, "batch-a")).state != ActivationState.STOPPED:
            await asyncio.sleep(0)
    assert (await snapshot(host, "batch-a")).cleanup.complete


async def test_parent_closes_while_its_request_is_waiting_for_a_shipment_worker(
    nats_connection, shipping
):
    make, children, gates, orders, shipments = shipping
    entered, release = asyncio.Event(), asyncio.Event()
    gates.append(release)

    async def prepare_shipments():
        entered.set()
        await release.wait()

    host, Orders, parent = await make(startup=prepare_shipments)
    north = parent()
    await north.start()
    request = asyncio.create_task(dispatch(nats_connection, north, "batch-a"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await north.stop()
        assert north.children.cleanup_report.complete
        assert (await snapshot(host, "batch-a")).state == ActivationState.STOPPED
        assert children[0].nc.is_closed
        assert not children[0].container.is_running
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_example_runs_with_pregenerated_client_and_reports_business_effects(nats_connection):
    env = dict(os.environ, CLIFFRACER_NATS_URL=nats_connection.connected_url.geturl())
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "examples.virtual_services.orders",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, stderr.decode()
    evidence = json.loads(stdout.decode().splitlines()[-1])
    assert evidence == {
        "capacity": "capacity",
        "conflict": "conflict",
        "north_closed": True,
        "north_state": "stopped",
        "north_totals": [2, 5],
        "progress_updates": 4,
        "south_closed": True,
        "south_totals": [4, 5],
    }


def _example_env() -> dict[str, str]:
    """The generator child's environment: the examples' root first, then the imported `src`."""
    return dict(os.environ, PYTHONPATH=os.pathsep.join([str(EXAMPLE.parents[1]), IMPORTED_SRC]))


def test_the_generator_child_imports_the_cliffracer_this_process_imported():
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        env=_example_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)


def test_shipment_client_matches_its_class_before_any_activation():
    env = _example_env()
    result = subprocess.run(
        [
            console_script("cliffracer-generate-client"),
            "--class",
            "examples.virtual_services.shipments:Shipments",
            "--service",
            "shipments",
            "--namespace",
            "retail",
            "--out",
            str(EXAMPLE / "shipment_client.py"),
            "--check",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
