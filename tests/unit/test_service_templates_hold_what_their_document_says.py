"""Claims in `docs/service-templates.md` that no other test reads.

Each test names the sentence it holds. The behaviour a template promises is
mostly pinned in `test_service_templates.py`; these are the promises that
sat beside it unpinned: nothing dials, claims are weak, a stopped object is
refused, every declaration marker is refused, a revision cannot be registered
twice with different content, the error family, the contract's table and the
fields an activation value carries.
"""

import gc
import weakref
from dataclasses import fields, replace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import Field

from cliffracer import ServiceConfig, broadcast, listener, rpc, timer, validated_listener
from cliffracer.core.extension import Extension
from cliffracer.introspect import describe
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import (
    ActivationAddress,
    ActivationCapacityError,
    ActivationConflict,
    ActivationReference,
    ActivationSnapshot,
    ActivationUnavailable,
    ContractMismatch,
    LogicalIdentity,
    RpcContract,
    SupervisionError,
    TemplateError,
)
from cliffracer.runners.templates import _constructed
from tests.fixtures.shipment_templates import (
    Parcel,
    Shipments,
    ShipmentSettings,
    shipment_client_class,
    shipment_template,
)

pytestmark = pytest.mark.unit


def settings():
    return ShipmentSettings(warehouse="north", destinations=["retail"])


def runtime(**overrides):
    return ServiceConfig(**{"name": "shipment_a", "health_port": 0, **overrides})


# --- "Registration performs no broker operations" / "starts no resources" ----


def test_registering_normalizing_and_constructing_dial_no_broker():
    never = AsyncMock(side_effect=AssertionError("a template operation dialled the broker"))
    with patch("cliffracer.core.dial.connect", never), patch("nats.connect", never):
        registered = TemplateCatalog().register(shipment_template())
        service = registered.construct(registered.normalize(settings()), runtime())
    never.assert_not_awaited()
    lifecycle = service.container.lifecycle
    assert service.nc is None
    assert service.health_listener.port is None
    assert not (lifecycle.is_running or lifecycle.is_starting or lifecycle.is_stopped)
    assert not lifecycle.active_tasks


# --- "Claims retain weak references" -----------------------------------------


def test_a_claim_does_not_keep_an_unreachable_service_alive():
    registered = TemplateCatalog().register(shipment_template())
    service = registered.construct(registered.normalize(settings()), runtime())
    claim = id(service)
    assert claim in _constructed
    seen = weakref.ref(service)
    del service
    gc.collect()
    assert seen() is None
    assert claim not in _constructed


# --- "An already started, stopped or claimed object is refused without
#     calling its teardown" ------------------------------------------------


async def test_a_stopped_object_is_refused_and_its_teardown_is_not_called():
    service = Shipments(settings(), runtime())
    await service.stop()
    assert service.container.lifecycle.is_stopped
    calls = []
    original = service.stop

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    service.stop = spy
    registered = TemplateCatalog().register(
        shipment_template(factory=lambda config, assigned: service)
    )
    with pytest.raises(TemplateError, match="already used or owned"):
        registered.construct(registered.normalize(settings()), runtime())
    assert calls == []
    assert service.stops == 0


# --- "Registration names and refuses declared listeners, broadcasts and timers"


@pytest.mark.parametrize("declaration", ["listener", "validated_listener", "broadcast", "timer"])
def test_every_event_declaration_marker_is_refused_at_registration(declaration):
    class Freight(Shipments):
        pass

    async def dispatch(self, subject: str = "") -> None:
        pass

    decorate = {
        "listener": listener("shipment.created"),
        "validated_listener": validated_listener("shipment.created", Parcel),
        "broadcast": broadcast("shipment.created"),
        "timer": timer(interval=30),
    }[declaration]
    Freight.dispatch = decorate(dispatch)
    with pytest.raises(TemplateError, match="unsupported template declaration dispatch"):
        TemplateCatalog().register(shipment_template(service_class=Freight))


# --- "Ordinary service construction outside the catalog retains its own
#     extension contract" -----------------------------------------------------


def test_an_extension_overriding_start_is_refused_only_by_the_catalog():
    class Transport(Extension):
        async def start(self) -> None:
            raise AssertionError("construction must not start an extension")

    class Freight(Shipments):
        transport = Transport()

    service = Freight(settings(), runtime())
    assert service.transport.service is service
    with pytest.raises(TemplateError, match="transport.start"):
        TemplateCatalog().register(shipment_template(service_class=Freight, factory=Freight))


