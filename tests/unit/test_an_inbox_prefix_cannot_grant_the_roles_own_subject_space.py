"""An inbox prefix whose grant covers the role's own subjects is refused.

The inbox grant is `<prefix>.>`, a subscribe permission over everything beneath the prefix.
`validate_inbox_prefix` refuses only the bare `_INBOX`, a leading `$`, wildcards and empty
tokens, so a prefix that is the environment prefix, the namespace or a subject family the role
uses was accepted, and the role's "inbox" grant became a blanket subscribe over its own
traffic: with `inbox_prefix='east'` and `subject_prefix='east'` the role could subscribe to
every subject in its environment.

The generator refuses such a prefix and names what it overlaps. Every prefix that is not a grant
over the role's own space stays accepted, including a dedicated one outside `_INBOX`.
`ServiceConfig` and `validate_inbox_prefix` are unchanged.
"""

import pytest

from cliffracer import ServiceConfig, rpc
from cliffracer.broker_permissions import broker_permissions

pytestmark = pytest.mark.unit


class Lookup:
    @rpc
    async def find(self, key: str) -> str:
        return key


def _config(inbox: str, **kwargs) -> ServiceConfig:
    return ServiceConfig(name="orders", nats_inbox_prefix=inbox, **kwargs)


def _service(inbox: str, **kwargs):
    return broker_permissions(Lookup, _config(inbox, **kwargs), role="service")


# --- the audit's four prefixes, and the forms beside them ----------------------------------


@pytest.mark.parametrize(
    ("inbox", "config", "overlap"),
    [
        pytest.param("east", {"subject_prefix": "east"}, "east.>", id="the-environment-prefix"),
        pytest.param(
            "east.retail",
            {"subject_prefix": "east", "namespace": "retail"},
            "east.>",
            id="the-prefix-and-namespace",
        ),
        pytest.param(
            "east.other",
            {"subject_prefix": "east", "namespace": "retail"},
            "east.>",
            id="another-namespace-in-the-environment",
        ),
        pytest.param("retail", {"namespace": "retail"}, "retail.>", id="the-namespace"),
        pytest.param(
            "retail.replies", {"namespace": "retail"}, "retail.>", id="beneath-the-namespace"
        ),
        pytest.param("orders.rpc", {}, "orders.rpc.*", id="the-service-rpc-subjects"),
        pytest.param("orders", {}, "orders.rpc.*", id="the-service-name"),
    ],
)
def test_a_service_prefix_that_covers_its_own_subject_space_is_refused_naming_the_overlap(
    inbox, config, overlap
):
    with pytest.raises(ValueError, match="overlap") as refused:
        _service(inbox, **config)

    assert overlap in str(refused.value), str(refused.value)
    assert repr(inbox) in str(refused.value), str(refused.value)


@pytest.mark.parametrize(
    ("inbox", "config"),
    [
        pytest.param("_INBOX.orders", {}, id="under-inbox"),
        pytest.param("_INBOX.east", {"subject_prefix": "east"}, id="inbox-named-for-the-prefix"),
        pytest.param("orders_replies", {}, id="a-dedicated-prefix-outside-inbox"),
        pytest.param("orders.replies", {}, id="a-dotted-dedicated-prefix"),
        pytest.param("west.replies", {"subject_prefix": "east"}, id="another-environment"),
        pytest.param(
            "replies", {"subject_prefix": "east", "namespace": "retail"}, id="outside-the-scope"
        ),
    ],
)
def test_CONTROL_a_prefix_that_is_not_a_grant_over_the_services_own_traffic_is_accepted(
    inbox, config
):
    policy = _service(inbox, **config)

    assert f"{inbox}.>" in policy.subscribe


# --- the client role ------------------------------------------------------------------------------


def test_a_client_prefix_that_covers_the_subjects_it_calls_is_refused():
    config = ServiceConfig(name="orders")

    with pytest.raises(ValueError, match="overlap") as refused:
        broker_permissions(Lookup, config, role="client", inbox_prefix="orders.rpc")

    assert "orders.rpc.find" in str(refused.value), str(refused.value)


def test_a_client_prefix_inside_the_environment_is_refused():
    config = ServiceConfig(name="orders", subject_prefix="east")

    with pytest.raises(ValueError, match="overlap") as refused:
        broker_permissions(Lookup, config, role="client", inbox_prefix="east.replies")

    assert "east.>" in str(refused.value), str(refused.value)


def test_CONTROL_a_client_prefix_outside_the_subjects_it_calls_is_accepted():
    config = ServiceConfig(name="orders", subject_prefix="east")

    policy = broker_permissions(Lookup, config, role="client", inbox_prefix="_INBOX.customer")

    assert policy.subscribe == ("_INBOX.customer.>",)


# --- the subjects a caller adds -------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["extra_publish", "extra_subscribe"])
def test_a_prefix_that_covers_a_subject_the_caller_adds_is_refused_naming_it(direction):
    with pytest.raises(ValueError, match="overlap") as refused:
        broker_permissions(
            Lookup,
            _config("audit"),
            role="service",
            **{direction: ["audit.events"]},
        )

    assert "audit.events" in str(refused.value), str(refused.value)


def test_CONTROL_added_subjects_beside_the_prefix_are_accepted():
    policy = broker_permissions(
        Lookup,
        _config("_INBOX.orders"),
        role="service",
        extra_publish=["audit.events"],
        extra_subscribe=["audit.replies"],
    )

    assert {"audit.events"} <= set(policy.publish)
    assert {"audit.replies", "_INBOX.orders.>"} <= set(policy.subscribe)


# --- what is unchanged ------------------------------------------------------------------------------


def test_the_config_and_the_prefix_validator_still_accept_the_prefixes_the_generator_refuses():
    config = _config("east", subject_prefix="east")

    assert config.nats_inbox_prefix == "east"
