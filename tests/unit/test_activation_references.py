"""Pinned shipment addresses survive defaults while incompatible clients are refused."""

from dataclasses import replace

import pytest

from cliffracer.client import ServiceClient
from cliffracer.introspect import describe
from cliffracer.runners.contracts import (
    ActivationAddress,
    ActivationReference,
    ContractMismatch,
    LogicalIdentity,
    RpcContract,
    TemplateError,
)
from tests.fixtures.shipment_templates import Shipments, shipment_client_class

pytestmark = pytest.mark.unit


def reference(address):
    return ActivationReference(
        LogicalIdentity("retail", "shipments", "batch-a"),
        "host",
        1,
        "warehouse-a",
        RpcContract.from_description(describe(Shipments)),
        address,
    )


@pytest.mark.parametrize("prefix", [None, "freight"])
def test_binding_pins_routing_and_preserves_connection_options(monkeypatch, prefix):
    client_class = shipment_client_class()
    ref = reference(ActivationAddress("shipment_a", subject_prefix=prefix))
    client = ref.bind(client_class, timeout=7, headers={"authorization": "test-value"})
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "unrelated")
    expected = "freight.shipment_a.rpc.ship" if prefix else "shipment_a.rpc.ship"
    assert client._subject("rpc.ship") == expected
    assert client.timeout == 7
    assert client.headers == {"authorization": "test-value"}
    successor = replace(ref, generation=2, address=ActivationAddress("shipment_b", "south", prefix))
    next_client = successor.bind(client_class)
    assert next_client._subject("rpc.ship") != client._subject("rpc.ship")
    assert client._subject("rpc.ship") == expected


@pytest.mark.parametrize("field", ["service", "namespace", "subject_prefix"])
def test_binding_refuses_routing_overrides(field):
    ref = reference(ActivationAddress("shipment_a"))
    with pytest.raises(TemplateError, match="routing comes from its reference"):
        ref.bind(shipment_client_class(), **{field: "other"})


@pytest.mark.parametrize("change", ["missing", "extra", "changed"])
def test_client_binding_requires_the_complete_template_contract(change):
    client_class = shipment_client_class()
    if change == "missing":
        client_class.SIGNATURES = {}
    elif change == "extra":
        client_class.SIGNATURES["reserve"] = "another-signature"
    else:
        client_class.SIGNATURES["ship"] = "another-signature"
    with pytest.raises(ContractMismatch) as caught:
        reference(ActivationAddress("shipment_a")).bind(client_class)
    assert getattr(caught.value, change) == (("reserve",) if change == "extra" else ("ship",))


def test_ordinary_clients_keep_the_environment_prefix_default(monkeypatch):
    client = ServiceClient(service="orders", verify=False)
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "retail")
    assert client._subject("rpc.ship") == "retail.orders.rpc.ship"
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "wholesale")
    assert client._subject("rpc.ship") == "wholesale.orders.rpc.ship"
