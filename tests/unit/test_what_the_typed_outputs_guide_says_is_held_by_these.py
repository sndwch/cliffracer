"""Claims `docs/typed-outputs.md` makes that no other test pins, each held here.

The guide's two Python blocks are executed as written, so an example that stops running, or a
route it asserts that changes, fails here and not in a reader's editor. The JSON block is compared
with a real envelope, field for field. The rest follow the guide's order: what an `Output` does
not change, how placeholders are substituted, what publication checks, what a typed event carries,
what permissions and idempotency do with it.

Routes, drift refusal, retained defaults, generation recognition, JetStream coverage and broker
grants are held by `test_output_bindings.py` in the unit and integration tiers.
"""

import asyncio
import json
import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from cliffracer import CliffracerService, Output, OutputError, ServiceConfig, listener, rpc
from cliffracer.broker_permissions import broker_permissions
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.extension import Extension
from cliffracer.core.idempotency import IdempotencyContext
from cliffracer.core.outputs import OutputProducer
from cliffracer.core.subjects import subject_matches
from cliffracer.introspect import describe
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import RpcContract, TemplateError
from tests.fixtures.shipment_outputs import (
    ShipmentProgress,
    ShipmentWorker,
    shipment_output_client_class,
    shipment_output_template,
)

pytestmark = pytest.mark.unit

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "typed-outputs.md"
FENCE = re.compile(r"^```(?P<lang>\w+)[^\S\n]*\n(?P<source>.*?)^```[^\S\n]*$", re.S | re.M)


def fences(language: str) -> list[str]:
    return [m["source"] for m in FENCE.finditer(GUIDE.read_text()) if m["lang"] == language]


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


class Wire:
    """A stand-in for `nc` that records what is published."""

    def __init__(self):
        self.published: list[tuple[str, bytes, dict]] = []

    async def publish(self, subject, data, headers=None, **kw):
        self.published.append((subject, data, dict(headers or {})))

    def last(self):
        subject, data, headers = self.published[-1]
        return subject, json.loads(data), headers


def child_of(template, accepted, config=None, *, producer=None):
    config = config or runtime()
    bindings = template.bind_outputs(accepted, config)
    if producer is not None:
        bindings = replace(bindings, producer=producer)
    child = template.construct(accepted, config, bindings=bindings)
    child.nc = Wire()
    return child


def shipment_child(**options):
    template = TemplateCatalog().register(shipment_output_template(**options))
    accepted = template.normalize({"warehouse": "north", "batch": "north"})
    return template, accepted


def run(coroutine):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coroutine)


# -- the examples run as written ----------------------------------------------------------------


def test_the_guides_python_blocks_run_and_the_template_constructs_what_they_plan():
    blocks = fences("python")
    assert len(blocks) == 2, "the guide has an example and a permissions block"
    namespace: dict = {"__name__": "typed_outputs_guide"}
    for block in blocks:
        exec(compile(block, str(GUIDE), "exec"), namespace)  # asserts its own routes

    template, accepted, config = namespace["template"], namespace["accepted"], namespace["runtime"]
    child = template.construct(accepted, config)
    child.nc = Wire()
    assert run(child.ship(order_id="one", quantity=2)) == 2
    subject, envelope, _ = child.nc.last()
    assert subject == "east.retail.batches.north.orders.one.progress"
    assert envelope["data"] == {"quantity": 2}

    policy = namespace["policy"]
    assert set(namespace["planned"].publish_subjects) <= set(policy.publish)
    assert subject not in policy.publish  # the grant is the wildcard family, not a literal


# -- what an Output does not change -------------------------------------------------------------


def test_declaring_an_output_adds_no_rpc_method_and_no_broker_grant():
    class Plain(CliffracerService):
        @rpc
        async def ship(self, order_id: str, quantity: int) -> int:
            return 0

        @rpc
        async def finish(self) -> int:
            return 0

    config = runtime()
    with_outputs, without = describe(ShipmentWorker), describe(Plain)
    assert RpcContract.from_description(with_outputs).signatures == (
        RpcContract.from_description(without).signatures
    )
    assert without.outputs == []
    assert with_outputs.outputs
    assert broker_permissions(ShipmentWorker, config, role="service") == broker_permissions(
        Plain, config, role="service"
    )


def test_a_template_refuses_inbound_listeners():
    class Listening(ShipmentWorker):
        @listener("batches.north.closed")
        async def on_closed(self, subject: str) -> None: ...

    with pytest.raises(TemplateError, match="unsupported template declaration"):
        TemplateCatalog().register(
            shipment_output_template(service_class=Listening, factory=Listening)
        )