# --- "Registration and construction check for class and settings-schema drift" -


def test_registering_a_revision_again_after_its_class_changed_is_a_conflict():
    class Freight(Shipments):
        pass

    catalog = TemplateCatalog()
    declaration = shipment_template(service_class=Freight, factory=Freight)
    catalog.register(declaration)

    @rpc
    async def reserve(self, warehouse: str) -> bool:
        return bool(warehouse)

    Freight.reserve = reserve
    with pytest.raises(ActivationConflict, match="already registered"):
        catalog.register(declaration)
    renamed = catalog.register(replace(declaration, revision="warehouse-b"))
    assert "reserve" in dict(renamed.contract.signatures)


def test_registering_a_revision_again_after_its_settings_schema_changed_is_a_conflict():
    class FreightSettings(ShipmentSettings):
        pass

    catalog = TemplateCatalog()
    declaration = shipment_template(settings_model=FreightSettings)
    catalog.register(declaration)
    warehouse = Field(min_length=10)
    warehouse.annotation = str
    FreightSettings.model_fields["warehouse"] = warehouse
    FreightSettings.model_rebuild(force=True)
    with pytest.raises(ActivationConflict, match="already registered"):
        catalog.register(declaration)


# --- "`SupervisionError` is the common error base ..." -----------------------


@pytest.mark.parametrize(
    "error",
    [
        TemplateError,
        ContractMismatch,
        ActivationConflict,
        ActivationCapacityError,
        ActivationUnavailable,
    ],
)
def test_every_activation_error_is_a_supervision_error(error):
    assert issubclass(error, SupervisionError)


def test_only_the_contract_mismatch_specializes_the_template_error():
    assert issubclass(ContractMismatch, TemplateError)
    for sibling in (ActivationConflict, ActivationCapacityError, ActivationUnavailable):
        assert not issubclass(sibling, TemplateError)
    assert SupervisionError not in TemplateError.__subclasses__()


# --- "An `RpcContract` freezes the complete sorted method/signature table" ---


def test_a_contract_is_a_sorted_table_whose_identity_follows_its_content_alone():
    forward = RpcContract((("reserve", "r1"), ("ship", "s1")))
    shuffled = RpcContract((("ship", "s1"), ("reserve", "r1")))
    assert forward.signatures == (("reserve", "r1"), ("ship", "s1"))
    assert shuffled.signatures == forward.signatures
    assert shuffled.identity == forward.identity
    assert RpcContract((("reserve", "r1"), ("ship", "s2"))).identity != forward.identity
    assert RpcContract((("reserve", "r1"),)).identity != forward.identity
    assert forward.identity.startswith("sha256:")


def test_a_contract_is_the_complete_method_table_of_the_described_class():
    description = describe(Shipments)
    contract = RpcContract.from_description(description)
    assert [name for name, _ in contract.signatures] == sorted(m.name for m in description.methods)
    assert dict(contract.signatures) == {m.name: m.signature_hash for m in description.methods}


# --- "An activation reference contains ...; the address contains ... no
#     broker credentials"; snapshots hold lifecycle observations only --------


def test_an_activation_reference_carries_exactly_the_documented_values():
    assert [f.name for f in fields(ActivationReference)] == [
        "identity",
        "incarnation",
        "generation",
        "revision",
        "contract",
        "address",
        "outputs",
    ]
    assert [f.name for f in fields(LogicalIdentity)] == ["scope", "template", "key"]
    assert [f.name for f in fields(ActivationAddress)] == [
        "service",
        "namespace",
        "subject_prefix",
    ]


def test_a_snapshot_holds_lifecycle_observations_and_no_settings_or_credentials():
    names = {f.name for f in fields(ActivationSnapshot)}
    assert names == {
        "reference",
        "owner",
        "state",
        "reason",
        "cleanup",
        "broker_state",
        "retry_until",
    }


# --- "`bind` ... Connection and request options are ordinary `ServiceClient`
#     constructor arguments ... live verification still runs on first use" ----


def test_binding_leaves_live_verification_on_unless_the_caller_turns_it_off():
    reference = ActivationReference(
        LogicalIdentity("retail", "shipments", "batch-a"),
        "host",
        1,
        "warehouse-a",
        RpcContract.from_description(describe(Shipments)),
        ActivationAddress("shipment_a"),
    )
    verified = reference.bind(shipment_client_class())
    assert verified._verify is True
    assert verified._verified is False
    assert reference.bind(shipment_client_class(), verify=False)._verify is False
