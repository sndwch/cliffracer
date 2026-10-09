"""Shipment templates retain their contract, business inputs and construction identity."""

import copy
import os
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SecretStr,
    ValidationError,
    model_serializer,
    model_validator,
)
from pydantic.dataclasses import dataclass as validated_dataclass

from cliffracer import ServiceConfig, listener, rpc, timer
from cliffracer.core.extension import Extension
from cliffracer.introspect import describe
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import (
    ActivationAddress,
    ActivationConflict,
    ActivationReference,
    ActivationSnapshot,
    ActivationState,
    CleanupOutcome,
    ContractMismatch,
    LogicalIdentity,
    RpcContract,
    TemplateError,
)
from tests.fixtures.shipment_templates import (
    Parcel,
    Shipments,
    ShipmentSettings,
    shipment_template,
)

pytestmark = pytest.mark.unit


def settings():
    return ShipmentSettings(warehouse="north", destinations=["retail"], quantities={"bolts": 2})


def runtime(**overrides):
    return ServiceConfig(**{"name": "shipment_a", "health_port": 0, **overrides})


def test_catalog_pins_a_revision_and_resolves_explicit_revisions():
    catalog = TemplateCatalog()
    declaration = shipment_template()
    registered = catalog.register(declaration)
    assert catalog.register(declaration) is registered
    assert catalog.resolve("shipments", "warehouse-a") is registered
    next_revision = catalog.register(replace(declaration, revision="warehouse-b"))
    assert next_revision is not registered
    assert next_revision.contract == registered.contract
    with pytest.raises(ActivationConflict, match="already registered"):
        catalog.register(replace(declaration, max_rpc_concurrency=7))
    with pytest.raises(TemplateError, match="unknown template"):
        catalog.resolve("shipments", "missing")
    with pytest.raises(FrozenInstanceError):
        registered.definition.revision = "changed"


def test_callers_and_factories_cannot_change_accepted_shipment_settings():
    received = []

    def factory(config, assigned):
        received.append(config)
        config.destinations.append("wholesale")
        config.quantities["bolts"] = 9
        return Shipments(config, assigned)

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    original = settings()
    accepted = registered.normalize(original)
    original.destinations[0] = "wrong destination"
    original.quantities["bolts"] = 100
    first = registered.construct(accepted, runtime())
    second = registered.construct(accepted, runtime(name="shipment_b"))
    assert accepted.materialize().destinations == ["retail"]
    assert accepted.materialize().quantities == {"bolts": 2}
    assert first.settings.destinations == second.settings.destinations == ["retail", "wholesale"]
    assert received[0] is not received[1]
    first.settings.destinations.clear()
    assert second.settings.destinations == ["retail", "wholesale"]


def test_settings_comparison_uses_normalized_business_values():
    registered = TemplateCatalog().register(shipment_template())
    raw = {"warehouse": "north", "destinations": ["retail"], "quantities": {"bolts": "2"}}
    accepted = registered.normalize(raw)
    assert accepted == registered.normalize(settings())
    raw["destinations"].append("wholesale")
    assert accepted != registered.normalize(raw)
    with pytest.raises(ValidationError):
        registered.normalize({"warehouse": "north", "destinations": [], "unexpected": 4})


def test_settings_validators_cannot_mutate_the_callers_input():
    class TrimmedSettings(ShipmentSettings):
        @model_validator(mode="before")
        @classmethod
        def trim_destinations(cls, data):
            data["destinations"][:] = [value.strip() for value in data["destinations"]]
            return data

    raw = {"warehouse": "north", "destinations": [" retail "]}
    registered = TemplateCatalog().register(shipment_template(settings_model=TrimmedSettings))
    accepted = registered.normalize(raw)
    assert raw["destinations"] == [" retail "]
    assert accepted.materialize().destinations == ["retail"]


