"""Typed shipment outputs preserve accepted routing and reject malformed publications."""

import json
import os
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from cliffracer import Output, OutputError, ServiceConfig
from cliffracer.broker_permissions import broker_permissions
from cliffracer.core.outputs import OutputContract, describe_outputs
from cliffracer.introspect import Description, describe
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import ActivationConflict, RpcContract, TemplateError
from tests.fixtures.shipment_outputs import (
    BatchSettings,
    ShipmentProgress,
    ShipmentWorker,
    shipment_output_template,
)

pytestmark = pytest.mark.unit


def runtime(**options):
    return ServiceConfig(
        **{
            "name": "shipment_batch_a",
            "namespace": "retail",
            "subject_prefix": "east",
            "health_port": 0,
            "nats_inbox_prefix": "_INBOX.shipping",
            **options,
        }
    )


def planned(**options):
    template = TemplateCatalog().register(shipment_output_template(**options))
    accepted = template.normalize({"warehouse": "north", "batch": "batch_a"})
    return template, accepted


def test_batch_routes_and_permissions_use_the_same_accepted_subject_families():
    template, accepted = planned()
    config = runtime()
    bindings = template.bind_outputs(accepted, config)
    assert bindings.publish_subjects == (
        "east.retail.batches.batch_a.closed",
        "east.retail.batches.batch_a.orders.*.progress",
    )
    assert (
        bindings.output("progress").subject
        == "east.retail.batches.batch_a.orders.{order_id}.progress"
    )
    assert (
        bindings.output("progress").resolve({"order_id": "order_9"})
        == "east.retail.batches.batch_a.orders.order_9.progress"
    )
    policy = broker_permissions(
        ShipmentWorker, config, role="service", extra_publish=bindings.publish_subjects
    )
    assert set(bindings.publish_subjects) <= set(policy.publish)
    assert "east.retail.batches.*.orders.*.progress" not in policy.publish
    assert "warehouse-access-canary" not in repr(bindings)
    assert "internal_note" not in repr(bindings)


def test_outbound_schema_and_routes_have_a_separate_serializable_contract():
    template, _ = planned()
    description = describe(ShipmentWorker)
    restored = Description.from_dict(description.to_dict())
    template.output_contract.verify(restored.outputs)
    assert OutputContract(tuple(restored.outputs)).identity == template.output_contract.identity
    assert all(
        output.to_dict()["schema"]["validation"]["type"] == "object" for output in restored.outputs
    )
    legacy = description.to_dict()
    legacy.pop("outputs")
    assert Description.from_dict(legacy).outputs == []
    with pytest.raises(OutputError, match="contract changed"):
        template.output_contract.verify(Description.from_dict(legacy).outputs)
    with pytest.raises(OutputError, match="repeats"):
        template.output_contract.verify([*restored.outputs, restored.outputs[0]])


@pytest.mark.parametrize(
    "subject",
    [
        "",
        "orders..sent",
        "orders.*.sent",
        "orders.>.sent",
        "orders.{batch.id}",
        "orders.{batch[0]}",
        "orders.{batch!r}",
        "orders.{batch:10}",
        "orders.{{batch}}",
        "orders.prefix-{batch}",
        "orders.{batch()}",
        "orders.{%batch%}",
        "orders.{unknown}",
    ],
)
def test_output_expressions_accept_only_declared_whole_token_placeholders(subject):
    with pytest.raises(OutputError):
        Output(ShipmentProgress, subject, settings=("batch",))


@pytest.mark.parametrize(
    "value", ["", "another.batch", "*", ">", "north south", "north\n", "north\x00", "{order_id}"]
)
def test_batch_routing_values_cannot_expand_the_declared_channel(value):
    template = TemplateCatalog().register(shipment_output_template())
    with pytest.raises(OutputError, match="single concrete subject token"):
        template.normalize({"warehouse": "north", "batch": value})


@pytest.mark.parametrize(
    "options",
    [
        {"settings": "batch"},
        {"settings": ("batch", "batch")},
        {"settings": ("batch",), "parameters": ("batch",)},
        {"parameters": ("batch.id",)},
    ],
)
def test_binding_times_and_parameter_names_are_explicit(options):
    with pytest.raises(OutputError):
        Output(ShipmentProgress, "batches.{batch}", **options)


