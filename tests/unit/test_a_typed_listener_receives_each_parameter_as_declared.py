"""A typed listener with several parameters receives each one as the type it declares.

The handler's arguments are validated as a model synthesized from its parameters. They were then
read back with `model_dump()`, which turns every nested model and aliased field into plain
data again, so `item: Item` arrived as a `dict` (keyed by field name even where the model aliases
it), and the handler's own annotation was not honoured. An RPC handler is given the validated
attribute of each parameter, and so is a listener now.
"""

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.messages import Message
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "items.created"


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    qty: int = 1


class Aliased(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    box_width: int = Field(alias="boxWidth")


class ByName(BaseModel):
    """Reads its field by name only, so its by-alias dump is a form it does not read back."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)

    thing: int = Field(alias="other")


class Note(Message):
    text: str = ""


async def deliver(handler_name: str, body: dict, *, correlation_id: str | None = None):
    class Consumer(CliffracerService):
        received: list = []

        @listener(SUBJECT, fanout=True)
        async def on_item(self, item: Item, count: int) -> None:
            self.received.append((item, count))

        @listener("aliased.created", fanout=True)
        async def on_aliased(self, item: Aliased, count: int) -> None:
            self.received.append((item, count))

        @listener("byname.created", fanout=True)
        async def on_byname(self, item: ByName, count: int) -> None:
            self.received.append((item, count))

        @listener("items.listed", fanout=True)
        async def on_listed(self, items: list[Item], by_name: dict[str, Item], count: int) -> None:
            self.received.append((items, by_name, count))

        @listener("items.maybe", fanout=True)
        async def on_maybe(self, item: Item | None, count: int) -> None:
            self.received.append((item, count))

        @listener("notes.created", fanout=True)
        async def on_note(self, note: Note, count: int) -> None:
            self.received.append((note, count))

        @listener("plain.created", fanout=True)
        async def on_plain(self, name: str, count: int = 0) -> None:
            self.received.append((name, count))

    subjects = {
        "on_item": SUBJECT,
        "on_aliased": "aliased.created",
        "on_byname": "byname.created",
        "on_listed": "items.listed",
        "on_maybe": "items.maybe",
        "on_note": "notes.created",
        "on_plain": "plain.created",
    }
    svc = Consumer(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    subject = subjects[handler_name]
    headers = {"Content-Type": "application/json"}
    if correlation_id is not None:
        body = {**body, "correlation_id": correlation_id}
    message = MockMessage(subject=subject, data=json.dumps(body).encode(), headers=headers)
    outcome = await svc.container._dispatch_event(message, pattern=subject)
    return outcome, svc.received


async def test_a_model_parameter_arrives_as_the_model():
    outcome, received = await deliver("on_item", {"item": {"name": "a", "qty": 2}, "count": 3})

    assert outcome == DispatchOutcome.OK
    assert received == [(Item(name="a", qty=2), 3)]
    assert type(received[0][0]) is Item


async def test_an_aliased_model_parameter_arrives_as_the_model_with_its_alias_read():
    outcome, received = await deliver("on_aliased", {"item": {"boxWidth": 4}, "count": 3})

    assert outcome == DispatchOutcome.OK
    assert type(received[0][0]) is Aliased and received[0][0].box_width == 4


async def test_a_model_read_by_name_only_arrives_as_the_model_it_was_validated_as():
    """The validated model is handed over as it is: not dumped by alias and read again, which a
    model that reads its fields by name only refuses."""
    outcome, received = await deliver("on_byname", {"item": {"thing": 1}, "count": 2})

    assert outcome == DispatchOutcome.OK
    assert received == [(ByName(thing=1), 2)] and type(received[0][0]) is ByName


async def test_models_inside_a_list_and_a_dict_arrive_as_models():
    body = {
        "items": [{"name": "a"}, {"name": "b", "qty": 2}],
        "by_name": {"x": {"name": "c"}},
        "count": 1,
    }

    outcome, received = await deliver("on_listed", body)

    items, by_name, count = received[0]
    assert outcome == DispatchOutcome.OK and count == 1
    assert [type(i) for i in items] == [Item, Item] and type(by_name["x"]) is Item


@pytest.mark.parametrize("item", [{"name": "a"}, None], ids=["given", "none"])
async def test_an_optional_model_parameter_is_the_model_or_none(item):
    outcome, received = await deliver("on_maybe", {"item": item, "count": 1})

    assert outcome == DispatchOutcome.OK
    assert received[0][0] == (Item(name="a") if item else None)


async def test_a_message_parameter_is_given_the_events_correlation_id_as_an_rpc_gives_it():
    outcome, received = await deliver(
        "on_note", {"note": {"text": "hi"}, "count": 1}, correlation_id="c-9"
    )

    assert outcome == DispatchOutcome.OK
    assert received[0][0].correlation_id == "c-9"


async def test_CONTROL_plain_parameters_arrive_unchanged():
    outcome, received = await deliver("on_plain", {"name": "a", "count": 2})

    assert outcome == DispatchOutcome.OK and received == [("a", 2)]