def test_repeated_shipment_settings_retain_strict_typed_defaults():
    @dataclass
    class Packing:
        labels: list[str]

    class DispatchSettings(BaseModel):
        model_config = ConfigDict(strict=True)
        warehouse: str
        ship_on: date = date(2026, 10, 1)
        destinations: tuple[str, ...] = ("retail",)
        manifest: UUID = UUID("63a2c6ee-96da-4368-bab5-f265aec00576")
        packing: Packing = Field(default_factory=lambda: Packing(["fragile"]))

    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize({"warehouse": "north"})
    retry = template.normalize({"warehouse": "north"}, defaults=accepted)
    shipment = template.construct(retry, runtime())
    assert retry == accepted
    assert shipment.settings.ship_on == date(2026, 10, 1)
    assert shipment.settings.destinations == ("retail",)
    assert shipment.settings.manifest == UUID("63a2c6ee-96da-4368-bab5-f265aec00576")
    assert shipment.settings.packing == Packing(["fragile"])
    shipment.settings.packing.labels.clear()
    assert accepted.materialize().packing.labels == ["fragile"]
    changed = template.normalize(
        {"warehouse": "north", "ship_on": date(2026, 10, 2)}, defaults=accepted
    )
    assert changed != accepted
    assert changed.materialize().ship_on == date(2026, 10, 2)
    with pytest.raises(ValidationError) as missing:
        template.normalize({}, defaults=accepted)
    assert [error["loc"] for error in missing.value.errors()] == [("warehouse",)]
    with pytest.raises(ValidationError) as invalid:
        template.normalize({"warehouse": "north", "ship_on": "2026-10-02"}, defaults=accepted)
    assert [error["loc"] for error in invalid.value.errors()] == [("ship_on",)]


def test_root_shipment_settings_remain_a_complete_value_on_retry():
    class DispatchSettings(RootModel[dict[str, str]]):
        root: dict[str, str] = Field(default_factory=dict)

    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize({"warehouse": "north"})
    retry = template.normalize({"warehouse": "north"}, defaults=accepted)
    assert retry == accepted
    assert retry.materialize().root == {"warehouse": "north"}
    cleared = template.normalize({}, defaults=accepted)
    assert cleared != accepted
    assert cleared.materialize().root == {}


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("shape", ["path", "serialization", "excluded"])
def test_shipment_defaults_must_be_reconstructible_before_acceptance(monkeypatch, nested, shape):
    options = {
        "path": {"validation_alias": AliasPath("route", "batch")},
        "serialization": {"serialization_alias": "routing"},
        "excluded": {"exclude": True},
    }[shape]

    class Routing(BaseModel):
        batch: str = Field(default_factory=lambda: os.environ["SHIPMENT_BATCH"], **options)

    class DispatchSettings(BaseModel):
        warehouse: str
        routes: list[Routing] = Field(default_factory=lambda: [Routing()])

    monkeypatch.setenv("SHIPMENT_BATCH", "routing-private-canary")
    template = TemplateCatalog().register(
        shipment_template(settings_model=DispatchSettings if nested else Routing)
    )
    with pytest.raises(TemplateError, match="batch.*round-trip.*serialized key") as caught:
        template.normalize({"warehouse": "north"} if nested else {})
    assert "routing-private-canary" not in str(caught.value)


@pytest.mark.parametrize(
    "config",
    [ConfigDict(populate_by_name=True), ConfigDict(validate_by_alias=False, validate_by_name=True)],
)
def test_shipment_alias_paths_can_use_explicit_field_name_population(monkeypatch, config):
    class DispatchSettings(BaseModel):
        model_config = config
        warehouse: str
        batch: str = Field(
            validation_alias=AliasPath("route", "batch"),
            default_factory=lambda: os.environ["SHIPMENT_BATCH"],
        )

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize({"warehouse": "north"})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    assert template.normalize({"warehouse": "north"}, defaults=accepted) == accepted
    changed = template.normalize({"warehouse": "north", "batch": "second"}, defaults=accepted)
    assert changed.materialize().batch == "second"


def test_ignored_field_names_do_not_displace_accepted_shipment_defaults(monkeypatch):
    class DispatchSettings(BaseModel):
        warehouse: str
        batch: str = Field(
            alias="routing",
            validation_alias=AliasChoices("routing", AliasPath("route", "batch")),
            default_factory=lambda: os.environ["SHIPMENT_BATCH"],
        )

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize({"warehouse": "north"})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    retry = template.normalize({"warehouse": "north", "batch": "ignored"}, defaults=accepted)
    assert retry == accepted
    assert retry.materialize().batch == "first"


def test_single_key_alias_paths_retain_accepted_shipment_defaults(monkeypatch):
    class DispatchSettings(BaseModel):
        batch: str = Field(
            validation_alias=AliasPath("batch"),
            default_factory=lambda: os.environ["SHIPMENT_BATCH"],
        )

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize({})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    assert template.normalize({}, defaults=accepted) == accepted
    assert (
        template.normalize({"batch": "second"}, defaults=accepted).materialize().batch == "second"
    )


