"""Shipment batches emit typed progress on retained, broker-enforced routes."""

import asyncio
import json

import pytest
from nats.js.errors import NotFoundError
from pydantic import ValidationError

from cliffracer import Output, OutputError, ServiceConfig, StreamDeclarationError, StreamSpec
from cliffracer.broker_permissions import broker_permissions
from cliffracer.core.correlation import CorrelationContext, correlation_id_var
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.outputs import OutputContract
from cliffracer.introspect import describe
from cliffracer.runners import LocalSupervisor, TemplateCatalog
from cliffracer.runners.contracts import ActivationConflict, ActivationState, LogicalIdentity
from cliffracer.runners.supervisor import ActivationTerminated
from tests.fixtures.shipment_outputs import (
    ShipmentProgress,
    ShipmentWorker,
    shipment_output_client_class,
    shipment_output_template,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

#: The streams the JetStream output test declares for its host, beside the ledger it adds itself.
STREAM_NAMES = ("SHIPMENT_PROGRESS", "SHIPMENT_FAILURES")


def shipping_config(**overrides):
    return ServiceConfig(
        **{
            "name": "shipping_host",
            "namespace": "retail",
            "subject_prefix": "east",
            "health_port": 0,
            "nats_inbox_prefix": "_INBOX.shipping",
            **overrides,
        }
    )


def shipping_host(**overrides):
    children = []

    def factory(settings, runtime):
        child = ShipmentWorker(settings, runtime)
        children.append(child)
        return child

    host = LocalSupervisor(shipping_config(**overrides))
    host.register(shipment_output_template(factory=factory))
    return host, children


async def ensure(host, owner, key, settings):
    return await host.ensure(owner, "shipments", key, settings, revision="shipping-a")


async def test_two_batches_publish_the_same_contract_to_separate_order_channels(nats_connection):
    host, children = shipping_host()
    messages = await nats_connection.subscribe("east.retail.batches.>")
    await nats_connection.flush()
    client_class = shipment_output_client_class()
    async with host:
        owner = await host.open_owner("retail")
        north = await ensure(host, owner, "north", {"warehouse": "first", "batch": "north"})
        south = await ensure(host, owner, "south", {"warehouse": "second", "batch": "south"})
        async with (
            north.bind(client_class, nc=nats_connection) as a,
            south.bind(client_class, nc=nats_connection) as b,
        ):
            assert await a.ship(order_id="order_1", quantity=2) == 2
            assert await b.ship(order_id="order_2", quantity=5) == 5
            assert await a.ship(order_id="order_3", quantity=3) == 5
            assert await a.finish() == 5
        received = [await messages.next_msg(timeout=2) for _ in range(4)]
        assert [message.subject for message in received] == [
            "east.retail.batches.north.orders.order_1.progress",
            "east.retail.batches.south.orders.order_2.progress",
            "east.retail.batches.north.orders.order_3.progress",
            "east.retail.batches.north.closed",
        ]
        payloads = [json.loads(message.data) for message in received]
        assert [payload["data"] for payload in payloads] == [
            {"quantity": 2, "stage": "sent"},
            {"quantity": 5, "stage": "sent"},
            {"quantity": 3, "stage": "sent"},
            {"shipped": 5},
        ]
        assert north.outputs.contract == south.outputs.contract
        assert north.outputs.accepts(payloads[0]["output"])
        assert not south.outputs.accepts(payloads[0]["output"])
        description = await nats_connection.request(
            HandlerDiscovery.with_namespace(
                children[0].config, children[0].config.name + ".describe"
            ),
            b"",
            timeout=2,
        )
        described = json.loads(description.data)
        assert {output["name"] for output in described["outputs"]} == {"progress", "summary"}
        inspection = repr(await host.inspect(north.identity))
        assert "warehouse-access-canary" not in inspection
        assert "internal_note" not in inspection
    await messages.unsubscribe()


async def test_retries_and_successors_retain_the_accepted_default_channel(
    nats_connection, monkeypatch
):
    monkeypatch.setenv("SHIPMENT_BATCH", "accepted")
    host, children = shipping_host()
    messages = await nats_connection.subscribe("east.retail.batches.>")
    await nats_connection.flush()
    async with host:
        owner = await host.open_owner("retail")
        first = await ensure(host, owner, "batch", {"warehouse": "north"})
        monkeypatch.setenv("SHIPMENT_BATCH", "redirected")
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "west")
        assert await ensure(host, owner, "batch", {"warehouse": "north"}) == first
        with pytest.raises(ActivationConflict):
            await ensure(host, owner, "batch", {"warehouse": "north", "batch": "redirected"})
        await children[0].progress.publish(
            ShipmentProgress(quantity=2, stage="packed"), parameters={"order_id": "one"}
        )
        old = json.loads((await messages.next_msg(timeout=2)).data)
        assert (await host.stop(first)).complete
        second = await host.reactivate(owner, first)
        assert await host.reactivate(owner, first) == second
        await children[1].progress.publish(
            ShipmentProgress(quantity=3, stage="sent"), parameters={"order_id": "two"}
        )
        message = await messages.next_msg(timeout=2)
        assert message.subject == "east.retail.batches.accepted.orders.two.progress"
        current = json.loads(message.data)
        assert second.generation == first.generation + 1
        assert second.outputs.publish_subjects == first.outputs.publish_subjects
        assert second.outputs.accepts(current["output"])
        assert not second.outputs.accepts(old["output"])
        assert first.outputs.accepts(old["output"])
        altered = {
            **current["output"],
            "producer": {**current["output"]["producer"], "generation": True},
        }
        assert not first.outputs.accepts(altered)
        assert not second.outputs.accepts({"name": "progress"})
    await messages.unsubscribe()