def test_rpc_grants_name_the_assigned_service_so_another_assigned_name_has_none():
    policy = broker_permissions(ShipmentWorker, runtime(), role="service")
    assert "east.retail.shipment_batch_a.rpc.*" in policy.subscribe
    assigned = "east.retail.activation_incarnation_0123.rpc.ship"
    assert not any(subject_matches(grant, assigned) for grant in policy.subscribe), policy.subscribe


# -- substitution ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "orders.{batch|upper}",
        "orders.{{ batch }}",
        "orders.{% for x in batch %}{{ x }}{% endfor %}",
        "orders.{% if batch %}",
        "orders.a b",
        "orders.a\tb",
        "orders.a\x00b",
        "orders.a{b",
        "orders.a}b",
    ],
)
def test_filters_loops_jinja_blocks_and_unsafe_literal_tokens_are_refused(subject):
    # No placeholder is declared, so only the token check can refuse these.
    with pytest.raises(OutputError, match="single concrete subject token"):
        Output(ShipmentProgress, subject)


def test_setting_names_are_the_models_python_field_names_not_its_aliases():
    class Aliased(BaseModel):
        model_config = ConfigDict(extra="forbid")
        batch: str = Field(alias="routing")

    class ByAlias(CliffracerService):
        progress = Output(ShipmentProgress, "batches.{routing}", settings=("routing",))

    class ByName(CliffracerService):
        progress = Output(ShipmentProgress, "batches.{batch}", settings=("batch",))

    refused = TemplateCatalog().register(
        shipment_output_template(
            service_class=ByAlias,
            settings_model=Aliased,
            factory=lambda s, r: ByAlias(r),
        )
    )
    with pytest.raises(OutputError, match="undeclared settings field"):
        refused.normalize({"routing": "north"})

    accepted_template = TemplateCatalog().register(
        shipment_output_template(
            service_class=ByName, settings_model=Aliased, factory=lambda s, r: ByName(r)
        )
    )
    bound = accepted_template.bind_outputs(
        accepted_template.normalize({"routing": "north"}), runtime()
    )
    assert bound.publish_subjects == ("east.retail.batches.north",)


def test_a_repeated_placeholder_takes_the_same_value_at_every_position():
    class Repeating(ShipmentWorker):
        twice = Output(
            ShipmentProgress,
            "batches.{batch}.orders.{order_id}.of.{batch}.again.{order_id}",
            settings=("batch",),
            parameters=("order_id",),
        )

    template = TemplateCatalog().register(
        shipment_output_template(service_class=Repeating, factory=Repeating)
    )
    bindings = template.bind_outputs(
        template.normalize({"warehouse": "north", "batch": "north"}), runtime()
    )
    twice = bindings.output("twice")
    assert twice.resolve({"order_id": "one"}) == (
        "east.retail.batches.north.orders.one.of.north.again.one"
    )
    assert twice.publish_subject == "east.retail.batches.north.orders.*.of.north.again.*"


def test_route_values_that_are_not_strings_are_refused():
    class Numbered(BaseModel):
        batch: int

    class ByNumber(CliffracerService):
        progress = Output(
            ShipmentProgress,
            "batches.{batch}.{order_id}",
            settings=("batch",),
            parameters=("order_id",),
        )

    template = TemplateCatalog().register(
        shipment_output_template(
            service_class=ByNumber, settings_model=Numbered, factory=lambda s, r: ByNumber(r)
        )
    )
    with pytest.raises(OutputError, match="single concrete subject token"):
        template.normalize({"batch": 7})

    template, accepted = shipment_child()
    bound = template.bind_outputs(accepted, runtime())
    with pytest.raises(OutputError, match="single concrete subject token"):
        bound.output("progress").resolve({"order_id": 7})


def test_the_namespace_and_the_environment_prefix_are_applied_once_by_the_ordinary_builder(
    monkeypatch,
):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "envprefix")
    config = ServiceConfig(name="shipment_batch_a", namespace="retail", health_port=0)
    template, accepted = shipment_child()
    bound = template.bind_outputs(accepted, config)

    expected = HandlerDiscovery.with_namespace(config, "batches.north.orders.{order_id}.progress")
    assert bound.output("progress").subject == expected
    assert expected == "envprefix.retail.batches.north.orders.{order_id}.progress"
    for subject in bound.publish_subjects:
        assert subject.split(".").count("envprefix") == 1
        assert subject.split(".").count("retail") == 1


