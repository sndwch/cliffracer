"""A model's own `__eq__` does not decide which form of it is sent.

A model compared by its record id (`__eq__` reads `id` alone) whose two fields have crossed aliases
reads one of its forms back with those fields swapped, and its own `__eq__` calls that equal.
Judged by that, a generated client settled on the swapping form and then refused the call as
having no form that arrives intact, though another form does; and a service's `call_rpc` or
`publish_event` sent the swapping form, so the model arrived changed and nothing said so. Judged by
its class and field values, it goes out in the form that arrives intact, as the same model with
pydantic's own `__eq__` does, at the top level and nested.
"""

import pydantic_core
import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cliffracer import ServiceClient
from cliffracer.core.validation import _same, wire_models

pytestmark = pytest.mark.unit


class ById(BaseModel):
    """Equal by record id, as ORM-style models often are; `first` and `last` alias each other."""

    model_config = ConfigDict(populate_by_name=True)
    id: int
    first: str = Field(alias="last")
    last: str = Field(alias="first")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ById) and self.id == other.id

    __hash__ = None  # type: ignore[assignment]


class Plain(BaseModel):
    """`ById`'s twin with pydantic's own `__eq__`."""

    model_config = ConfigDict(populate_by_name=True)
    id: int
    first: str = Field(alias="last")
    last: str = Field(alias="first")


class HoldsById(BaseModel):
    account: ById


class HoldsPlain(BaseModel):
    account: Plain


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


def _fields(model: BaseModel) -> dict:
    return {
        name: _fields(v) if isinstance(v, BaseModel) else v
        for name, v in ((name, getattr(model, name)) for name in type(model).model_fields)
    }


def _account(cls):
    # By field name, so `first` holds "x" and `last` "y", whatever the aliases say.
    return cls.model_validate({"id": 1, "first": "x", "last": "y"}, by_name=True, by_alias=False)


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(_account(ById), ById, {"id": 1, "last": "x", "first": "y"}, id="equal-by-id"),
        pytest.param(_account(Plain), Plain, {"id": 1, "last": "x", "first": "y"}, id="plain-twin"),
        pytest.param(
            HoldsById(account=_account(ById)),
            HoldsById,
            {"account": {"id": 1, "last": "x", "first": "y"}},
            id="equal-by-id-nested",
        ),
        pytest.param(
            HoldsPlain(account=_account(Plain)),
            HoldsPlain,
            {"account": {"id": 1, "last": "x", "first": "y"}},
            id="plain-twin-nested",
        ),
    ],
)
def test_a_model_is_sent_in_the_form_that_arrives_intact_whatever_its_eq(value, annotation, wire):
    by_name = TypeAdapter(annotation).dump_python(value, mode="json", by_alias=False)
    assert _fields(TypeAdapter(annotation).validate_python(by_name)) != _fields(value), (
        "the by-name form must read back changed for this row to mean anything"
    )

    sent = _encode(value, annotation)

    assert sent == wire
    assert _fields(TypeAdapter(annotation).validate_python(sent)) == _fields(value)


class SerById(BaseModel):
    """Equal by record id; `first` and `last` are WRITTEN under each other's names (crossed
    serialization aliases), and read by their own."""

    id: int
    first: str = Field(serialization_alias="last")
    last: str = Field(serialization_alias="first")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SerById) and self.id == other.id

    __hash__ = None  # type: ignore[assignment]


class SerPlain(BaseModel):
    """`SerById`'s twin with pydantic's own `__eq__`."""

    id: int
    first: str = Field(serialization_alias="last")
    last: str = Field(serialization_alias="first")


class HoldsSerById(BaseModel):
    account: SerById


class HoldsSerPlain(BaseModel):
    account: SerPlain


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(SerById(id=1, first="x", last="y"), id="equal-by-id"),
        pytest.param(SerPlain(id=1, first="x", last="y"), id="plain-twin"),
        pytest.param(
            HoldsSerById(account=SerById(id=1, first="x", last="y")), id="equal-by-id-nested"
        ),
        pytest.param(
            HoldsSerPlain(account=SerPlain(id=1, first="x", last="y")), id="plain-twin-nested"
        ),
    ],
)
def test_a_service_sends_a_model_in_the_form_that_arrives_intact_whatever_its_eq(value):
    """`call_rpc` and `publish_event` write a model by alias first. For these the alias form is
    read back with `first` and `last` swapped, which an id-only `__eq__` called equal: the model
    arrived changed and nothing said so."""
    alias_form = pydantic_core.to_jsonable_python(value)
    assert _fields(type(value).model_validate(alias_form)) != _fields(value), (
        "the alias form must read back changed for this row to mean anything"
    )

    sent = wire_models({"a": value})["a"]

    assert _fields(type(value).model_validate(sent)) == _fields(value), sent


class Narrow(BaseModel):
    f: int


class Wider(Narrow):
    g: int = 2


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param(Narrow(f=1), Wider(f=1, g=2), id="narrower-first"),
        pytest.param(Wider(f=1, g=2), Narrow(f=1), id="wider-first"),
    ],
)
def test_two_models_holding_different_fields_are_not_the_same_in_either_order(a, b):
    """Judged field by field, a model is not the same as one holding more fields, whichever is
    compared first."""
    assert _same(a, b) is False
