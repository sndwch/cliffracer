"""What `broker_permissions` refuses, and the grants it derives at its edges.

Each row pins a behaviour no test failed without, found by a survivor probe of
`cliffracer/broker_permissions.py`: the names it will turn into a subject, the arguments it accepts,
the JetStream grants a service gets, the response grant's default and the bound on its duration.
"""

import math
from dataclasses import replace

import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener, rpc
from cliffracer.broker_permissions import BrokerPermissions, broker_permissions
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

INT64_MAX_NS = 2**63 - 1


class Orders(CliffracerService):
    @rpc
    async def place(self, n: int) -> int:
        return n

    @listener("orders.audit", fanout=True)
    async def audit(self, subject: str) -> None:
        pass


def config(**fields) -> ServiceConfig:
    return ServiceConfig(name="orders", nats_inbox_prefix="_INBOX.orders", health_port=0, **fields)


def description_with_a_method_named(name: str):
    described = describe(Orders, config=config())
    return replace(described, methods=[replace(described.methods[0], name=name)])


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("*", id="a-whole-token-wildcard"),
        pytest.param(">", id="a-whole-token-tail-wildcard"),
        pytest.param("place*", id="a-wildcard"),
        pytest.param("pl>ace", id="a-tail-wildcard"),
        pytest.param("pla ce", id="white-space"),
        pytest.param("orders.place", id="a-dot"),
        pytest.param("pla/ce", id="a-slash"),
    ],
)
def test_a_client_is_refused_a_method_name_that_is_not_one_subject_token(name):
    """A `Description` passed in is not built by `describe`, so its method names are read as they
    are; one that is not a single token would become a wider or a different outbound subject. A
    name that is a whole wildcard, `*` or `>`, is a valid subject token, so only the resource-name
    check refuses it: let through, it would grant `orders.rpc.*`, every method."""
    with pytest.raises(ValueError):
        broker_permissions(
            description_with_a_method_named(name),
            config(),
            role="client",
            inbox_prefix="_INBOX.customer",
        )


def test_CONTROL_a_client_is_granted_a_method_name_that_is_one_token():
    granted = broker_permissions(
        description_with_a_method_named("place_order"),
        config(),
        role="client",
        inbox_prefix="_INBOX.customer",
    )
    assert "orders.rpc.place_order" in granted.publish


def test_a_contract_that_is_neither_a_service_class_nor_a_description_is_refused():
    with pytest.raises(TypeError, match="expected a service class or Description"):
        broker_permissions({"service": "orders"}, config(), role="service")  # type: ignore[arg-type]


def test_a_description_of_another_service_is_refused():
    other = replace(describe(Orders, config=config()), service="shipments")

    with pytest.raises(ValueError, match="description.service must match config.name"):
        broker_permissions(other, config(), role="service")


def test_a_role_that_is_neither_service_nor_client_is_refused():
    with pytest.raises(ValueError, match="role must be 'service' or 'client'"):
        broker_permissions(Orders, config(), role="admin")  # type: ignore[arg-type]


def test_a_service_with_jetstream_off_gets_no_jetstream_grant_though_it_declares_streams():
    granted = broker_permissions(
        Orders,
        config(
            jetstream_enabled=False,
            jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.created"])],
        ),
        role="service",
    )

    assert [subject for subject in granted.publish if subject.startswith("$JS")] == []


def test_a_fanout_listener_on_a_jetstream_service_is_granted_its_subject_and_needs_no_stream():
    """A fanout listener is a core NATS subscription, not a JetStream consumer, even on a service
    with JetStream enabled: its messages arrive on its own subject. So it is granted a plain
    subscribe on that subject, and it needs no declared stream; refusing it as "needs exactly one
    declared stream" would refuse a listener JetStream never delivers to."""
    granted = broker_permissions(
        Orders,
        config(
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.created"])],
        ),
        role="service",
    )

    assert "orders.audit" in granted.subscribe
    assert not any(subject.startswith("$JS.API.CONSUMER") for subject in granted.publish)


def test_a_response_grant_allows_one_reply_unless_told_otherwise():
    rendered = BrokerPermissions(response_ttl=5.0).to_nats_permissions()

    assert rendered["allow_responses"]["max"] == 1


def test_a_response_grant_at_the_largest_whole_second_int64_nanoseconds_can_hold_is_accepted():
    """NATS reads the grant's duration as int64 nanoseconds. 9_223_372_036 seconds is the largest
    whole number of seconds whose nanoseconds fit, so it is accepted and rendered as written."""
    seconds = 9_223_372_036
    assert seconds * 1_000_000_000 <= INT64_MAX_NS

    rendered = BrokerPermissions(response_ttl=seconds).to_nats_permissions()

    assert rendered["allow_responses"]["expires"] == f"{seconds * 1_000_000_000}ns"


def test_a_response_grant_whose_nanoseconds_overflow_int64_is_refused():
    """9_223_372_036.9 seconds is 9_223_372_036_900_000_000 nanoseconds, above int64's maximum of
    9_223_372_036_854_775_807, so NATS could not read it: refused, not rendered."""
    seconds = 9_223_372_036.9
    assert math.ceil(seconds * 1_000_000_000) > INT64_MAX_NS

    with pytest.raises(ValueError, match="within NATS's duration range"):
        BrokerPermissions(response_ttl=seconds)
