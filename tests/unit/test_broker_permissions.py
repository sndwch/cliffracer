"""Declared order and shipment roles receive only their required subjects."""

from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, rpc
from cliffracer.broker_permissions import (
    RESPONSE_GRANT_MARGIN,
    BrokerPermissions,
    broker_permissions,
)
from cliffracer.client import ServiceClient
from cliffracer.introspect import Description, describe
from cliffracer.testing import assert_rpc_permissions
from tests.fixtures.permission_orders import Orders, Shipments

pytestmark = pytest.mark.unit


def configuration(**kwargs):
    return ServiceConfig(
        name="orders",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.order_workers",
        **kwargs,
    )


def test_order_role_uses_runtime_subscription_shapes_and_preserves_environment_boundaries():
    policy = broker_permissions(Orders, configuration(), role="service")
    assert set(policy.subscribe) == {
        "_INBOX.order_workers.>",
        "east.retail.orders.rpc.*",
        "east.retail.orders.async.*",
        "east.retail.orders.describe",
        "east.*.orders.created",
        "east.*.orders.returned",
    }
    assert policy.publish == ("east.dlq.orders",)
    assert policy.to_nats_permissions()["allow_responses"] == {"max": 1, "expires": "30000000000ns"}


def test_customer_role_names_methods_and_requires_explicit_async_access():
    config = configuration()
    policy = broker_permissions(Orders, config, role="client", inbox_prefix="_INBOX.customer")
    assert policy.publish == ("east.retail.orders.describe", "east.retail.orders.rpc.reserve")
    assert policy.subscribe == ("_INBOX.customer.>",)
    assert "allow_responses" not in policy.to_nats_permissions()
    async_policy = broker_permissions(
        Orders, config, role="client", inbox_prefix="_INBOX.customer", allow_async=True
    )
    assert set(async_policy.publish) - set(policy.publish) == {"east.retail.orders.async.reserve"}
    assert_rpc_permissions(Orders, config, async_policy, allow_async=True)


def test_serialized_listener_contract_keeps_cross_namespace_routing_and_excludes_private_handlers():
    config = configuration()
    original = describe(Orders, config=config)
    restored = Description.from_dict(original.to_dict())
    assert [listener.handler_name for listener in restored.listeners] == ["created", "returned"]
    assert all(listener.cross_namespace for listener in restored.listeners)
    assert broker_permissions(restored, config, role="service") == broker_permissions(
        Orders, config, role="service"
    )
    assert restored.methods == original.methods
    legacy = original.to_dict()
    for listener in legacy["listeners"]:
        listener.pop("cross_namespace")
    assert not any(listener.cross_namespace for listener in Description.from_dict(legacy).listeners)


def test_planning_an_order_role_never_constructs_or_starts_the_service():
    class UnstartedOrders(Orders):
        def __init__(self):
            raise AssertionError("configuration planning constructed an order service")

    assert (
        "east.retail.orders.rpc.reserve"
        in broker_permissions(
            UnstartedOrders, configuration(), role="client", inbox_prefix="_INBOX.customer"
        ).publish
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "_INBOX",
        "_INBOX.>",
        "_INBOX.*",
        "_INBOX..orders",
        "_INBOX.orders ",
        "_INBOX.orders\x00",
        "$JS.API",
    ],
)
def test_an_inbox_prefix_cannot_expand_into_other_roles(prefix):
    with pytest.raises(ValueError):
        broker_permissions(Orders, configuration(), role="client", inbox_prefix=prefix)
    with pytest.raises(ValueError):
        ServiceConfig.model_validate({**configuration().model_dump(), "nats_inbox_prefix": prefix})


def test_service_policy_requires_the_same_inbox_prefix_as_its_connection():
    with pytest.raises(ValueError, match="config.nats_inbox_prefix"):
        broker_permissions(
            Orders, configuration(), role="service", inbox_prefix="_INBOX.someone_else"
        )
    with pytest.raises(ValueError, match="required"):
        broker_permissions(Orders, ServiceConfig(name="orders"), role="client")
    with pytest.raises(ValueError, match="required"):
        broker_permissions(Orders, configuration(), role="client")


@pytest.mark.parametrize(
    "pattern",
    ["", "orders..created", "orders.>.created", "orders.cre*", "orders.\x00", "orders created"],
)
def test_invalid_explicit_subject_patterns_are_refused(pattern):
    with pytest.raises(ValueError):
        broker_permissions(Orders, configuration(), role="service", extra_publish=[pattern])


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), 1e308, True])
def test_response_grants_have_a_finite_positive_budget(ttl):
    with pytest.raises(ValueError, match="finite"):
        broker_permissions(Orders, configuration(), role="service", response_ttl=ttl)


def test_empty_grants_deny_instead_of_rendering_unrestricted_empty_lists():
    assert BrokerPermissions().to_nats_permissions() == {
        "publish": {"deny": [">"]},
        "subscribe": {"deny": [">"]},
    }