async def test_invalid_order_tokens_and_payloads_never_emit_a_shipment(nats_connection):
    host, children = shipping_host()
    messages = await nats_connection.subscribe("east.retail.batches.>")
    await nats_connection.flush()
    async with host:
        owner = await host.open_owner("retail")
        await ensure(host, owner, "batch", {"warehouse": "north", "batch": "north"})
        child = children[0]
        for order in ("one.extra", "*", ">", "", "one two", "{other}"):
            try:
                await child.progress.publish(
                    ShipmentProgress(quantity=8, stage="sent"), parameters={"order_id": order}
                )
            except OutputError:
                pass
        try:
            await child.progress.publish(
                ShipmentProgress.model_construct(quantity=-7, stage="sent"),
                parameters={"order_id": "confirmed"},
            )
        except ValidationError:
            pass
        await child.progress.publish(
            ShipmentProgress(quantity=3, stage="sent"), parameters={"order_id": "confirmed"}
        )
        await child.nc.flush()
        await nats_connection.flush()
        message = await messages.next_msg(timeout=2)
        remaining = messages.pending_msgs
        await messages.unsubscribe()
        assert message.subject == "east.retail.batches.north.orders.confirmed.progress"
        assert json.loads(message.data)["data"] == {"quantity": 3, "stage": "sent"}
        assert remaining == 0


async def test_publication_retains_correlation_and_send_hooks(nats_connection):
    host, children = shipping_host()
    messages = await nats_connection.subscribe("east.retail.batches.north.orders.one.progress")
    await nats_connection.flush()
    async with host:
        owner = await host.open_owner("retail")
        await ensure(host, owner, "batch", {"warehouse": "north", "batch": "north"})
        token = CorrelationContext.set("shipment-correlation")
        try:
            await children[0].progress.publish(
                ShipmentProgress(quantity=3, stage="sent"), parameters={"order_id": "one"}
            )
        finally:
            correlation_id_var.reset(token)
        message = await messages.next_msg(timeout=2)
        assert json.loads(message.data)["correlation_id"] == "shipment-correlation"
        assert message.headers["X-Correlation-ID"] == "shipment-correlation"
    await messages.unsubscribe()


async def _remove_streams(js, names):
    """Delete the streams that exist among `names`; a missing one is not an error."""
    for name in names:
        try:
            await js.delete_stream(name)
        except NotFoundError:
            pass


async def test_jetstream_outputs_need_declared_coverage_and_distinguish_generations(
    nats_connection,
):
    ledger = nats_connection.jetstream()
    ledger_config = shipping_config(name="returns_ledger")
    ledger_name = ledger_config.prefixed_name("SHIPMENT_FOREIGN_LEDGER")
    # The test's config writes `subject_prefix` itself, so these names carry no session prefix and
    # the suite's teardown does not sweep them. They are removed here, before the test reads any
    # state it expects to be empty and after it whatever happens.
    names = [ledger_name, *(ledger_config.prefixed_name(n) for n in STREAM_NAMES)]
    await _remove_streams(ledger, names)
    try:
        await _jetstream_outputs_need_declared_coverage(nats_connection, ledger, ledger_name)
    finally:
        await _remove_streams(ledger, names)