def test_an_output_cannot_read_an_undeclared_settings_attribute():
    class UnknownSetting(ShipmentWorker):
        other = Output(ShipmentProgress, "warehouses.{absent}", settings=("absent",))

    template = TemplateCatalog().register(
        shipment_output_template(service_class=UnknownSetting, factory=UnknownSetting)
    )
    with pytest.raises(OutputError, match="undeclared settings field"):
        template.normalize({"warehouse": "north"})


def test_caller_factory_and_runtime_mutations_do_not_redirect_accepted_outputs():
    children = []

    def factory(settings, config):
        settings.batch = "factory_batch"
        child = ShipmentWorker(settings, config)
        children.append(child)
        return child

    template = TemplateCatalog().register(shipment_output_template(factory=factory))
    settings = BatchSettings(warehouse="north", batch="accepted")
    accepted = template.normalize(settings)
    settings.batch = "caller_batch"
    config = runtime()
    child = template.construct(accepted, config)
    config.namespace = "wholesale"
    assert child.settings.batch == "factory_batch"
    assert (
        child.output_bindings.output("progress").resolve({"order_id": "one"})
        == "east.retail.batches.accepted.orders.one.progress"
    )
    with pytest.raises(FrozenInstanceError):
        child.output_bindings.outputs[0].namespace = "another"


@pytest.mark.parametrize("change", ["subject", "parameters", "schema"])
def test_output_drift_is_refused_while_rpc_signatures_remain_stable(monkeypatch, change):
    class Progress(ShipmentProgress):
        pass

    class Worker(ShipmentWorker):
        progress = Output(
            Progress,
            "batches.{batch}.orders.{order_id}.progress",
            settings=("batch",),
            parameters=("order_id",),
        )

    catalog = TemplateCatalog()
    template = catalog.register(shipment_output_template(service_class=Worker, factory=Worker))
    accepted = template.normalize({"warehouse": "north", "batch": "batch_a"})
    if change == "schema":
        field = Field(gt=10)
        field.annotation = int
        Progress.model_fields["quantity"] = field
        Progress.model_rebuild(force=True)
    else:
        declaration = Output(
            Progress,
            "batches.{batch}.updates.{order_id}"
            if change == "subject"
            else "batches.{batch}.orders.{parcel}.progress",
            settings=("batch",),
            parameters=("order_id",) if change == "subject" else ("parcel",),
        )
        declaration.__set_name__(Worker, "progress")
        monkeypatch.setattr(Worker, "progress", declaration)
    assert template.contract == RpcContract.from_description(describe(Worker))
    assert template.output_contract.identity != OutputContract(describe_outputs(Worker)).identity
    with pytest.raises(OutputError, match="contract changed"):
        template.construct(accepted, runtime())
    with pytest.raises(ActivationConflict):
        catalog.register(template.definition)


async def test_bad_publication_parameters_or_payloads_never_reach_the_transport():
    template, accepted = planned()
    child = template.construct(accepted, runtime())
    child.nc = AsyncMock()
    valid = ShipmentProgress(quantity=3, stage="sent")
    for parameters in ({}, {"order_id": "one", "batch": "other"}, {"order_id": "one.extra"}):
        with pytest.raises(OutputError):
            await child.progress.publish(valid, parameters=parameters)
    invalid = ShipmentProgress.model_construct(quantity=-1, stage="sent")
    with pytest.raises(ValidationError):
        await child.progress.publish(invalid, parameters={"order_id": "one"})
    with pytest.raises(OutputError):
        await child.progress.publish(
            {"quantity": 3, "stage": "sent"}, parameters={"order_id": "one"}
        )
    child.nc.publish.assert_not_awaited()
    await child.progress.publish(valid, parameters={"order_id": "one"})
    sent = child.nc.publish.call_args
    assert sent.args[0] == "east.retail.batches.batch_a.orders.one.progress"
    assert json.loads(sent.args[1])["data"] == {"quantity": 3, "stage": "sent"}