def test_explicit_outbound_grants_are_stable_wire_subjects_and_copied():
    grants = ["east.retail.shipments.*", "east.retail.shipments.*"]
    policy = broker_permissions(Orders, configuration(), role="service", extra_publish=grants)
    grants.append("west.>")
    assert policy.publish == ("east.dlq.orders", "east.retail.shipments.*")
    rendered = policy.to_nats_permissions()
    rendered["publish"]["allow"].clear()
    assert policy.publish == ("east.dlq.orders", "east.retail.shipments.*")
    with pytest.raises(ValueError, match="collection"):
        broker_permissions(Orders, configuration(), role="service", extra_publish="orders.created")


def test_rpc_coverage_names_the_missing_new_order_method():
    class CancellableOrders(Orders):
        @rpc
        async def cancel(self, order_id: str) -> bool:
            return True

    config = configuration()
    policy = broker_permissions(Orders, config, role="client", inbox_prefix="_INBOX.customer")
    with pytest.raises(AssertionError, match="rpc cancel: east.retail.orders.rpc.cancel"):
        assert_rpc_permissions(CancellableOrders, config, policy)
    service_policy = broker_permissions(CancellableOrders, config, role="service")
    assert_rpc_permissions(
        CancellableOrders, config, service_policy, role="service", allow_async=True
    )
    with pytest.raises(AssertionError, match="describe"):
        assert_rpc_permissions(
            Orders, config, replace(policy, publish=("east.retail.orders.rpc.reserve",))
        )


def test_jetstream_grants_name_declared_resources_and_separate_creation_from_updates():
    config = configuration(
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="SHIPMENTS", subjects=["retail.shipments.*"]),
            StreamSpec(name="FAILURES", subjects=["dlq.orders"]),
        ],
    )
    policy = broker_permissions(Shipments, config, role="service")
    assert "$JS.API.STREAM.LIST" in policy.publish
    assert "$JS.API.STREAM.NAMES" in policy.publish
    assert "$JS.API.STREAM.CREATE.east_SHIPMENTS" in policy.publish
    assert "$JS.API.CONSUMER.DURABLE.CREATE.east_SHIPMENTS.east_packing" in policy.publish
    assert (
        "$JS.API.CONSUMER.CREATE.east_SHIPMENTS.east_dispatching.east.retail.shipments.sent"
        in policy.publish
    )
    assert "$JS.API.CONSUMER.MSG.NEXT.east_SHIPMENTS.east_dispatching" in policy.publish
    assert "$JS.ACK.east_SHIPMENTS.east_packing.>" in policy.publish
    assert "$JS.API.>" not in policy.publish
    assert not any(".DELETE." in subject or ".UPDATE." in subject for subject in policy.publish)
    assert "east.retail.shipments.packed" not in policy.subscribe
    config.jetstream_update_streams = True
    assert set(broker_permissions(Shipments, config, role="service").publish) - set(
        policy.publish
    ) == {
        "$JS.API.STREAM.UPDATE.east_SHIPMENTS",
        "$JS.API.STREAM.UPDATE.east_FAILURES",
    }


def test_jetstream_listener_must_map_to_one_declared_stream():
    config = configuration(jetstream_enabled=True)
    with pytest.raises(ValueError, match="exactly one declared stream"):
        broker_permissions(Shipments, config, role="service")
    config.jetstream_streams = [
        StreamSpec(name=name, subjects=["retail.shipments.*"]) for name in ["NORTH", "SOUTH"]
    ]
    with pytest.raises(ValueError, match="exactly one declared stream"):
        broker_permissions(Shipments, config, role="service")


def test_a_bound_shipment_role_can_only_inspect_and_use_its_preprovisioned_resources():
    config = configuration(
        jetstream_enabled=True,
        jetstream_resource_mode="bind",
        jetstream_streams=[
            StreamSpec(name="SHIPMENTS", subjects=["retail.shipments.*"]),
            StreamSpec(name="FAILURES", subjects=["dlq.orders"]),
        ],
    )
    policy = broker_permissions(Shipments, config, role="service")
    assert "$JS.API.STREAM.INFO.east_SHIPMENTS" in policy.publish
    assert "$JS.API.CONSUMER.INFO.east_SHIPMENTS.east_packing" in policy.publish
    assert "$JS.API.CONSUMER.INFO.east_SHIPMENTS.east_dispatching" in policy.publish
    assert "$JS.API.CONSUMER.MSG.NEXT.east_SHIPMENTS.east_dispatching" in policy.publish
    assert "$JS.API.STREAM.LIST" not in policy.publish
    assert "$JS.API.STREAM.NAMES" not in policy.publish
    assert not any(".CREATE." in subject or ".UPDATE." in subject for subject in policy.publish)