@pytest.mark.parametrize("decorator", [dataclass, validated_dataclass])
def test_dataclass_routing_defaults_must_accept_their_serialized_keys(monkeypatch, decorator):
    @decorator
    class Routing:
        batch: str = Field(
            validation_alias=AliasPath("route", "batch"),
            default_factory=lambda: os.environ["SHIPMENT_BATCH"],
        )

    class DispatchSettings(BaseModel):
        routing: Routing

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    with pytest.raises(TemplateError, match="batch.*round-trip.*serialized key"):
        template.normalize({"routing": {}})


@pytest.mark.parametrize("nested", [False, True])
def test_custom_serializers_must_retain_every_shipment_setting(monkeypatch, nested):
    class Routing(BaseModel):
        warehouse: str = "north"
        batch: str = Field(default_factory=lambda: os.environ["SHIPMENT_BATCH"])

        @model_serializer
        def serialize(self):
            return {"warehouse": self.warehouse}

    class DispatchSettings(BaseModel):
        routes: dict[str, Routing] = Field(default_factory=lambda: {"retail": Routing()})

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(
        shipment_template(settings_model=DispatchSettings if nested else Routing)
    )
    with pytest.raises(TemplateError, match="batch.*round-trip.*serialized key"):
        template.normalize({})


class DispatchWindow(Enum):
    MORNING = "morning"


@pytest.mark.parametrize("key", [True, DispatchWindow.MORNING, datetime(2026, 10, 1)])
def test_serialized_mapping_keys_preserve_nested_shipment_routes(key):
    class Routing(BaseModel):
        batch: str = "first"

    key_type = type(key)

    class DispatchSettings(BaseModel):
        routes: dict[key_type, Routing]

    original = DispatchSettings(routes={key: Routing()})
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize(original)
    assert accepted.materialize() == original
    assert accepted.materialize().routes[key].batch == "first"


@pytest.mark.parametrize("single_key", [False, True])
def test_dataclass_shipment_routes_retain_supported_aliases(monkeypatch, single_key):
    @dataclass
    class Routing:
        batch: str = Field(
            validation_alias=AliasPath("batch") if single_key else AliasPath("route", "batch"),
            default_factory=lambda: os.environ["SHIPMENT_BATCH"],
        )

    class DispatchSettings(BaseModel):
        model_config = ConfigDict(populate_by_name=not single_key)
        routing: Routing

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize({"routing": {}})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    assert accepted.materialize().routing.batch == "first"


def test_forward_dataclass_annotations_cannot_hide_omitted_shipment_routes(monkeypatch):
    @dataclass
    class Routing:
        batch: "Annotated[str, Field(validation_alias=AliasPath('route', 'batch'))]" = field(
            default_factory=lambda: os.environ["SHIPMENT_BATCH"]
        )

    class DispatchSettings(BaseModel):
        routing: Routing

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    with pytest.raises(TemplateError, match="batch.*round-trip.*serialized key"):
        template.normalize({"routing": {}})


def test_locally_declared_dataclass_types_retain_valid_shipment_routes():
    @dataclass
    class Routing:
        batch: str = "first"

    @dataclass
    class Warehouse:
        routing: "Routing"

    class DispatchSettings(BaseModel):
        warehouse: Warehouse

    original = DispatchSettings(warehouse={"routing": {}})
    template = TemplateCatalog().register(shipment_template(settings_model=DispatchSettings))
    accepted = template.normalize(original)
    assert accepted.materialize() == original
    assert accepted.materialize().warehouse.routing.batch == "first"


def test_lossy_settings_serialization_is_refused_without_echoing_values():
    class ShippingCredentials(BaseModel):
        secret: SecretStr

    registered = TemplateCatalog().register(shipment_template(settings_model=ShippingCredentials))
    with pytest.raises(TemplateError, match="round-trip") as caught:
        registered.normalize({"secret": "private-canary"})
    assert "private-canary" not in str(caught.value)


def test_registered_settings_schema_cannot_change_under_an_accepted_batch():
    class FreightSettings(ShipmentSettings):
        pass

    registered = TemplateCatalog().register(shipment_template(settings_model=FreightSettings))
    accepted = registered.normalize(settings())
    warehouse = Field(min_length=10)
    warehouse.annotation = str
    FreightSettings.model_fields["warehouse"] = warehouse
    FreightSettings.model_rebuild(force=True)
    with pytest.raises(TemplateError, match="settings schema changed"):
        registered.construct(accepted, runtime())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "other_shipment"),
        ("namespace", "another"),
        ("subject_prefix", "another"),
        ("health_host", "0.0.0.0"),
        ("health_port", 8765),
        ("nats_url", "nats://other:4222"),
        ("nats_token", "private-canary"),
        ("max_rpc_concurrency", 99),
    ],
)
def test_factory_cannot_change_the_assigned_runtime(field, value):
    def factory(config, assigned):
        setattr(assigned, field, value)
        return Shipments(config, assigned)

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    assigned = runtime()
    before = assigned.model_copy(deep=True)
    with pytest.raises(TemplateError, match="host-assigned") as caught:
        registered.construct(registered.normalize(settings()), assigned)
    assert assigned == before
    assert "private-canary" not in str(caught.value)