# -- publication ----------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["subclass", "mutated", "not_an_object", "aliases", "serializer"])
def test_the_declared_class_and_a_round_tripping_json_object_are_what_publication_sends(how):
    class Alias(BaseModel):
        x: int = Field(validation_alias="in", serialization_alias="out")

    class Asymmetric(BaseModel):
        x: int

        def model_dump_json(self, **kwargs):
            return json.dumps({"x": self.x + 1})

    class Listing(RootModel[list[int]]): ...

    class Subclassed(ShipmentProgress): ...

    mutated = ShipmentProgress(quantity=1, stage="sent")
    mutated.quantity = -3
    model, value, expected = {
        "subclass": (ShipmentProgress, Subclassed(quantity=1, stage="sent"), OutputError),
        "mutated": (ShipmentProgress, mutated, ValidationError),
        "not_an_object": (Listing, Listing([1]), OutputError),
        "aliases": (Alias, Alias(**{"in": 1}), ValidationError),
        "serializer": (Asymmetric, Asymmetric(x=1), OutputError),
    }[how]

    class Worker(ShipmentWorker):
        status = Output(model, "batches.{batch}.status", settings=("batch",))

    template, accepted = shipment_child(service_class=Worker, factory=Worker)
    child = child_of(template, accepted)
    with pytest.raises(expected):
        run(child.status.publish(value))
    assert child.nc.published == []


def test_publication_checks_the_contract_again_so_a_drifted_class_cannot_publish():
    class Worker(ShipmentWorker): ...

    template, accepted = shipment_child(service_class=Worker, factory=Worker)
    child = child_of(template, accepted)
    drifted = Output(
        ShipmentProgress,
        "batches.{batch}.moved.{order_id}",
        settings=("batch",),
        parameters=("order_id",),
    )
    drifted.__set_name__(Worker, "progress")
    Worker.progress = drifted
    with pytest.raises(OutputError, match="contract changed"):
        run(
            child.progress.publish(
                ShipmentProgress(quantity=1, stage="sent"), parameters={"order_id": "one"}
            )
        )
    assert child.nc.published == []


# -- the contract and the bindings --------------------------------------------------------------


def test_the_description_lists_each_output_with_both_schemas_beside_the_rpc_table():
    described = describe(ShipmentWorker).to_dict()
    outputs = {output["name"]: output for output in described["outputs"]}
    assert set(outputs) == {"progress", "summary"}
    progress = outputs["progress"]
    assert set(progress) == {"name", "subject", "settings", "parameters", "schema"}
    assert progress["subject"] == "batches.{batch}.orders.{order_id}.progress"
    assert (progress["settings"], progress["parameters"]) == (["batch"], ["order_id"])
    assert set(progress["schema"]) == {"validation", "serialization"}
    assert not {"progress", "summary"} & {method["name"] for method in described["methods"]}


def test_a_generated_client_verifies_rpc_methods_and_ignores_the_outputs():
    client_class = shipment_output_client_class()
    live = describe(ShipmentWorker, service="shipment_worker").to_dict()
    live["outputs"] = [{**live["outputs"][0], "subject": "different.{batch}.{order_id}"}]
    reply = SimpleNamespace(data=json.dumps(live).encode(), headers=None)
    client = client_class(nats_url="nats://unused.invalid:1")
    client._request = AsyncMock(return_value=reply)
    run(client.verify())
    assert client._verified


def test_bindings_and_the_service_surface_are_immutable():
    template, accepted = shipment_child()
    child = child_of(template, accepted)
    with pytest.raises(AttributeError):
        child.output_bindings = None
    for name, value in (("outputs", ()), ("producer", None), ("contract", None)):
        with pytest.raises(FrozenInstanceError):
            setattr(child.output_bindings, name, value)
    assert isinstance(child.output_bindings.outputs, tuple)


# -- a typed event ---------------------------------------------------------------------------------


PRODUCER = OutputProducer(
    "retail", "shipments", "north", "shipping-a", "incarnation", 1, "shipment_batch_a"
)


def test_a_typed_event_carries_the_envelope_and_the_output_object_the_guide_shows():
    (shown,) = fences("json")
    shown = json.loads(shown)["output"]
    template, accepted = shipment_child()
    child = child_of(template, accepted, producer=PRODUCER)
    run(
        child.progress.publish(
            ShipmentProgress(quantity=2, stage="sent"), parameters={"order_id": "one"}
        )
    )
    _, envelope, _ = child.nc.last()
    assert set(envelope) == {"data", "timestamp", "source_service", "correlation_id", "output"}
    assert envelope["source_service"] == "shipment_batch_a"
    output = envelope["output"]
    assert set(output) == set(shown) == {"name", "contract", "producer"}
    assert output["name"] == shown["name"] == "progress"
    assert output["contract"].startswith("sha256:")
    assert output["contract"] == template.output_contract.identity
    assert set(output["producer"]) == set(shown["producer"])
    assert output["producer"]["scope"] == shown["producer"]["scope"]
    assert output["producer"]["template"] == shown["producer"]["template"]
    assert output["producer"]["key"] == shown["producer"]["key"]
    assert output["producer"]["revision"] == shown["producer"]["revision"]
    assert output["producer"]["generation"] == shown["producer"]["generation"]
    assert child.output_bindings.accepts(output)


