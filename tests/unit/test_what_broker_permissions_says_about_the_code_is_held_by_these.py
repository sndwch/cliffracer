"""Claims `docs/broker-permissions.md` makes about the code that no other test holds.

The claims the document makes that other tests already hold are named in the pull request that
added this file, each beside the test that would go red. These pin the rest:

- The grants are sorted, immutable tuples, and the planner connects to no broker.
- The response grant renders its expiry, and is absent when `response_ttl` is `None`.
- A JetStream service publishes its declared stream subjects, in provision and in bind mode.
- Acknowledgment grants cover the ordinary and the domain-aware address forms, in both modes.
- Explicit outbound and inbound subjects are applied as written, and the role is granted no
  inbox but its own.
- A service connection and a generated client leave the inbox prefix alone when none is set.
- Bind mode compares every `StreamSpec` field and every consumer field the document lists.
"""

import socket
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from nats.js.api import AckPolicy, ConsumerConfig

from cliffracer import ServiceConfig, StreamSpec
from cliffracer.broker_permissions import BrokerPermissions, broker_permissions
from cliffracer.client import ServiceClient
from cliffracer.core.jetstream import (
    TUNED_CONSUMER_FIELDS,
    ConsumerBindingError,
    StreamDeclarationError,
    validate_bound_consumer,
    validate_bound_streams,
)
from tests.fixtures.permission_orders import Orders, Shipments

pytestmark = pytest.mark.unit


def order_config(**kwargs) -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.order_workers",
        health_port=0,
        **kwargs,
    )


def shipment_config(mode: str = "provision", **kwargs) -> ServiceConfig:
    return order_config(
        jetstream_enabled=True,
        jetstream_resource_mode=mode,
        jetstream_streams=[
            StreamSpec(name="SHIPMENTS", subjects=["retail.shipments.*"]),
            StreamSpec(name="FAILURES", subjects=["dlq.orders"]),
        ],
        **kwargs,
    )


# -- the grants are sorted, immutable tuples ---------------------------------------------------


def test_publish_and_subscribe_are_sorted_immutable_tuples():
    unsorted = BrokerPermissions(("b.two", "a.one"), ("d.four", "c.three"))
    assert unsorted.publish == ("a.one", "b.two")
    assert unsorted.subscribe == ("c.three", "d.four")

    derived = broker_permissions(Shipments, shipment_config(), role="service")
    for direction in (derived.publish, derived.subscribe):
        assert isinstance(direction, tuple)
        assert list(direction) == sorted(direction)

    with pytest.raises(FrozenInstanceError):
        derived.publish = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        derived.publish.append("a.one")  # type: ignore[attr-defined]


# -- the planner connects to no broker -----------------------------------------------------------


@pytest.mark.parametrize("role", ["service", "client"])
def test_planning_grants_dials_nothing(role):
    refuse = AssertionError("broker_permissions opened a connection")
    with (
        patch.object(socket.socket, "connect", side_effect=refuse),
        patch("socket.create_connection", side_effect=refuse),
        patch("cliffracer.core.dial.connect", AsyncMock(side_effect=refuse)),
        patch("cliffracer.core.dial.connect", AsyncMock(side_effect=refuse)),
        patch("nats.aio.client.Client.connect", AsyncMock(side_effect=refuse)),
    ):
        policy = broker_permissions(
            Shipments,
            shipment_config(),
            role=role,
            inbox_prefix="_INBOX.customers" if role == "client" else None,
        )
    assert policy.subscribe


# -- the response grant ----------------------------------------------------------------------------


def test_the_response_grant_carries_the_configured_expiry_and_none_leaves_it_out():
    config = order_config()
    rendered = broker_permissions(
        Orders, config, role="service", response_ttl=5
    ).to_nats_permissions()
    assert rendered["allow_responses"] == {"max": 1, "expires": "5000000000ns"}

    without = broker_permissions(Orders, config, role="service", response_ttl=None)
    assert "allow_responses" not in without.to_nats_permissions()
    assert without.response_ttl is None


# -- JetStream grants ----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["provision", "bind"])
def test_a_jetstream_service_publishes_its_declared_stream_subjects(mode):
    policy = broker_permissions(Shipments, shipment_config(mode), role="service")
    assert {"east.retail.shipments.*", "east.dlq.orders"} <= set(policy.publish)

    plain = broker_permissions(Orders, order_config(), role="service")
    assert plain.publish == ("east.dlq.orders",)