def test_factory_cannot_relabel_the_facade_while_internal_routing_stays_elsewhere():
    def factory(config, assigned):
        wrong = assigned.model_copy(update={"name": "other_shipment"})
        service = Shipments(config, wrong)
        service.config = assigned
        return service

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    with pytest.raises(TemplateError, match="host-assigned"):
        registered.construct(registered.normalize(settings()), runtime())


def test_registered_dispatch_limits_apply_without_mutating_host_configuration():
    registered = TemplateCatalog().register(
        shipment_template(max_rpc_concurrency=3, max_async_rpc_concurrency=4)
    )
    assigned = runtime(max_rpc_concurrency=1, max_async_rpc_concurrency=2)
    service = registered.construct(registered.normalize(settings()), assigned)
    assert service.config.max_rpc_concurrency == 3
    assert service.config.max_async_rpc_concurrency == 4
    assert assigned.max_rpc_concurrency == 1
    assert assigned.max_async_rpc_concurrency == 2


def test_factories_return_the_exact_registered_class():
    class WholesaleShipments(Shipments):
        pass

    for factory in (lambda config, assigned: object(), WholesaleShipments):
        registered = TemplateCatalog().register(shipment_template(factory=factory))
        with pytest.raises(TemplateError, match="exact registered service class"):
            registered.construct(registered.normalize(settings()), runtime())


def test_an_object_can_only_be_claimed_once_even_across_catalogs():
    registered = TemplateCatalog().register(shipment_template())
    accepted = registered.normalize(settings())
    service = registered.construct(accepted, runtime())
    second = TemplateCatalog().register(shipment_template(factory=lambda config, assigned: service))
    with pytest.raises(TemplateError, match="already used or owned"):
        second.construct(second.normalize(settings()), runtime())
    assert service.stops == 0
    assert not service.container.lifecycle.is_stopped


def test_service_documentation_and_runtime_names_do_not_change_the_rpc_contract(monkeypatch):
    registered = TemplateCatalog().register(shipment_template())
    first_description = describe(Shipments, service="shipment_a")
    monkeypatch.setattr(Shipments.ship, "__doc__", "Book a parcel for this warehouse.")
    second_description = describe(Shipments, service="shipment_b")
    assert first_description.description_hash != second_description.description_hash
    registered.contract.verify(second_description)
    assert registered.contract == RpcContract.from_description(first_description)
    service = registered.construct(registered.normalize(settings()), runtime())
    assert service.config.name == "shipment_a"


@pytest.mark.parametrize("change", ["missing", "extra", "changed"])
def test_rpc_shape_changes_are_refused_before_construction(monkeypatch, change):
    built = []

    def factory(config, assigned):
        built.append(config)
        return Shipments(config, assigned)

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    accepted = registered.normalize(settings())

    @rpc
    async def ship(self, destination: str) -> bool:
        return bool(destination)

    if change == "missing":
        monkeypatch.delattr(Shipments, "ship")
    else:
        monkeypatch.setattr(
            Shipments, "ship" if change == "changed" else "reserve", ship, raising=False
        )
    with pytest.raises(ContractMismatch) as caught:
        registered.construct(accepted, runtime())
    assert getattr(caught.value, change) == (("reserve",) if change == "extra" else ("ship",))
    assert built == []


def test_a_referenced_parcel_schema_change_invalidates_the_registered_contract():
    registered = TemplateCatalog().register(shipment_template())
    original = Parcel.model_fields["quantity"]
    changed = Field(gt=5)
    changed.annotation = int
    try:
        Parcel.model_fields["quantity"] = changed
        Parcel.model_rebuild(force=True)
        with pytest.raises(ContractMismatch) as caught:
            registered.contract.verify(describe(Shipments))
        assert caught.value.changed == ("ship",)
    finally:
        Parcel.model_fields["quantity"] = original
        Parcel.model_rebuild(force=True)