def test_direct_catalog_construction_emits_a_null_producer_that_nothing_accepts():
    template, accepted = shipment_child()
    child = child_of(template, accepted)
    run(child.summary.publish(ShipmentWorker.summary.model(shipped=1)))
    _, envelope, _ = child.nc.last()
    assert envelope["output"]["producer"] is None
    assert not child.output_bindings.accepts(envelope["output"])


# -- the ordinary send path -------------------------------------------------------------------------


class Stamp(Extension):
    """Sets a header and records what the send hooks saw."""

    def __init__(self):
        self.seen: list[tuple[str, str]] = []

    async def before_call(self, ctx):
        self.seen.append((ctx.kind, ctx.subject))
        ctx.headers["X-Stamp"] = "stamped"


def test_send_hooks_run_around_a_typed_publication_and_a_broker_error_propagates():
    class Stamped(ShipmentWorker):
        stamp = Stamp()

    template, accepted = shipment_child(service_class=Stamped, factory=Stamped)
    child = child_of(template, accepted)
    run(child.container._setup_extensions())
    run(
        child.progress.publish(
            ShipmentProgress(quantity=1, stage="sent"), parameters={"order_id": "one"}
        )
    )
    subject, _, headers = child.nc.last()
    assert headers["X-Stamp"] == "stamped"
    assert child.stamp.seen == [("publish_event", subject)]

    child.nc.publish = AsyncMock(side_effect=ConnectionError("broker refused"))
    with pytest.raises(ConnectionError, match="broker refused"):
        run(
            child.progress.publish(
                ShipmentProgress(quantity=1, stage="sent"), parameters={"order_id": "one"}
            )
        )


def message_id(child, *, key=None):
    run(
        child.progress.publish(
            ShipmentProgress(quantity=1, stage="sent"),
            parameters={"order_id": "one"},
            idempotency_key=key,
        )
    )
    return child.nc.last()[2].get("Nats-Msg-Id")


def generation(number, **config):
    producer = replace(PRODUCER, generation=number)
    template, accepted = shipment_child()
    return child_of(template, accepted, runtime(**config), producer=producer)


def test_an_explicit_key_is_the_message_id_and_spans_generations():
    first, second = generation(1), generation(2)
    assert message_id(first, key="shipment-7")
    assert message_id(first, key="shipment-7") == message_id(second, key="shipment-7")
    assert message_id(first, key="shipment-7") != message_id(first, key="shipment-8")


def test_an_ambient_key_is_caller_controlled_and_spans_generations():
    first, second = generation(1), generation(2)
    token = IdempotencyContext.set("ambient-9")
    try:
        ambient = message_id(first)
        assert ambient == message_id(second)
    finally:
        IdempotencyContext.reset(token)
    assert message_id(first) is None
    assert ambient == message_id(second, key="ambient-9")


def test_the_automatic_key_covers_the_producer():
    first_a, first_b, second = (generation(n, idempotent_publishing=True) for n in (1, 1, 2))
    assert message_id(first_a)
    assert message_id(first_a) == message_id(first_b)
    assert message_id(first_a) != message_id(second)


class Titled(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Titled"})
    quantity: int = Field(gt=0)
    stage: str


class Untitled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: int = Field(gt=0)
    stage: str


def test_the_automatic_key_changes_when_only_the_output_contract_changes():
    ids = []
    for model in (Titled, Untitled):

        class Worker(CliffracerService):
            progress = Output(
                model, "batches.{batch}.{order_id}", settings=("batch",), parameters=("order_id",)
            )

        template = TemplateCatalog().register(
            shipment_output_template(service_class=Worker, factory=lambda s, r, w=Worker: w(r))
        )
        accepted = template.normalize({"warehouse": "north", "batch": "north"})
        child = child_of(template, accepted, runtime(idempotent_publishing=True), producer=PRODUCER)
        run(child.progress.publish(model(quantity=1, stage="sent"), parameters={"order_id": "one"}))
        ids.append(child.nc.last()[2]["Nats-Msg-Id"])
    assert ids[0] != ids[1]