@pytest.mark.parametrize("mode", ["provision", "bind"])
@pytest.mark.parametrize("durable", ["packing", "dispatching"])
def test_acknowledgment_grants_cover_the_ordinary_and_the_domain_aware_forms(mode, durable):
    policy = broker_permissions(Shipments, shipment_config(mode), role="service")
    assert f"$JS.ACK.east_SHIPMENTS.east_{durable}.>" in policy.publish
    assert f"$JS.ACK.*.*.east_SHIPMENTS.east_{durable}.>" in policy.publish


def test_the_provisioning_role_may_inspect_each_durable_it_names():
    policy = broker_permissions(Shipments, shipment_config("provision"), role="service")
    assert "$JS.API.CONSUMER.INFO.east_SHIPMENTS.east_packing" in policy.publish
    assert "$JS.API.CONSUMER.INFO.east_SHIPMENTS.east_dispatching" in policy.publish


def test_a_bound_role_may_use_what_it_reads_and_nothing_that_creates():
    policy = broker_permissions(Shipments, shipment_config("bind"), role="service")
    assert "$JS.API.STREAM.INFO.east_SHIPMENTS" in policy.publish
    assert "$JS.API.STREAM.INFO.east_FAILURES" in policy.publish
    assert "$JS.API.CONSUMER.MSG.NEXT.east_SHIPMENTS.east_dispatching" in policy.publish
    assert not [s for s in policy.publish if ".CREATE." in s or ".UPDATE." in s]


# -- explicit subjects and the role's inbox ---------------------------------------------------------


def test_explicit_subjects_are_applied_as_written_without_a_prefix():
    policy = broker_permissions(
        Orders,
        order_config(),
        role="service",
        extra_publish=["billing.invoices.*"],
        extra_subscribe=["billing.refunds.>"],
    )
    assert "billing.invoices.*" in policy.publish
    assert "billing.refunds.>" in policy.subscribe
    assert not [s for s in (*policy.publish, *policy.subscribe) if s.startswith("east.billing")]


@pytest.mark.parametrize("role", ["service", "client"])
def test_the_role_is_granted_no_inbox_but_its_own(role):
    policy = broker_permissions(
        Shipments,
        shipment_config(),
        role=role,
        inbox_prefix="_INBOX.customers" if role == "client" else None,
    )
    own = "_INBOX.customers.>" if role == "client" else "_INBOX.order_workers.>"
    assert [s for s in (*policy.publish, *policy.subscribe) if s.startswith("_INBOX")] == [own]


# -- unset leaves nats-py's default inbox in force -----------------------------------------------


async def test_a_service_connection_sends_no_inbox_prefix_when_none_is_set():
    set_config = order_config()
    unset = ServiceConfig(name="orders", health_port=0)
    sent = {}
    for label, config in (("set", set_config), ("unset", unset)):
        service = Orders(config)
        with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
            await service.container.connection.connect()
        sent[label] = connect.call_args.kwargs
    assert sent["set"]["inbox_prefix"] == "_INBOX.order_workers"
    assert "inbox_prefix" not in sent["unset"]


async def test_a_generated_client_sends_no_inbox_prefix_when_none_is_set():
    kwargs = {}
    for label, options in (("set", {"inbox_prefix": "_INBOX.customer"}), ("unset", {})):
        client = ServiceClient(service="orders", verify=False, **options)
        with patch("cliffracer.core.dial.connect", AsyncMock(return_value=AsyncMock())) as connect:
            await client.__aenter__()
        kwargs[label] = connect.call_args.kwargs
        await client.close()
    assert kwargs["set"]["inbox_prefix"] == "_INBOX.customer"
    assert "inbox_prefix" not in kwargs["unset"]


# -- bind mode compares what the document says it compares --------------------------------------

DECLARED = StreamSpec(
    name="EAST_SHIPMENTS",
    subjects=["east.shipments.*"],
    storage="file",
    retention="limits",
    max_age_seconds=60,
    duplicate_window_seconds=30,
)
DIFFERENT = {
    "name": "EAST_OTHER",
    "subjects": ["east.other.*"],
    "storage": "memory",
    "retention": "interest",
    "max_age_seconds": 61,
    "duplicate_window_seconds": 31,
}


#: Fields compared one way only, each with a case of its own below rather than in `DIFFERENT`.
ONE_WAY = {"allow_msg_schedules"}


def test_every_stream_field_has_a_case_below():
    assert set(DIFFERENT) | ONE_WAY == set(StreamSpec.model_fields)
    assert not set(DIFFERENT) & ONE_WAY


