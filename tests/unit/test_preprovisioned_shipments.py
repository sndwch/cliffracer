"""Pre-provisioned shipment consumers are bound only when their contracts match."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import NotFoundError

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener
from cliffracer.core.jetstream import ConsumerBindingError, validate_bound_consumer

pytestmark = pytest.mark.unit


def shipment_config() -> ServiceConfig:
    return ServiceConfig(
        name="shipments",
        jetstream_enabled=True,
        jetstream_resource_mode="bind",
        jetstream_ack_wait=12,
        jetstream_max_deliver=4,
        jetstream_max_ack_pending=8,
    )


def shipment_consumer(*, durable: str, subject: str, pull: bool) -> ConsumerConfig:
    return ConsumerConfig(
        name=durable,
        durable_name=durable,
        filter_subject=subject,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=12,
        max_deliver=4,
        max_ack_pending=8,
        deliver_subject=None if pull else "_INBOX.shippers.delivery",
        deliver_group=None if pull else durable,
    )


@pytest.mark.parametrize("pull", [False, True])
async def test_matching_push_and_pull_shipment_consumers_are_accepted(pull: bool):
    durable = "dispatching" if pull else "packing"
    subject = "shipments.sent" if pull else "shipments.packed"
    js = AsyncMock()
    info = SimpleNamespace(config=shipment_consumer(durable=durable, subject=subject, pull=pull))
    js.consumer_info.return_value = info

    assert (
        await validate_bound_consumer(
            js,
            stream="SHIPMENTS",
            durable=durable,
            subject=subject,
            pull=pull,
            config=shipment_config(),
        )
        is info
    )
    js.consumer_info.assert_awaited_once_with("SHIPMENTS", durable)


async def test_a_missing_shipment_consumer_names_what_the_operator_must_create():
    js = AsyncMock()
    js.consumer_info.side_effect = NotFoundError()
    with pytest.raises(ConsumerBindingError) as exc:
        await validate_bound_consumer(
            js,
            stream="SHIPMENTS",
            durable="packing",
            subject="shipments.packed",
            pull=False,
            config=shipment_config(),
        )
    message = str(exc.value)
    assert "packing" in message
    assert "SHIPMENTS" in message
    assert "shipments.packed" in message
    assert "Create it" in message


async def test_a_shipment_consumer_for_the_wrong_subject_is_refused_before_binding():
    js = AsyncMock()
    js.consumer_info.return_value = SimpleNamespace(
        config=shipment_consumer(durable="packing", subject="payments.captured", pull=False)
    )
    with pytest.raises(ConsumerBindingError) as exc:
        await validate_bound_consumer(
            js,
            stream="SHIPMENTS",
            durable="packing",
            subject="shipments.packed",
            pull=False,
            config=shipment_config(),
        )
    message = str(exc.value)
    assert "payments.captured" in message
    assert "shipments.packed" in message
    assert "Recreate or update" in message


async def test_a_push_listener_refuses_a_pull_shipment_consumer():
    js = AsyncMock()
    js.consumer_info.return_value = SimpleNamespace(
        config=shipment_consumer(durable="packing", subject="shipments.packed", pull=True)
    )
    with pytest.raises(ConsumerBindingError, match="expected a push consumer"):
        await validate_bound_consumer(
            js,
            stream="SHIPMENTS",
            durable="packing",
            subject="shipments.packed",
            pull=False,
            config=shipment_config(),
        )


async def test_a_shipment_delivery_inbox_owned_by_another_role_is_refused():
    js = AsyncMock()
    consumer = shipment_consumer(durable="packing", subject="shipments.packed", pull=False)
    consumer.deliver_subject = "_INBOX.payments.delivery"
    js.consumer_info.return_value = SimpleNamespace(config=consumer)
    config = shipment_config().model_copy(update={"nats_inbox_prefix": "_INBOX.shippers"})
    with pytest.raises(ConsumerBindingError) as exc:
        await validate_bound_consumer(
            js,
            stream="SHIPMENTS",
            durable="packing",
            subject="shipments.packed",
            pull=False,
            config=config,
        )
    message = str(exc.value)
    assert "_INBOX.payments.delivery" in message
    assert "_INBOX.shippers" in message


async def test_a_named_ephemeral_shipment_consumer_is_not_mistaken_for_a_durable():
    js = AsyncMock()
    consumer = shipment_consumer(durable="packing", subject="shipments.packed", pull=False)
    consumer.durable_name = None
    js.consumer_info.return_value = SimpleNamespace(config=consumer)

    with pytest.raises(ConsumerBindingError, match="durable_name.*expected 'packing'"):
        await validate_bound_consumer(
            js,
            stream="SHIPMENTS",
            durable="packing",
            subject="shipments.packed",
            pull=False,
            config=shipment_config(),
        )


class ShipmentSorting(CliffracerService):
    @listener("shipments.packed", durable="packing")
    async def pack(self, quantity: int) -> None:
        pass

    @listener("shipments.sent", durable="dispatching", pull=True)
    async def dispatch(self, quantity: int) -> None:
        pass


async def test_shipment_listeners_use_non_provisioning_bind_apis_and_validated_push_config():
    config = shipment_config().model_copy(
        update={
            "jetstream_streams": [
                StreamSpec(name="SHIPMENTS", subjects=["shipments.*"]),
            ]
        }
    )
    service = ShipmentSorting(config)
    service.nc = AsyncMock()
    service.js = AsyncMock()
    service._discover_handlers()

    packing = shipment_consumer(durable="packing", subject="shipments.packed", pull=False)
    dispatching = shipment_consumer(durable="dispatching", subject="shipments.sent", pull=True)
    consumers = {
        "packing": SimpleNamespace(config=packing),
        "dispatching": SimpleNamespace(config=dispatching),
    }

    async def consumer_info(stream: str, durable: str):
        assert stream == "SHIPMENTS"
        return consumers[durable]

    service.js.consumer_info.side_effect = consumer_info

    await service.container._setup_subscriptions()

    service.js.subscribe.assert_not_awaited()
    service.js.pull_subscribe.assert_not_awaited()
    service.js.subscribe_bind.assert_awaited_once()
    push_call = service.js.subscribe_bind.await_args
    assert push_call.kwargs["stream"] == "SHIPMENTS"
    assert push_call.kwargs["consumer"] == "packing"
    assert push_call.kwargs["config"] is packing
    service.js.pull_subscribe_bind.assert_awaited_once_with(
        consumer="dispatching", stream="SHIPMENTS"
    )