async def _jetstream_outputs_need_declared_coverage(nats_connection, ledger, ledger_name):
    ledger_config = shipping_config(name="returns_ledger")
    await ledger.add_stream(
        name=ledger_name,
        subjects=[HandlerDiscovery.with_namespace(ledger_config, "batches.north.closed")],
    )
    host, children = shipping_host(
        jetstream_enabled=True,
        idempotent_publishing=True,
        jetstream_streams=[
            StreamSpec(
                name="SHIPMENT_PROGRESS", subjects=["retail.batches.north.orders.*.progress"]
            ),
            StreamSpec(name="SHIPMENT_FAILURES", subjects=["dlq.*"]),
        ],
    )
    async with host:
        owner = await host.open_owner("retail")
        first = await ensure(host, owner, "batch", {"warehouse": "north", "batch": "north"})
        child = children[0]
        event = ShipmentProgress(quantity=3, stage="sent")
        assert not (await child.progress.publish(event, parameters={"order_id": "one"})).duplicate
        assert (await child.progress.publish(event, parameters={"order_id": "one"})).duplicate
        try:
            await child.finish()
        except StreamDeclarationError:
            pass
        assert (await ledger.stream_info(ledger_name)).state.messages == 0
        assert (await host.stop(first)).complete
        await host.reactivate(owner, first)
        successor = await children[1].progress.publish(event, parameters={"order_id": "one"})
        status = await nats_connection.jetstream().stream_info(
            child.config.prefixed_name("SHIPMENT_PROGRESS")
        )
        assert status.state.messages == 2
        assert not successor.duplicate


async def test_runtime_output_drift_fails_live_readiness_and_closes_the_child(
    nats_connection, monkeypatch
):
    replacement = Output(
        ShipmentProgress,
        "different.{batch}.orders.{order_id}",
        settings=("batch",),
        parameters=("order_id",),
    )

    class Worker(ShipmentWorker):
        async def on_startup(self):
            replacement.__set_name__(Worker, "progress")
            monkeypatch.setattr(Worker, "progress", replacement)

    children = []

    def factory(settings, config):
        child = Worker(settings, config)
        children.append(child)
        return child

    host = LocalSupervisor(shipping_config())
    registered = host.register(shipment_output_template(service_class=Worker, factory=factory))
    async with host:
        owner = await host.open_owner("retail")
        try:
            await ensure(host, owner, "batch", {"warehouse": "north", "batch": "north"})
        except ActivationTerminated:
            pass
        snapshot = await host.inspect(LogicalIdentity("retail", "shipments", "batch"))
        assert snapshot.state == ActivationState.FAILED
        assert snapshot.cleanup.complete
        assert children[0].nc.is_closed
        assert registered.output_contract != OutputContract(tuple(describe(Worker).outputs))


async def test_accepted_routes_work_only_when_the_broker_grants_them(secured_broker):
    start, connect, serve, errors = secured_broker
    template = TemplateCatalog().register(shipment_output_template())
    accepted = template.normalize({"warehouse": "north", "batch": "north"})
    other = template.normalize({"warehouse": "south", "batch": "south"})
    config = shipping_config()
    planned = template.bind_outputs(accepted, config)
    other_config = config.model_copy(
        update={"name": "other_shipping_host", "nats_inbox_prefix": "_INBOX.other_shipping"}
    )
    url, inspector = await start(
        {
            "warehouse": broker_permissions(
                ShipmentWorker, config, role="service", extra_publish=planned.publish_subjects
            ),
            "other_warehouse": broker_permissions(ShipmentWorker, other_config, role="service"),
        }
    )
    child = await serve(
        lambda assigned: template.construct(accepted, assigned), config, url, "warehouse"
    )
    ungranted = await serve(
        lambda assigned: template.construct(other, assigned), other_config, url, "other_warehouse"
    )
    messages = await inspector.subscribe("east.retail.batches.>")
    await inspector.flush()
    await child.progress.publish(
        ShipmentProgress(quantity=3, stage="sent"), parameters={"order_id": "one"}
    )
    assert (
        await messages.next_msg(timeout=2)
    ).subject == "east.retail.batches.north.orders.one.progress"
    delivery = asyncio.create_task(messages.next_msg(timeout=2))
    refusal = asyncio.create_task(errors["other_warehouse"].get())
    try:
        await ungranted.progress.publish(
            ShipmentProgress(quantity=9, stage="sent"), parameters={"order_id": "one"}
        )
        await ungranted.nc.flush()
        done, _ = await asyncio.wait(
            {delivery, refusal}, timeout=2, return_when=asyncio.FIRST_COMPLETED
        )
        assert done
        if delivery in done:
            pytest.fail(f"an ungranted shipment reached {delivery.result().subject}")
        assert "permissions violation for publish" in str(refusal.result())
        assert errors["warehouse"].empty()
    finally:
        delivery.cancel()
        refusal.cancel()
        await asyncio.gather(delivery, refusal, return_exceptions=True)
        await messages.unsubscribe()