async def test_bind_mode_refuses_a_stream_without_the_schedules_its_declaration_asks_for():
    declared = StreamSpec(
        name="EAST_SHIPMENTS",
        subjects=["east.shipments.*", "_sched.east.shipments.*.*"],
        allow_msg_schedules=True,
    )
    js = AsyncMock()
    js.stream_info.return_value = SimpleNamespace(
        config=declared.to_stream_config().evolve(allow_msg_schedules=None)
    )
    with pytest.raises(StreamDeclarationError) as exc:
        await validate_bound_streams(js, [declared])
    assert "allow_msg_schedules is" in str(exc.value)


async def test_CONTROL_bind_mode_accepts_a_stream_that_allows_schedules_its_declaration_does_not_ask_for():
    js = AsyncMock()
    js.stream_info.return_value = SimpleNamespace(
        config=DECLARED.to_stream_config().evolve(allow_msg_schedules=True)
    )
    await validate_bound_streams(js, [DECLARED])


@pytest.mark.parametrize("field", sorted(DIFFERENT))
async def test_bind_mode_refuses_a_stream_that_differs_in_any_declared_field(field):
    live = DECLARED.model_copy(update={field: DIFFERENT[field]})
    js = AsyncMock()
    js.stream_info.return_value = SimpleNamespace(config=live.to_stream_config())
    with pytest.raises(StreamDeclarationError) as exc:
        await validate_bound_streams(js, [DECLARED])
    assert f"{field} is" in str(exc.value)


def shipment_consumer(*, pull: bool, **changes) -> ConsumerConfig:
    fields = {
        "name": "packing",
        "durable_name": "packing",
        "filter_subject": "shipments.packed",
        "ack_policy": AckPolicy.EXPLICIT,
        "ack_wait": 12,
        "max_deliver": 4,
        "max_ack_pending": 8,
        "deliver_subject": None if pull else "_INBOX.shippers.delivery",
        "deliver_group": None if pull else "packing",
    }
    return ConsumerConfig(**{**fields, **changes})


def bind_config() -> ServiceConfig:
    return ServiceConfig(
        name="shipments",
        jetstream_enabled=True,
        jetstream_resource_mode="bind",
        jetstream_ack_wait=12,
        jetstream_max_deliver=4,
        jetstream_max_ack_pending=8,
    )


async def bind(consumer: ConsumerConfig, *, pull: bool):
    js = AsyncMock()
    js.consumer_info.return_value = SimpleNamespace(config=consumer)
    return await validate_bound_consumer(
        js,
        stream="SHIPMENTS",
        durable="packing",
        subject="shipments.packed",
        pull=pull,
        config=bind_config(),
    )


TUNING_DRIFT = {
    "ack_policy": AckPolicy.ALL,
    "ack_wait": 99,
    "max_deliver": 99,
    "max_ack_pending": 99,
}


def test_every_tuned_consumer_field_has_a_case_below():
    assert set(TUNING_DRIFT) == set(TUNED_CONSUMER_FIELDS)


@pytest.mark.parametrize("field", sorted(TUNING_DRIFT))
async def test_bind_mode_refuses_a_consumer_whose_tuning_differs(field):
    drifted = shipment_consumer(pull=False, **{field: TUNING_DRIFT[field]})
    with pytest.raises(ConsumerBindingError, match=f"{field} is"):
        await bind(drifted, pull=False)


async def test_bind_mode_refuses_a_push_consumer_with_another_delivery_group():
    other = shipment_consumer(pull=False, deliver_group="someone_else")
    with pytest.raises(ConsumerBindingError, match="deliver_group is 'someone_else'"):
        await bind(other, pull=False)


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"deliver_subject": "_INBOX.shippers.delivery"}, "deliver_subject is"),
        ({"deliver_group": "packing"}, "deliver_group is"),
    ],
    ids=["delivery-subject", "delivery-group"],
)
async def test_bind_mode_refuses_a_pull_listener_whose_consumer_delivers_by_push(changes, named):
    with pytest.raises(ConsumerBindingError, match=f"{named}.*expected none for a pull consumer"):
        await bind(shipment_consumer(pull=True, **changes), pull=True)


async def test_CONTROL_bind_mode_accepts_the_consumers_the_cases_above_perturb():
    await bind(shipment_consumer(pull=False), pull=False)
    await bind(shipment_consumer(pull=True), pull=True)
