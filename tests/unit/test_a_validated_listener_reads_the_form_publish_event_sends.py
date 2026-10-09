"""A schema-validated listener reads the nested form `publish_event` sends, as a typed listener does.

`publish_event(topic, message=Schema(...))` sends `{"message": {...}}`. `@validated_listener(topic,
Schema)` read the whole payload flat as `Schema`: a schema with a required field or `extra="forbid"`
refused it, and a schema whose fields all have defaults read it as an empty model and dropped the
published values.

The rule is the typed listener's, held in one helper: the payload is the handler parameter's object
when no field of the schema is named or aliased like the parameter and the payload is exactly
`{<parameter>: <object>}`, apart from the `correlation_id` an event carries; any other payload,
a `RootModel`'s included, is read flat.
"""

import json

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, RootModel, model_validator

from cliffracer import CliffracerService, ServiceConfig, validated_listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "items.created"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    qty: int = 1


class Lenient(BaseModel):
    name: str = "default"
    qty: int = 1


class Wrapper(BaseModel):
    """A schema with a field named like the parameter that carries it."""

    message: Strict


class Tagged(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    correlation_id: str | None = None


class FromANumber(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int

    @model_validator(mode="before")
    @classmethod
    def accept_a_number(cls, data):
        return {"n": data} if isinstance(data, int) else data


def consumer(schema, parameter):
    if parameter == "message":

        class Consumer(CliffracerService):
            received: list = []

            @validated_listener(SUBJECT, schema, fanout=True)
            async def on(self, message: BaseModel) -> None:
                self.received.append(message)

    else:

        class Consumer(CliffracerService):  # type: ignore[no-redef]
            received: list = []

            @validated_listener(SUBJECT, schema, fanout=True)
            async def on(self, payload: BaseModel) -> None:
                self.received.append(payload)

    return Consumer


async def deliver(schema, body, parameter="message"):
    svc = consumer(schema, parameter)(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    message = MockMessage(
        subject=SUBJECT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    outcome = await svc.container._dispatch_event(message, pattern=SUBJECT)
    return outcome, svc.received


async def test_the_flat_form_delivers_the_schema():
    outcome, received = await deliver(Strict, {"name": "a", "qty": 2})

    assert outcome == DispatchOutcome.OK and received == [Strict(name="a", qty=2)]


async def test_the_nested_form_delivers_the_same_schema():
    outcome, received = await deliver(Strict, {"message": {"name": "a", "qty": 2}})

    assert outcome == DispatchOutcome.OK and received == [Strict(name="a", qty=2)]


async def test_a_schema_of_defaults_reads_the_nested_values_and_not_an_empty_model():
    outcome, received = await deliver(Lenient, {"message": {"name": "a", "qty": 2}})

    assert outcome == DispatchOutcome.OK and received == [Lenient(name="a", qty=2)]


async def test_the_enveloped_nested_form_delivers_the_schema():
    envelope = {
        "data": {"message": {"name": "a"}},
        "timestamp": "2026-10-03T12:00:00+00:00",
        "source_service": "publisher",
        "correlation_id": "c-1",
    }

    outcome, received = await deliver(Strict, envelope)

    assert outcome == DispatchOutcome.OK and received == [Strict(name="a")]


async def test_the_correlation_id_an_event_carries_is_not_a_key_of_the_nested_form():
    outcome, received = await deliver(Strict, {"message": {"name": "a"}, "correlation_id": "c-1"})

    assert outcome == DispatchOutcome.OK and received == [Strict(name="a")]


async def test_the_nested_key_is_the_name_of_the_handlers_parameter():
    outcome, received = await deliver(Strict, {"payload": {"name": "a"}}, parameter="payload")

    assert outcome == DispatchOutcome.OK and received == [Strict(name="a")]


async def test_a_nested_form_that_is_not_the_schema_is_still_refused():
    """Read flat, this payload is a `Lenient` of defaults with `message` ignored: only the nested
    reading refuses it."""
    outcome, received = await deliver(Lenient, {"message": {"name": 5}})

    assert outcome == DispatchOutcome.INVALID and received == []


async def test_CONTROL_a_schema_with_a_field_named_like_the_parameter_is_read_flat():
    outcome, received = await deliver(Wrapper, {"message": {"name": "a"}})

    assert outcome == DispatchOutcome.OK and received == [Wrapper(message=Strict(name="a"))]


async def test_CONTROL_a_payload_with_another_key_besides_the_parameter_is_read_flat():
    outcome, received = await deliver(Strict, {"message": {"name": "a"}, "name": "b"})

    assert outcome == DispatchOutcome.INVALID and received == []


async def test_CONTROL_a_payload_with_another_key_is_read_flat_for_a_schema_of_defaults():
    outcome, received = await deliver(Lenient, {"message": {"name": "a"}, "qty": 3})

    assert outcome == DispatchOutcome.OK and received == [Lenient(name="default", qty=3)]


async def test_CONTROL_a_value_that_is_not_an_object_is_read_flat():
    """Read as the nested form, `5` is not a `Lenient` and is refused; read flat, it is one of
    defaults."""
    outcome, received = await deliver(Lenient, {"message": 5})

    assert outcome == DispatchOutcome.OK and received == [Lenient()]


async def test_CONTROL_a_value_that_is_not_an_object_is_never_read_as_a_schema_it_could_become():
    outcome, received = await deliver(FromANumber, {"message": 5})

    assert outcome == DispatchOutcome.INVALID and received == []


async def test_CONTROL_a_schema_that_declares_a_correlation_id_reads_it_as_its_own_field():
    outcome, received = await deliver(Tagged, {"message": {"name": "a"}, "correlation_id": "c-1"})

    assert outcome == DispatchOutcome.INVALID and received == []


async def test_CONTROL_another_parameter_name_is_not_the_nested_key():
    outcome, received = await deliver(Strict, {"message": {"name": "a"}}, parameter="payload")

    assert outcome == DispatchOutcome.INVALID and received == []


class Aliased(BaseModel):
    """A schema that reads its one field from the key `message`: that payload is flat."""

    thing: dict = Field(alias="message")


class Choices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thing: dict = Field(validation_alias=AliasChoices("message", "thing"))


class Rooted(RootModel[dict[str, dict]]):
    """A schema whose whole value is a mapping: `{"message": {...}}` is its value."""


async def test_CONTROL_a_schema_that_reads_the_parameters_name_as_an_alias_is_read_flat():
    outcome, received = await deliver(Aliased, {"message": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Aliased(message={"a": 1})]


async def test_CONTROL_a_schema_that_reads_the_parameters_name_as_a_validation_alias_is_read_flat():
    outcome, received = await deliver(Choices, {"message": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Choices(message={"a": 1})]


async def test_CONTROL_a_root_model_schema_is_never_unwrapped():
    outcome, received = await deliver(Rooted, {"message": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [Rooted({"message": {"a": 1}})]


class ChoicesLater(BaseModel):
    """`message` is a later member of the field's validation aliases, not the first."""

    model_config = ConfigDict(extra="forbid")

    thing: dict = Field(validation_alias=AliasChoices("thing", "message"))


class ExtraDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "default"
    qty: int = 1


async def test_CONTROL_a_later_validation_alias_choice_named_like_the_parameter_is_read_flat():
    outcome, received = await deliver(ChoicesLater, {"message": {"a": 1}})

    assert outcome == DispatchOutcome.OK and received == [ChoicesLater(message={"a": 1})]


async def test_a_schema_of_defaults_that_keeps_extra_keys_reads_a_lone_parameter_key_as_nested():
    """The same payload is the schema with `message` as an extra key, or the nested form: the
    nested form is read."""
    outcome, received = await deliver(ExtraDefaults, {"message": {"name": "a", "qty": 2}})

    assert outcome == DispatchOutcome.OK and received == [ExtraDefaults(name="a", qty=2)]