async def test_generated_client_configures_its_inbox_at_connection_creation():
    nc = AsyncMock()
    with patch("cliffracer.core.dial.connect", return_value=nc) as connect:
        client = ServiceClient(service="orders", inbox_prefix="_INBOX.customer", verify=False)
        await client.__aenter__()
        assert connect.call_args.kwargs["inbox_prefix"] == "_INBOX.customer"
        await client.close()
    with pytest.raises(ValueError, match="borrowed"):
        ServiceClient(nc=nc, inbox_prefix="_INBOX.customer")


class Feeds(CliffracerService):
    @rpc
    async def tail(self, n: int) -> AsyncIterator[int]:
        for value in range(n):
            yield value

    @rpc
    async def latest(self) -> int:
        return 0


def feeds(**kwargs):
    return ServiceConfig(name="feeds", nats_inbox_prefix="_INBOX.feeds", **kwargs)


def test_a_service_that_streams_may_reply_without_limit_for_its_response_ttl():
    ttl = 60.0 + RESPONSE_GRANT_MARGIN
    policy = broker_permissions(
        Feeds, feeds(max_rpc_processing_time=60.0), role="service", response_ttl=ttl
    )

    assert policy.to_nats_permissions()["allow_responses"] == {
        "max": -1,
        "expires": f"{round(ttl * 1_000_000_000)}ns",
    }


def test_a_streaming_service_with_no_processing_bound_is_refused_by_name():
    with pytest.raises(
        ValueError, match=r"feeds streams the reply of tail: set max_rpc_processing"
    ):
        broker_permissions(Feeds, feeds(), role="service")


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(30.0, id="the-default-ttl-under-the-bound"),
        pytest.param(60.0, id="exactly-the-bound"),
        pytest.param(60.0 + RESPONSE_GRANT_MARGIN - 0.001, id="just-under-the-bound-and-margin"),
    ],
)
def test_a_streaming_service_whose_response_ttl_does_not_cover_bound_and_margin_is_refused(ttl):
    with pytest.raises(
        ValueError,
        match=rf"response_ttl={ttl}s does not cover .*=60.0s plus the {RESPONSE_GRANT_MARGIN}s .* tail",
    ):
        broker_permissions(
            Feeds, feeds(max_rpc_processing_time=60.0), role="service", response_ttl=ttl
        )


def test_a_streaming_service_with_no_response_grant_is_left_without_one():
    policy = broker_permissions(Feeds, feeds(), role="service", response_ttl=None)

    assert "allow_responses" not in policy.to_nats_permissions()


def test_a_client_of_a_streaming_service_gets_no_response_grant():
    policy = broker_permissions(Feeds, feeds(), role="client", inbox_prefix="_INBOX.readers")

    assert "allow_responses" not in policy.to_nats_permissions()


@pytest.mark.parametrize("count", [0, -2, True])
def test_a_response_count_nats_would_read_otherwise_is_refused(count):
    with pytest.raises(ValueError, match="response_max must be -1"):
        BrokerPermissions(response_ttl=1.0, response_max=count)


class Unary(CliffracerService):
    @rpc
    async def latest(self) -> int:
        return 0


def unary(**kwargs):
    return ServiceConfig(name="unary", nats_inbox_prefix="_INBOX.unary", **kwargs)


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(30.0, id="the-default-ttl-under-the-bound"),
        pytest.param(60.0, id="exactly-the-bound"),
        pytest.param(60.0 + RESPONSE_GRANT_MARGIN - 0.001, id="just-under-the-bound-and-margin"),
    ],
)
def test_a_unary_service_whose_response_ttl_does_not_cover_bound_and_margin_is_refused(ttl):
    with pytest.raises(
        ValueError,
        match=(
            rf"response_ttl={ttl}s does not cover max_rpc_processing_time=60.0s plus the "
            rf"{RESPONSE_GRANT_MARGIN}s margin .* deadline_exceeded reply without a word, .* "
            rf"at least {60.0 + RESPONSE_GRANT_MARGIN}$"
        ),
    ):
        broker_permissions(
            Unary, unary(max_rpc_processing_time=60.0), role="service", response_ttl=ttl
        )


def test_CONTROL_a_unary_grant_at_the_bound_and_margin_or_without_a_bound_is_derived():
    at_the_margin = broker_permissions(
        Unary,
        unary(max_rpc_processing_time=60.0),
        role="service",
        response_ttl=60.0 + RESPONSE_GRANT_MARGIN,
    )
    unbounded = broker_permissions(Unary, unary(), role="service", response_ttl=5.0)
    no_grant = broker_permissions(
        Unary, unary(max_rpc_processing_time=60.0), role="service", response_ttl=None
    )

    assert at_the_margin.to_nats_permissions()["allow_responses"] == {
        "max": 1,
        "expires": f"{round((60.0 + RESPONSE_GRANT_MARGIN) * 1_000_000_000)}ns",
    }
    assert unbounded.to_nats_permissions()["allow_responses"]["expires"] == "5000000000ns"
    assert "allow_responses" not in no_grant.to_nats_permissions()
