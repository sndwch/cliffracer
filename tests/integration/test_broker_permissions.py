"""Order and shipment roles operate under broker-enforced subject grants."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace

import nats
import pytest
from nats.js.api import ConsumerConfig

from cliffracer import CliffracerService, ConsumerBindingError, ServiceConfig, StreamSpec, rpc
from cliffracer.broker_permissions import (
    RESPONSE_GRANT_MARGIN,
    BrokerPermissions,
    broker_permissions,
)
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.jetstream import (
    StreamDeclarationError,
    consumer_config_for,
)
from cliffracer.introspect import Description, describe
from tests.fixtures.permission_orders import Orders, ShipmentLedger, Shipments
from tests.fixtures.permission_orders_client import OrdersClient

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


def order_config():
    return ServiceConfig(
        name="orders",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.order_workers",
        health_port=0,
    )


def bound_shipment_config():
    return ServiceConfig(
        name="shipments",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.shippers",
        health_port=0,
        jetstream_enabled=True,
        jetstream_resource_mode="bind",
        jetstream_max_deliver=1,
        jetstream_streams=[
            StreamSpec(name="SHIPMENTS", subjects=["retail.shipments.*"]),
            StreamSpec(name="FAILURES", subjects=["dlq.shipments"]),
        ],
    )


async def provision_bound_shipments(
    js, config, *, consumers=True, packed_subject=None, packing_is_durable=True
):
    for stream in config.effective_jetstream_streams:
        await js.add_stream(config=stream.to_stream_config())
    if not consumers:
        return
    tuning = consumer_config_for(config)
    shipment_stream = config.prefixed_name("SHIPMENTS")
    packing = config.prefixed_name("packing")
    dispatching = config.prefixed_name("dispatching")
    await js.add_consumer(
        shipment_stream,
        config=ConsumerConfig(
            name=packing,
            durable_name=packing if packing_is_durable else None,
            filter_subject=packed_subject or "east.retail.shipments.packed",
            deliver_subject="_INBOX.shippers.packing",
            deliver_group=packing,
            ack_policy=tuning.ack_policy,
            ack_wait=tuning.ack_wait,
            max_deliver=tuning.max_deliver,
            max_ack_pending=tuning.max_ack_pending,
        ),
    )
    await js.add_consumer(
        shipment_stream,
        config=ConsumerConfig(
            name=dispatching,
            durable_name=dispatching,
            filter_subject="east.retail.shipments.sent",
            ack_policy=tuning.ack_policy,
            ack_wait=tuning.ack_wait,
            max_deliver=tuning.max_deliver,
            max_ack_pending=tuning.max_ack_pending,
        ),
    )


async def publish_denied(sender, inspector, errors, subject):
    """A denied parcel neither reaches its destination nor passes unnoticed."""
    receiver = await inspector.subscribe(subject)
    await inspector.flush()
    delivery = asyncio.create_task(receiver.next_msg(timeout=2))
    refusal = asyncio.create_task(errors.get())
    try:
        await sender.publish(subject, b'{"quantity":19}')
        await sender.flush()
        done, _ = await asyncio.wait(
            {delivery, refusal}, timeout=2, return_when=asyncio.FIRST_COMPLETED
        )
        assert done, "broker neither refused nor delivered the parcel"
        if delivery in done:
            message = delivery.result()
            pytest.fail(f"unauthorized parcel reached {message.subject}: {message.data!r}")
        error = refusal.result()
        assert "permissions violation" in str(error).lower(), error
        assert str(error) == f'nats: permissions violation for publish to "{subject.lower()}"'
    finally:
        for task in (delivery, refusal):
            task.cancel()
        await asyncio.gather(delivery, refusal, return_exceptions=True)
        await receiver.unsubscribe()


async def subscribe_denied(connection, inspector, errors, subject):
    sub = await connection.subscribe(subject)
    delivery = asyncio.create_task(sub.next_msg(timeout=2))
    refusal = asyncio.create_task(errors.get())
    try:
        await connection.flush()
        concrete = subject.replace("*", "wholesale").replace(">", "receipt")
        await inspector.publish(concrete, b'{"quantity":19}')
        await inspector.flush()
        done, _ = await asyncio.wait(
            {delivery, refusal}, timeout=2, return_when=asyncio.FIRST_COMPLETED
        )
        assert done, "broker neither refused the subscription nor delivered the parcel"
        if delivery in done:
            message = delivery.result()
            pytest.fail(
                f"another role's parcel was disclosed on {message.subject}: {message.data!r}"
            )
        error = refusal.result()
        assert "permissions violation" in str(error).lower(), error
        assert str(error) == f'nats: permissions violation for subscription to "{subject.lower()}"'
    finally:
        for task in (delivery, refusal):
            task.cancel()
        await asyncio.gather(delivery, refusal, return_exceptions=True)
        await sub.unsubscribe()


async def test_order_calls_events_and_progress_use_the_derived_role(secured_broker):
    start, connect, serve, errors = secured_broker
    config = order_config()
    service_policy = broker_permissions(
        Description.from_dict(describe(Orders, config=config).to_dict()),
        config,
        role="service",
        extra_publish=["east.retail.shipments.progress"],
    )
    client_policy = broker_permissions(
        Orders, config, role="client", inbox_prefix="_INBOX.customer", allow_async=True
    )
    url, inspector = await start({"warehouse": service_policy, "customer": client_policy})
    orders = await serve(Orders, config, url, "warehouse")
    customer = await connect(url, "customer", inbox_prefix="_INBOX.customer")
    async with OrdersClient(
        nats_url=url.replace("nats://", "nats://customer:disposable@"),
        inbox_prefix="_INBOX.customer",
        subject_prefix="east",
        timeout=2,
    ) as client:
        assert await client.reserve(quantity=3) == 3
    orders.arrived.clear()
    await customer.publish("east.retail.orders.async.reserve", b'{"quantity":2}')
    await asyncio.wait_for(orders.arrived.wait(), timeout=2)
    orders.arrived.clear()
    await inspector.publish("east.wholesale.orders.created", b'{"quantity":4}')
    await asyncio.wait_for(orders.arrived.wait(), timeout=2)
    orders.arrived.clear()
    await inspector.publish("east.wholesale.orders.returned", b'{"quantity":1}')
    await asyncio.wait_for(orders.arrived.wait(), timeout=2)
    assert orders.quantities == [3, 2, 4, -1]
    progress = await inspector.subscribe("east.retail.shipments.progress")
    await inspector.flush()
    await orders.publish_event("shipments.progress", quantity=8)
    message = await progress.next_msg(timeout=2)
    assert json.loads(message.data)["data"]["quantity"] == 8
    await progress.unsubscribe()
    assert errors["warehouse"].empty()
    assert errors["customer"].empty()


async def test_customer_and_worker_cannot_reach_other_roles_or_undeclared_calls(secured_broker):
    start, connect, serve, errors = secured_broker
    config = order_config()
    url, inspector = await start(
        {
            "warehouse": broker_permissions(Orders, config, role="service"),
            "customer": broker_permissions(
                Orders, config, role="client", inbox_prefix="_INBOX.customer"
            ),
        }
    )
    orders = await serve(Orders, config, url, "warehouse")
    customer = await connect(url, "customer", inbox_prefix="_INBOX.customer")
    await publish_denied(customer, inspector, errors["customer"], "east.retail.orders.rpc.cancel")
    await publish_denied(customer, inspector, errors["customer"], "west.retail.orders.rpc.reserve")
    await publish_denied(
        orders.nc, inspector, errors["warehouse"], "_INBOX.another_customer.receipt"
    )
    await subscribe_denied(customer, inspector, errors["customer"], "_INBOX.another_customer.>")
    await subscribe_denied(orders.nc, inspector, errors["warehouse"], "west.*.orders.created")
    assert orders.quantities == []


async def test_an_empty_role_cannot_publish_or_subscribe(secured_broker):
    start, connect, serve, errors = secured_broker
    url, inspector = await start({"visitor": BrokerPermissions()})
    visitor = await connect(url, "visitor", inbox_prefix="_INBOX.visitor")
    await publish_denied(visitor, inspector, errors["visitor"], "retail.orders.created")
    await subscribe_denied(visitor, inspector, errors["visitor"], "retail.orders.created")


@pytest.mark.parametrize("finished", ["replied", "expired"])
async def test_a_worker_cannot_send_an_extra_or_expired_order_receipt(secured_broker, finished):
    start, connect, serve, errors = secured_broker
    config = order_config()
    ttl = 0.01 if finished == "expired" else 30
    url, inspector = await start(
        {"warehouse": broker_permissions(Orders, config, role="service", response_ttl=ttl)}
    )
    worker = await connect(url, "warehouse", inbox_prefix=config.nats_inbox_prefix)
    requests = await worker.subscribe("east.retail.orders.rpc.reserve")
    receipts = await inspector.subscribe("_INBOX.customer.order_receipt")
    await worker.flush()
    await inspector.flush()
    try:
        await inspector.publish(
            "east.retail.orders.rpc.reserve",
            b'{"quantity":3}',
            reply="_INBOX.customer.order_receipt",
        )
        request = await requests.next_msg(timeout=2)
        if finished == "replied":
            await request.respond(b'{"reserved":3}')
            assert json.loads((await receipts.next_msg(timeout=2)).data) == {"reserved": 3}
        else:
            # The response grant was issued before the request reached this subscriber.
            await asyncio.sleep(0.05)
        await publish_denied(worker, inspector, errors["warehouse"], request.reply)
    finally:
        await requests.unsubscribe()
        await receipts.unsubscribe()


async def test_shipments_provision_consume_acknowledge_dead_letter_and_rebind(secured_broker):
    start, connect, serve, errors = secured_broker
    config = ServiceConfig(
        name="shipments",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.shippers",
        health_port=0,
        jetstream_enabled=True,
        jetstream_max_deliver=1,
        jetstream_streams=[
            StreamSpec(name="SHIPMENTS", subjects=["retail.shipments.*"]),
            StreamSpec(name="FAILURES", subjects=["dlq.shipments"]),
        ],
    )
    # Shipment consumers acknowledge on their explicit stream/consumer subjects.
    url, inspector = await start(
        {"warehouse": broker_permissions(Shipments, config, role="service", response_ttl=None)}
    )
    shipments = await serve(Shipments, config, url, "warehouse")
    await shipments.publish_event("shipments.packed", quantity=2)
    await asyncio.wait_for(shipments.arrived.wait(), timeout=2)
    shipments.arrived.clear()
    await shipments.publish_event("shipments.sent", quantity=4)
    await asyncio.wait_for(shipments.arrived.wait(), timeout=2)
    assert shipments.shipped == [2, 4]
    failed = await inspector.subscribe("east.dlq.shipments")
    await inspector.flush()
    await shipments.publish_event("shipments.sent", quantity=-1)
    message = await failed.next_msg(timeout=3)
    assert json.loads(message.data)["original_subject"] == "east.retail.shipments.sent"
    await failed.unsubscribe()
    js = inspector.jetstream()
    async with asyncio.timeout(3):
        while True:
            status = await js.consumer_info(
                config.prefixed_name("SHIPMENTS"), config.prefixed_name("dispatching")
            )
            if status.num_ack_pending == 0:
                break
            await asyncio.sleep(0.01)
    assert errors["warehouse"].empty()
    await publish_denied(
        shipments.nc, inspector, errors["warehouse"], "$JS.API.STREAM.CREATE.PAYMENTS"
    )
    await publish_denied(
        shipments.nc,
        inspector,
        errors["warehouse"],
        "$JS.API.CONSUMER.DELETE.east_SHIPMENTS.east_packing",
    )
    await shipments.stop()
    replacement = await serve(Shipments, config, url, "warehouse")
    await replacement.publish_event("shipments.packed", quantity=7)
    await asyncio.wait_for(replacement.arrived.wait(), timeout=2)
    assert replacement.shipped == [7]
    assert shipments.shipped == [2, 4]
    assert errors["warehouse"].empty()


async def test_a_shipment_ledger_can_consume_every_declared_stream_subject(secured_broker):
    start, connect, serve, errors = secured_broker
    config = ServiceConfig(
        name="ledger",
        namespace=None,
        subject_prefix=None,
        nats_inbox_prefix="_INBOX.ledger",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="SHIPMENTS", subjects=["shipments.*", "dlq.ledger"]),
        ],
    )
    url, inspector = await start(
        {"warehouse": broker_permissions(ShipmentLedger, config, role="service", response_ttl=None)}
    )
    ledger = await serve(ShipmentLedger, config, url, "warehouse")
    await ledger.publish_event("shipments.delivered", quantity=9)
    await asyncio.wait_for(ledger.arrived.wait(), timeout=2)
    assert ledger.receipts == [9]
    async with asyncio.timeout(3):
        while (
            await inspector.jetstream().consumer_info(
                config.prefixed_name("SHIPMENTS"), config.prefixed_name("receipts")
            )
        ).num_ack_pending:
            await asyncio.sleep(0.01)
    assert errors["warehouse"].empty()


async def test_preprovisioned_shipments_run_and_rebind_without_resource_admin_grants(
    secured_broker,
):
    start, _, serve, errors = secured_broker
    config = bound_shipment_config()
    policy = broker_permissions(Shipments, config, role="service", response_ttl=None)
    url, inspector = await start({"warehouse": policy})
    await provision_bound_shipments(inspector.jetstream(), config)

    shipments = await serve(Shipments, config, url, "warehouse")
    await shipments.publish_event("shipments.packed", quantity=2)
    await asyncio.wait_for(shipments.arrived.wait(), timeout=2)
    shipments.arrived.clear()
    await shipments.publish_event("shipments.sent", quantity=4)
    await asyncio.wait_for(shipments.arrived.wait(), timeout=2)
    assert shipments.shipped == [2, 4]
    assert errors["warehouse"].empty()

    await shipments.stop()
    replacement = await serve(Shipments, config, url, "warehouse")
    await replacement.publish_event("shipments.packed", quantity=7)
    await asyncio.wait_for(replacement.arrived.wait(), timeout=2)
    assert replacement.shipped == [7]
    assert errors["warehouse"].empty()

    await publish_denied(replacement.nc, inspector, errors["warehouse"], "$JS.API.STREAM.LIST")
    await publish_denied(
        replacement.nc,
        inspector,
        errors["warehouse"],
        "$JS.API.STREAM.INFO.east_PAYMENTS",
    )
    await publish_denied(
        replacement.nc,
        inspector,
        errors["warehouse"],
        "$JS.API.CONSUMER.INFO.east_SHIPMENTS.east_other_team",
    )
    await publish_denied(replacement.nc, inspector, errors["warehouse"], "east.payments.captured")


async def test_bind_mode_names_a_missing_shipment_stream_before_subscribing(secured_broker):
    start, _, serve, _ = secured_broker
    config = bound_shipment_config()
    url, _ = await start(
        {"warehouse": broker_permissions(Shipments, config, role="service", response_ttl=None)}
    )
    with pytest.raises(StreamDeclarationError, match="east_SHIPMENTS.*Create it"):
        await serve(Shipments, config, url, "warehouse")


async def test_bind_mode_names_a_missing_shipment_consumer_before_binding(secured_broker):
    start, _, serve, _ = secured_broker
    config = bound_shipment_config()
    url, inspector = await start(
        {"warehouse": broker_permissions(Shipments, config, role="service", response_ttl=None)}
    )
    await provision_bound_shipments(inspector.jetstream(), config, consumers=False)
    with pytest.raises(ConsumerBindingError, match="east_packing.*Create it"):
        await serve(Shipments, config, url, "warehouse")


async def test_bind_mode_names_an_incompatible_shipment_consumer_before_binding(
    secured_broker,
):
    start, _, serve, _ = secured_broker
    config = bound_shipment_config()
    url, inspector = await start(
        {"warehouse": broker_permissions(Shipments, config, role="service", response_ttl=None)}
    )
    await provision_bound_shipments(
        inspector.jetstream(), config, packed_subject="east.retail.payments.captured"
    )
    with pytest.raises(ConsumerBindingError) as exc:
        await serve(Shipments, config, url, "warehouse")
    message = str(exc.value)
    assert "east_packing" in message
    assert "east.retail.payments.captured" in message
    assert "east.retail.shipments.packed" in message


async def test_bind_mode_refuses_a_named_ephemeral_shipment_consumer(secured_broker):
    start, _, serve, _ = secured_broker
    config = bound_shipment_config()
    url, inspector = await start(
        {"warehouse": broker_permissions(Shipments, config, role="service", response_ttl=None)}
    )
    await provision_bound_shipments(inspector.jetstream(), config, packing_is_durable=False)

    with pytest.raises(ConsumerBindingError, match="durable_name.*expected 'east_packing'"):
        await serve(Shipments, config, url, "warehouse")


class Feeds(CliffracerService):
    @rpc
    async def tail(self, n: int, pause: float = 0.01) -> AsyncIterator[int]:
        for value in range(n):
            yield value
            await asyncio.sleep(pause)


def feeds_config(cap: float = 30.0):
    return ServiceConfig(
        name="feeds",
        nats_inbox_prefix="_INBOX.feeds",
        max_rpc_processing_time=cap,
        health_port=0,
    )


async def read_stream_as(reader, config, n, pause=0.01):
    """Ask for `n` items from the reader's own inbox; return what arrives within a second of
    the last message, stopping at the envelope that ends the stream."""
    inbox = reader.new_inbox()
    sub = await reader.subscribe(inbox)
    await reader.flush()
    await reader.publish(
        HandlerDiscovery.outbound_subject(config, "feeds", "rpc", "tail"),
        json.dumps({"n": n, "pause": pause}).encode(),
        reply=inbox,
        headers={"Content-Type": "application/json", "Cliffracer-Stream": "1"},
    )
    got = []
    while True:
        try:
            message = await sub.next_msg(timeout=1.0)
        except nats.errors.TimeoutError:
            break
        got.append(message)
        if "Cliffracer-Stream-End" in (message.headers or {}):
            break
    await sub.unsubscribe()
    return got


@pytest.mark.parametrize("grant", ["derived", "one-reply"])
async def test_a_streaming_service_role_replies_with_every_item_and_one_reply_cuts_it(
    secured_broker, grant
):
    start, connect, serve, errors = secured_broker
    config = feeds_config()
    policy = broker_permissions(
        Feeds, config, role="service", response_ttl=30.0 + RESPONSE_GRANT_MARGIN
    )
    if grant == "one-reply":
        policy = replace(policy, response_max=1)
    reader_policy = broker_permissions(Feeds, config, role="client", inbox_prefix="_INBOX.reader")
    url, _ = await start({"feeder": policy, "reader": reader_policy})
    await serve(Feeds, config, url, "feeder")
    reader = await connect(url, "reader", inbox_prefix="_INBOX.reader")

    got = await read_stream_as(reader, config, 5)

    if grant == "derived":
        assert [json.loads(m.data) for m in got[:-1]] == [0, 1, 2, 3, 4]
        assert got[-1].headers["Cliffracer-Stream-End"] == "5"
        assert json.loads(got[-1].data)["success"] is True
    else:
        # The broker drops every reply after the first, with no word to either side.
        assert [json.loads(m.data) for m in got] == [0]


async def test_a_stream_cut_at_its_bound_delivers_its_end_under_a_grant_of_bound_and_margin(
    secured_broker,
):
    """The grant starts when the broker routes the request, before the service's deadline does;
    the margin is what lets the envelope published just after the deadline through."""
    start, connect, serve, errors = secured_broker
    cap = 1.0
    config = feeds_config(cap)
    policy = broker_permissions(
        Feeds, config, role="service", response_ttl=cap + RESPONSE_GRANT_MARGIN
    )
    reader_policy = broker_permissions(Feeds, config, role="client", inbox_prefix="_INBOX.reader")
    url, _ = await start({"feeder": policy, "reader": reader_policy})
    await serve(Feeds, config, url, "feeder")
    reader = await connect(url, "reader", inbox_prefix="_INBOX.reader")

    got = await read_stream_as(reader, config, 1000, pause=0.25)

    assert got, "nothing arrived"
    assert "Cliffracer-Stream-End" in (got[-1].headers or {}), (
        f"no End after {len(got)} items: the broker dropped the envelope"
    )
    assert json.loads(got[-1].data)["code"] == "deadline_exceeded"
    assert errors["feeder"].empty()


class Overruns(CliffracerService):
    """A unary method that runs far past any cap, so the service answers at its deadline."""

    @rpc
    async def slow(self) -> int:
        await asyncio.sleep(60)
        return 1


async def test_a_unary_reply_at_the_deadline_arrives_under_a_grant_of_bound_and_margin(
    secured_broker,
):
    """The grant starts when the broker routes the request, before the service's deadline does;
    the margin is what lets the deadline_exceeded reply published just after the deadline through.
    With no margin the broker refuses that reply and the caller times out."""
    start, connect, serve, errors = secured_broker
    cap = 1.0
    config = ServiceConfig(
        name="overruns", nats_inbox_prefix="_INBOX.overruns", max_rpc_processing_time=cap
    )
    policy = broker_permissions(
        Overruns, config, role="service", response_ttl=cap + RESPONSE_GRANT_MARGIN
    )
    caller_policy = broker_permissions(
        Overruns, config, role="client", inbox_prefix="_INBOX.caller"
    )
    url, _ = await start({"overruns": policy, "caller": caller_policy})
    await serve(Overruns, config, url, "overruns")
    caller = await connect(url, "caller", inbox_prefix="_INBOX.caller")

    try:
        reply = await caller.request(
            HandlerDiscovery.outbound_subject(config, "overruns", "rpc", "slow"),
            b"{}",
            timeout=cap + 2.0,
            headers={"Content-Type": "application/json"},
        )
    except nats.errors.TimeoutError:
        raise AssertionError(
            f"the caller timed out: the broker dropped the reply sent at the {cap}s deadline"
        ) from None

    assert json.loads(reply.data)["code"] == "deadline_exceeded"
    assert errors["overruns"].empty(), "the service's reply was refused by the broker"