def test_a_factory_cannot_change_the_rpc_contract_during_construction(monkeypatch):
    @rpc
    async def reserve(self, warehouse: str) -> bool:
        return bool(warehouse)

    def factory(config, assigned):
        service = Shipments(config, assigned)
        monkeypatch.setattr(Shipments, "reserve", reserve, raising=False)
        return service

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    with pytest.raises(ContractMismatch) as caught:
        registered.construct(registered.normalize(settings()), runtime())
    assert caught.value.extra == ("reserve",)


def test_a_factory_cannot_replace_a_declared_rpc_on_one_instance():
    async def ship(destination: str) -> bool:
        return bool(destination)

    def factory(config, assigned):
        service = Shipments(config, assigned)
        service.ship = ship
        return service

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    with pytest.raises(TemplateError, match="overrides registered RPC handler 'ship'"):
        registered.construct(registered.normalize(settings()), runtime())


@pytest.mark.parametrize("declaration", ["listener", "timer", "transport"])
def test_unsupported_declarations_are_named_without_starting_them(declaration):
    class Freight(Shipments):
        pass

    async def dispatch(self) -> None:
        pass

    if declaration == "listener":
        Freight.dispatch = listener("shipment.created")(dispatch)
    elif declaration == "timer":
        Freight.dispatch = timer(interval=30)(dispatch)
    else:

        class FreightTransport(Extension):
            async def start(self) -> None:
                raise AssertionError("registration must not start an extension")

        Freight.transport = FreightTransport()
    with pytest.raises(
        TemplateError, match="transport.start" if declaration == "transport" else "dispatch"
    ):
        TemplateCatalog().register(shipment_template(service_class=Freight))


def test_pure_worker_hooks_compose_with_templates():
    class TrackShipment(Extension):
        async def worker_setup(self, ctx) -> None:
            ctx.data["shipment"] = True

    class Freight(Shipments):
        tracking = TrackShipment()

    registered = TemplateCatalog().register(
        shipment_template(service_class=Freight, factory=Freight)
    )
    service = registered.construct(registered.normalize(settings()), runtime())
    assert service.tracking.service is service


def test_factory_added_transports_are_checked_before_startup():
    class FreightTransport(Extension):
        async def start(self) -> None:
            raise AssertionError("construction must not start a transport")

    def factory(config, assigned):
        service = Shipments(config, assigned)
        service.add_extension(FreightTransport(), "freight")
        return service

    registered = TemplateCatalog().register(shipment_template(factory=factory))
    with pytest.raises(TemplateError, match="freight.start"):
        registered.construct(registered.normalize(settings()), runtime())


@pytest.mark.parametrize(
    "overrides",
    [
        {"startup_timeout": float("inf")},
        {"cleanup_timeout": float("nan")},
        {"startup_timeout": 0},
        {"max_rpc_concurrency": 0},
        {"max_async_rpc_concurrency": True},
        {"service_class": object},
        {"settings_model": dict},
        {"factory": lambda: None},
    ],
)
def test_invalid_template_declarations_fail_at_the_boundary(overrides):
    with pytest.raises(TemplateError):
        shipment_template(**overrides)


def test_async_factories_are_refused_without_creating_a_coroutine():
    async def factory(config, assigned):
        return Shipments(config, assigned)

    with pytest.raises(TemplateError, match="synchronous"):
        shipment_template(factory=factory)


def test_snapshots_preserve_the_generation_and_do_not_embed_business_settings():
    reference = ActivationReference(
        LogicalIdentity("retail", "shipments", "batch-a"),
        "host-a",
        1,
        "warehouse-a",
        RpcContract.from_description(describe(Shipments)),
        ActivationAddress("shipment_a"),
    )
    snapshot = ActivationSnapshot(
        reference,
        "order-batch",
        ActivationState.UNFINISHED,
        "cleanup_deadline",
        CleanupOutcome(False, 1),
    )
    successor = replace(reference, generation=2, address=ActivationAddress("shipment_b"))
    assert snapshot.reference.generation == 1
    assert snapshot.reference.address != successor.address
    assert len({reference, successor}) == 2
    with pytest.raises(FrozenInstanceError):
        snapshot.reference.address.service = "shipment_b"
    assert "settings" not in snapshot.__dict__
    assert copy.deepcopy(snapshot) == snapshot
    with pytest.raises(ValueError, match="unfinished"):
        CleanupOutcome(True, 1)


@pytest.mark.parametrize(
    "address",
    [
        {"service": "shipment.*"},
        {"service": "shipment", "namespace": "retail.*"},
        {"service": "shipment", "subject_prefix": "retail.>"},
    ],
)
def test_activation_addresses_reuse_runtime_subject_validation(address):
    with pytest.raises(ValidationError):
        ActivationAddress(**address)