@pytest.mark.parametrize(("supplied", "normalized"), [(0, False), (1, True), (1, 1.0)])
async def test_payload_type_changes_cannot_reach_shipment_subscribers(supplied, normalized):
    class DispatchStatus(BaseModel):
        quantities: list[int | bool | float]

        @field_validator("quantities")
        @classmethod
        def normalize_quantities(cls, values):
            return [type(normalized)(value) for value in values]

    class Worker(ShipmentWorker):
        status = Output(DispatchStatus, "batches.{batch}.status", settings=("batch",))

    template, accepted = planned(service_class=Worker, factory=Worker)
    child = template.construct(accepted, runtime())
    child.nc = AsyncMock()
    altered = DispatchStatus.model_construct(quantities=[supplied])
    rejected = False
    try:
        await child.status.publish(altered)
    except OutputError as exc:
        assert "round-trip" in str(exc)
        rejected = True
    child.nc.publish.assert_not_awaited()
    assert rejected
    valid = DispatchStatus(quantities=[supplied])
    await child.status.publish(valid)
    child.nc.publish.assert_awaited_once()
    sent = child.nc.publish.call_args
    assert sent.args[0] == "east.retail.batches.batch_a.status"
    quantity = json.loads(sent.args[1])["data"]["quantities"][0]
    assert type(quantity) is type(normalized)
    assert quantity == normalized


async def test_an_unbound_output_cannot_use_mutable_service_settings_as_a_route():
    child = ShipmentWorker(BatchSettings(warehouse="north"), runtime())
    child.nc = AsyncMock()
    with pytest.raises(OutputError, match="bound"):
        await child.progress.publish(
            ShipmentProgress(quantity=1, stage="sent"), parameters={"order_id": "one"}
        )
    child.nc.publish.assert_not_awaited()


@pytest.mark.parametrize("override", ["descriptor", "bindings"])
def test_factories_cannot_replace_the_bound_publishing_surface(override):
    def factory(settings, config):
        child = ShipmentWorker(settings, config)
        if override == "descriptor":
            child.progress = object()
        else:
            child._output_bindings = template.bind_outputs(accepted, config)
        return child

    template, accepted = planned(factory=factory)
    with pytest.raises(TemplateError, match="output"):
        template.construct(accepted, runtime())


def test_repeated_settings_reuse_accepted_defaults_but_keep_required_inputs(monkeypatch):
    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_output_template())
    accepted = template.normalize({"warehouse": "north"})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    assert template.normalize({"warehouse": "north"}, defaults=accepted) == accepted
    assert (
        template.normalize({"warehouse": "north", "batch": "second"}, defaults=accepted) != accepted
    )
    with pytest.raises(ValidationError):
        template.normalize({}, defaults=accepted)


@pytest.mark.parametrize(
    "changed_input", [{"batch_code": "second"}, {"route": {"batch": "second"}}]
)
def test_aliased_defaults_preserve_both_retry_values_and_explicit_changes(
    monkeypatch, changed_input
):
    class Settings(BaseModel):
        model_config = ConfigDict(extra="forbid")
        warehouse: str
        batch: str = Field(
            alias="routing",
            validation_alias=AliasChoices("routing", "batch_code", AliasPath("route", "batch")),
            default_factory=lambda: os.environ["SHIPMENT_BATCH"],
        )

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_output_template(settings_model=Settings))
    accepted = template.normalize({"warehouse": "north"})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    assert template.normalize({"warehouse": "north"}, defaults=accepted) == accepted
    changed = template.normalize({"warehouse": "north", **changed_input}, defaults=accepted)
    assert changed != accepted
    assert changed.materialize().batch == "second"


def test_settings_materialization_refuses_validator_changes(monkeypatch):
    class Settings(BatchSettings):
        @model_validator(mode="after")
        def route(self):
            self.batch = os.environ["SHIPMENT_BATCH"]
            return self

    monkeypatch.setenv("SHIPMENT_BATCH", "first")
    template = TemplateCatalog().register(shipment_output_template(settings_model=Settings))
    accepted = template.normalize({"warehouse": "north"})
    monkeypatch.setenv("SHIPMENT_BATCH", "second")
    with pytest.raises(TemplateError, match="round-trip"):
        accepted.materialize()
