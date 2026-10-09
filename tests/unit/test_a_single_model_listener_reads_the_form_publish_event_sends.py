"""A listener whose one parameter is a model reads the nested form `publish_event` sends.

`publish_event(topic, item=Item(...))` puts each keyword argument under its name, so the payload is
`{"item": {...}}`. A listener `on(self, item: Item)` read the whole payload as an `Item`, flat. A
model with a required field or `extra="forbid"` refused the nested payload and it was dead-lettered;
a model whose fields all have defaults read it as an empty model and dropped the published values.

The payload is now read as the model's own object when no field of the model is named or aliased like
the parameter (by its alias, or a validation alias: each `AliasChoices` member, the first element of an
`AliasPath`) and the payload is exactly `{<parameter>: <object>}` (the `correlation_id` an event carries
is not a key of it). A `RootModel` is never unwrapped. Any other payload is read flat, as before.
"""

import json

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)
from pydantic.alias_generators import to_camel

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "items.created"


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    qty: int = 1


class Lenient(BaseModel):
    name: str = "default"
    qty: int = 1


class Wrapper(BaseModel):
    """A model with a field named like the parameter that carries it."""

    item: Item


async def deliver(model, body):
    class Consumer(CliffracerService):
        received: list = []

        @listener(SUBJECT, fanout=True)
        async def on(self, item: model) -> None:  # type: ignore[valid-type]
            self.received.append(item)

    svc = Consumer(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    message = MockMessage(
        subject=SUBJECT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    outcome = await svc.container._dispatch_event(message, pattern=SUBJECT)
    return outcome, svc.received


async def test_the_flat_form_delivers_the_model():
    outcome, received = await deliver(Item, {"name": "a", "qty": 2})

    assert outcome == DispatchOutcome.OK and received == [Item(name="a", qty=2)]


async def test_the_nested_form_delivers_the_same_model():
    outcome, received = await deliver(Item, {"item": {"name": "a", "qty": 2}})

    assert outcome == DispatchOutcome.OK and received == [Item(name="a", qty=2)]


async def test_a_model_of_defaults_reads_the_nested_values_and_not_an_empty_model():
    outcome, received = await deliver(Lenient, {"item": {"name": "a", "qty": 2}})

    assert outcome == DispatchOutcome.OK and received == [Lenient(name="a", qty=2)]


async def test_the_enveloped_nested_form_delivers_the_model():
    envelope = {
        "data": {"item": {"name": "a"}},
        "timestamp": "2026-10-03T12:00:00+00:00",
        "source_service": "publisher",
        "correlation_id": "c-1",
    }

    outcome, received = await deliver(Item, envelope)

    assert outcome == DispatchOutcome.OK and received == [Item(name="a")]


async def test_the_correlation_id_an_event_carries_is_not_a_key_of_the_nested_form():
    outcome, received = await deliver(Item, {"item": {"name": "a"}, "correlation_id": "c-1"})

    assert outcome == DispatchOutcome.OK and received == [Item(name="a")]


async def test_a_nested_form_that_is_not_the_model_is_still_refused():
    """Read flat, this payload is a `Lenient` of defaults with `item` ignored: only the nested
    reading refuses it."""
    outcome, received = await deliver(Lenient, {"item": {"name": 5}})

    assert outcome == DispatchOutcome.INVALID and received == []


async def test_CONTROL_a_model_with_a_field_named_like_the_parameter_is_read_flat():
    outcome, received = await deliver(Wrapper, {"item": {"name": "a"}})

    assert outcome == DispatchOutcome.OK and received == [Wrapper(item=Item(name="a"))]


async def test_CONTROL_a_payload_with_another_key_besides_the_parameter_is_read_flat():
    outcome, received = await deliver(Item, {"item": {"name": "a"}, "name": "b"})

    assert outcome == DispatchOutcome.INVALID and received == []


async def test_CONTROL_a_payload_with_another_key_is_read_flat_for_a_model_of_defaults():
    outcome, received = await deliver(Lenient, {"item": {"name": "a"}, "qty": 3})

    assert outcome == DispatchOutcome.OK and received == [Lenient(name="default", qty=3)]


async def test_CONTROL_a_value_under_the_parameters_name_that_is_not_an_object_is_read_flat():
    """Read as the nested form, `5` is not a `Lenient` and is refused; read flat, it is one of
    defaults."""
    outcome, received = await deliver(Lenient, {"item": 5})

    assert outcome == DispatchOutcome.OK and received == [Lenient()]


class Tagged(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    correlation_id: str | None = None


async def test_CONTROL_a_model_that_declares_a_correlation_id_reads_it_as_one_of_its_fields():
    outcome, received = await deliver(Tagged, {"item": {"name": "a"}, "correlation_id": "c-1"})

    assert outcome == DispatchOutcome.INVALID and received == []


class FromANumber(BaseModel):
    """A model that accepts a bare number as well as an object."""

    model_config = ConfigDict(extra="forbid")

    n: int

    @model_validator(mode="before")
    @classmethod
    def accept_a_number(cls, data):
        return {"n": data} if isinstance(data, int) else data


async def test_CONTROL_a_value_that_is_not_an_object_is_never_read_as_the_model_it_could_become():
    outcome, received = await deliver(FromANumber, {"item": 5})

    assert outcome == DispatchOutcome.INVALID and received == []


class Inner(BaseModel):
    a: int


class Aliased(BaseModel):
    """A model that reads its one field from the key `item`: that is flat, not the nested form."""

    model_config = ConfigDict(extra="forbid")

    thing: Inner = Field(alias="item")


class Choices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thing: Inner = Field(validation_alias=AliasChoices("item", "thing"))


class Pathed(BaseModel):
    """A field read from the first element of a path: the payload's `item` is this model's own key."""

    model_config = ConfigDict(extra="forbid")

    value: int = Field(validation_alias=AliasPath("item", "a"))


class Rooted(RootModel[dict[str, dict]]):
    """A model whose whole value is a mapping: `{"item": {...}}` is its value, never a wrapper."""


class Camel(BaseModel):
    """A field whose alias comes from an alias generator: `itemData`, for the field `item_data`."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)

    item_data: Inner


async def deliver_under(parameter, model, body):
    namespace: dict = {}
    exec(
        f"async def on(self, {parameter}: model) -> None:\n    self.received.append({parameter})\n",
        {"model": model},
        namespace,
    )

    class Consumer(CliffracerService):
        received: list = []
        on = listener(SUBJECT, fanout=True)(namespace["on"])

    svc = Consumer(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    message = MockMessage(
        subject=SUBJECT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    outcome = await svc.container._dispatch_event(message, pattern=SUBJECT)
    return outcome, svc.received


async def test_CONTROL_a_model_that_reads_the_parameters_name_as_an_alias_is_read_flat():
    outcome, received = await deliver(Aliased, {"item": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Aliased(item={"a": 1})]


async def test_CONTROL_a_model_that_reads_the_parameters_name_as_a_validation_alias_choice_is_read_flat():
    outcome, received = await deliver(Choices, {"item": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Choices(item={"a": 1})]


async def test_CONTROL_a_model_that_reads_a_field_from_a_path_starting_at_the_parameters_name_is_read_flat():
    outcome, received = await deliver(Pathed, {"item": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Pathed.model_validate({"item": {"a": 1}})]


async def test_CONTROL_a_root_model_is_never_unwrapped():
    outcome, received = await deliver(Rooted, {"item": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Rooted({"item": {"a": 1}})]


async def test_CONTROL_a_model_that_reads_the_parameters_name_as_a_generated_alias_is_read_flat():
    outcome, received = await deliver_under("itemData", Camel, {"itemData": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Camel(itemData={"a": 1})]


async def test_the_nested_form_is_still_read_when_the_parameter_is_not_a_key_the_model_reads():
    outcome, received = await deliver_under("payload", Camel, {"payload": {"itemData": {"a": 1}}})

    assert outcome == DispatchOutcome.OK and received == [Camel(itemData={"a": 1})]


class Overridden(BaseModel):
    """A field whose alias is `item` and whose validation alias is another key."""

    a: int = 0
    thing: int = Field(0, alias="item", validation_alias="other")


async def test_CONTROL_a_key_that_is_a_fields_alias_counts_as_declared_whatever_its_validation_alias():
    """The payload `{"item": {...}}` a flat publisher sends is read as it was: as the model."""
    outcome, received = await deliver(Overridden, {"item": {"a": 5}})

    assert outcome == DispatchOutcome.OK and received == [Overridden()]


class ChoicesLater(BaseModel):
    """`item` is a later member of the field's validation aliases, not the first."""

    model_config = ConfigDict(extra="forbid")

    thing: Inner = Field(validation_alias=AliasChoices("thing", "item"))


class PathEndingInTheParameter(BaseModel):
    """A path that starts at `x`: `item` is a key inside it, not one of the model's own keys."""

    value: int = Field(0, validation_alias=AliasPath("x", "item"))


class ExtraDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "default"
    qty: int = 1


async def test_CONTROL_a_later_validation_alias_choice_named_like_the_parameter_is_read_flat():
    outcome, received = await deliver(ChoicesLater, {"item": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [ChoicesLater(item={"a": 1})]


async def test_a_path_whose_later_element_is_the_parameters_name_does_not_make_it_a_key():
    outcome, received = await deliver(PathEndingInTheParameter, {"item": {"x": {"item": 5}}})

    assert outcome == DispatchOutcome.OK and [r.value for r in received] == [5]


async def test_a_model_of_defaults_that_keeps_extra_keys_reads_a_lone_parameter_key_as_nested():
    """The same payload is the model with `item` as an extra key, or the nested form: the nested
    form is read."""
    outcome, received = await deliver(ExtraDefaults, {"item": {"name": "a", "qty": 2}})

    assert outcome == DispatchOutcome.OK and received == [ExtraDefaults(name="a", qty=2)]


async def test_a_payload_that_is_not_an_object_is_read_flat():
    """A bare number is no nested form: the model reads it as it reads any payload."""
    outcome, received = await deliver(FromANumber, 5)

    assert outcome == DispatchOutcome.OK and received == [FromANumber(n=5)]


class OnePath(BaseModel):
    """A field read from a one-element path: `item` is this model's own key, so the payload is flat."""

    model_config = ConfigDict(extra="forbid")

    thing: Inner = Field(validation_alias=AliasPath("item"))


async def test_a_one_element_path_counts_its_key_as_one_the_model_reads():
    outcome, received = await deliver(OnePath, {"item": {"a": 1}})

    assert outcome == DispatchOutcome.OK
    assert received == [OnePath.model_validate({"item": {"a": 1}})]
    assert received[0].thing == Inner(a=1)
