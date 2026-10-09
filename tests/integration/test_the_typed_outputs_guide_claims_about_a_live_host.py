"""Claims `docs/typed-outputs.md` makes about a supervised host that no other test pins.

The producer object on a typed event carries the identity the supervisor assigned, and a successor
carries the next generation under its own assigned name. A JetStream output publishes only to a
stream its service declared; publishing creates none.
"""

import json

import pytest
from nats.js.errors import NotFoundError

from cliffracer import ServiceConfig, StreamDeclarationError, StreamSpec
from cliffracer.runners import LocalSupervisor
from tests.fixtures.shipment_outputs import (
    ShipmentProgress,
    ShipmentWorker,
    shipment_output_template,
)

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

COVERED = "SHIPMENT_GUIDE_COVERED"


def host_config(**overrides):
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

    host = LocalSupervisor(host_config(**overrides))
    host.register(shipment_output_template(factory=factory))
    return host, children


async def test_the_supervisor_stamps_scope_template_key_revision_incarnation_generation_service(
    nats_connection,
):
    host, children = shipping_host()
    messages = await nats_connection.subscribe("east.retail.batches.north.closed")
    await nats_connection.flush()
    async with host:
        owner = await host.open_owner("retail")
        first = await host.ensure(
            owner, "shipments", "north", {"warehouse": "w", "batch": "north"}, revision="shipping-a"
        )
        await children[0].summary.publish(ShipmentWorker.summary.model(shipped=1))
        stamped = json.loads((await messages.next_msg(timeout=2)).data)["output"]["producer"]
        assert stamped == {
            "scope": "retail",
            "template": "shipments",
            "key": "north",
            "revision": "shipping-a",
            "incarnation": host.incarnation,
            "generation": 1,
            "service": children[0].config.name,
        }
        assert first.outputs.producer.service == children[0].config.name

        assert (await host.stop(first)).complete
        await host.reactivate(owner, first)
        await children[1].summary.publish(ShipmentWorker.summary.model(shipped=2))
        successor = json.loads((await messages.next_msg(timeout=2)).data)["output"]["producer"]
        assert successor == {
            **stamped,
            "generation": 2,
            "service": children[1].config.name,
        }
        assert successor["service"] != stamped["service"]
    await messages.unsubscribe()


async def test_publishing_to_an_uncovered_subject_is_refused_and_creates_no_stream(
    nats_connection,
):
    config = host_config()
    js = nats_connection.jetstream()
    declared = config.prefixed_name(COVERED)
    subject = "east.retail.batches.north.closed"
    try:
        await js.delete_stream(declared)
    except NotFoundError:
        pass
    host, children = shipping_host(
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name=COVERED, subjects=["dlq.*"])],
    )
    try:
        async with host:
            owner = await host.open_owner("retail")
            await host.ensure(
                owner,
                "shipments",
                "north",
                {"warehouse": "w", "batch": "north"},
                revision="shipping-a",
            )
            with pytest.raises(StreamDeclarationError):
                await children[0].summary.publish(ShipmentWorker.summary.model(shipped=1))
            with pytest.raises(StreamDeclarationError):
                await children[0].progress.publish(
                    ShipmentProgress(quantity=1, stage="sent"), parameters={"order_id": "one"}
                )
            with pytest.raises(NotFoundError):
                await js.find_stream_name_by_subject(subject)
            with pytest.raises(NotFoundError):
                await js.find_stream_name_by_subject(
                    "east.retail.batches.north.orders.one.progress"
                )
    finally:
        try:
            await js.delete_stream(declared)
        except NotFoundError:
            pass
