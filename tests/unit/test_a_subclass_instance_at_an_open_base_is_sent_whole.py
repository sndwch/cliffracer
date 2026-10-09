"""A model subclass instance sent where its base is declared keeps its own fields when the base
allows extras.

The client writes a value through its declared annotation, and pydantic writes a model by its
declared class, so the fields only the subclass declares are not in that dump. A base with
`extra="allow"` holds such fields as its extras, so they are added to the form and the receiver
reads them there. A base that ignores or forbids extras could not hold them, and gets the form it
always did.
"""

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from cliffracer.client import ServiceClient
from cliffracer.core.validation import read_python_then_json, wire_models

pytestmark = pytest.mark.unit


class OpenBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    a: int


class OpenChild(OpenBase):
    c: int = 0


class IgnoringBase(BaseModel):
    a: int


class IgnoringChild(IgnoringBase):
    c: int = 0


class ForbiddingBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int


class ForbiddingChild(ForbiddingBase):
    c: int = 0


class Holds(BaseModel):
    item: OpenBase
    items: list[OpenBase]


class HoldsIgnoring(BaseModel):
    item: IgnoringBase


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


def _received(wire, annotation):
    adapter = TypeAdapter(annotation)
    return read_python_then_json(wire, adapter.validate_python, adapter.validate_json)


def test_a_subclass_instance_at_an_open_base_arrives_with_its_own_fields_as_extras():
    wire = _encode(OpenChild(a=1, c=3, z=9), OpenBase)

    assert wire == {"a": 1, "z": 9, "c": 3}
    assert _received(wire, OpenBase).model_extra == {"z": 9, "c": 3}


def test_a_subclass_instance_held_at_an_open_base_arrives_with_its_own_fields_as_extras():
    sent = Holds(item=OpenChild(a=1, c=3, z=9), items=[OpenChild(a=2, c=4)])

    wire = _encode(sent, Holds)

    assert wire == {"item": {"a": 1, "z": 9, "c": 3}, "items": [{"a": 2, "c": 4}]}
    received = _received(wire, Holds)
    assert received.item.model_extra == {"z": 9, "c": 3}
    assert received.items[0].model_extra == {"c": 4}


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(
            {"x": OpenChild(a=1, c=3)}, dict[str, OpenBase], {"x": {"a": 1, "c": 3}}, id="a-dict"
        ),
        pytest.param(
            (OpenChild(a=1, c=3), OpenChild(a=2, c=4, z=9)),
            tuple[OpenBase, OpenBase],
            [{"a": 1, "c": 3}, {"a": 2, "z": 9, "c": 4}],
            id="a-tuple",
        ),
    ],
)
def test_a_subclass_instance_in_a_dict_or_a_tuple_at_an_open_base_keeps_its_own_fields(
    value, annotation, wire
):
    assert _encode(value, annotation) == wire


def test_a_service_sending_a_model_that_holds_one_at_an_open_base_keeps_its_own_fields():
    """`call_rpc`, `call_async` and `publish_event` write a value by its own class
    (`wire_models`), which writes what it holds by the field's declared class."""
    wire = wire_models(Holds(item=OpenChild(a=1, c=3, z=9), items=[OpenChild(a=2, c=4)]))

    assert wire == {"item": {"a": 1, "z": 9, "c": 3}, "items": [{"a": 2, "c": 4}]}
    assert Holds.model_validate(wire).item.model_extra == {"z": 9, "c": 3}


def test_a_service_sending_a_model_that_holds_one_at_an_ignoring_base_sends_it_as_the_base():
    assert wire_models(HoldsIgnoring(item=IgnoringChild(a=1, c=3))) == {"item": {"a": 1}}


@pytest.mark.parametrize(
    ("value", "annotation"),
    [
        pytest.param(IgnoringChild(a=1, c=3), IgnoringBase, id="a-base-that-ignores-extras"),
        pytest.param(ForbiddingChild(a=1, c=3), ForbiddingBase, id="a-base-that-forbids-extras"),
    ],
)
def test_a_subclass_instance_at_a_base_that_cannot_hold_its_fields_is_sent_as_the_base(
    value, annotation
):
    assert _encode(value, annotation) == {"a": 1}


def test_CONTROL_an_open_base_instance_of_its_own_class_is_sent_as_it_was():
    assert _encode(OpenBase(a=1, z=9), OpenBase) == {"a": 1, "z": 9}
